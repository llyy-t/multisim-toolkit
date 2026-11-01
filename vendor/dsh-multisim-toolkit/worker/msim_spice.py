"""SPICE netlist channel: run a SPICE deck through Multisim's DoCommandLine and
read the resulting SPICE3 raw file back into plain numbers.

This module has no COM dependency and no third-party dependency: it is pure
stdlib so it can be imported by ``msim_com`` (which owns the COM session) and by
tests on any interpreter bitness.

Why this exists
---------------
The COM analysis API (``DoDCOperatingPoint`` / ``DoACSweep`` / ``RunSimulation``)
only ever reports *named outputs*, and a design only has named outputs when
something on the schematic writes one (a probe, an instrument, a power meter).
A netlist dropped into Multisim becomes a schematic with **no** probes, so every
COM analysis on it fails with ``输出未找到`` (output not found).

``DoCommandLine`` does not work that way: it runs the deck on Multisim's SPICE
engine directly and writes a SPICE3 raw file with **every** node and branch
current in it. Measured on this machine: 0.25-0.55 s per analysis. That is the
only path that can analyse a circuit nobody drew.

Limits (measured, do not try to work around them):
  * Only standard SPICE primitives survive: R C L V I E F G H B D Q M and
    behavioural sources. Multisim *library* parts (74LS00, D_CHIP, ...) are
    silently dropped: an ``X`` line naming a library model produces no
    additional variables.
  * Inline ``.subckt`` **does** work (verified: a behavioural NAND inside a
    subcircuit instantiates and simulates; nodes come back as ``V(n1)`` and
    branch currents as ``I(bgate:x1)``). The subcircuit body must come after
    the lines that instantiate it is not required, but the deck must contain at
    least one grounded component or Multisim refuses with
    ``No ground node was found`` / ``No nodes present in circuit``.
  * The netlist and every path handed to DoCommandLine must be pure ASCII and
    free of spaces. A UTF-8 BOM or a Chinese first-line comment corrupts the
    first card line and the engine then reports ``No nodes present in circuit``.
  * Every path handed to DoCommandLine must be free of spaces.
"""

from __future__ import annotations

import math
import os
import re
import struct
from typing import Any, Dict, List, Optional, Sequence, Tuple


class SpiceError(Exception):
    """Raised for anything this module cannot do; carries a stable code."""

    def __init__(self, code: str, message: str, detail: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        out = {"code": self.code, "message": self.message}
        if self.detail:
            out["detail"] = self.detail
        return out


# --------------------------------------------------------------------------
# netlist / command file assembly
# --------------------------------------------------------------------------

_ANALYSIS_PREFIXES = (
    "op",
    "dc",
    "ac",
    "tran",
    "tf",
    "noise",
    "pz",
    "sens",
    "disto",
    "four",
    "meas",
    "measure",
    "let",
    "print",
    "plot",
    "set",
    "linearize",
    "fourier",
    "sp",
)


def _require_no_spaces(path: str, label: str) -> str:
    if not path:
        raise SpiceError("BAD_ARGS", "%s must not be empty" % label)
    if " " in path:
        raise SpiceError(
            "BAD_ARGS",
            "%s must not contain spaces: %s" % (label, path),
            "Multisim's command engine rejects a command file path with spaces, "
            "so the whole working directory has to be space free.",
        )
    return path


def normalise_netlist(netlist: str) -> str:
    """Return a deck ending with exactly one ``.end`` line.

    Two engine quirks are repaired here because they produce useless error
    messages downstream:

    * a leading UTF-8 BOM makes the first card line unreadable, and the engine
      then reports ``No nodes present in circuit``;
    * non-ASCII text in a card comment corrupts the card, and only an ASCII
      banner is safe on this engine.
    """
    text = (netlist or "").lstrip("\ufeff")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        raise SpiceError("BAD_ARGS", "netlist is empty")
    out: List[str] = []
    for index, line in enumerate(lines):
        if line.isascii():
            out.append(line)
            continue
        if line.lstrip().startswith("*"):
            cleaned = line.encode("ascii", "replace").decode("ascii")
            if index == 0:
                cleaned = "* msim_spice deck (comments were transliterated to ASCII)"
            out.append(cleaned)
            continue
        raise SpiceError(
            "BAD_ARGS",
            "netlist card %d contains non-ASCII text: %r" % (index + 1, line[:80]),
            "Multisim's command engine reads the deck as ASCII. Keep card text "
            "ASCII; put any Chinese explanation in the tool result, not in the "
            "netlist.",
        )
    lines = out
    if not lines[0].lstrip().startswith(("*", ".title", ".TITLE")):
        lines.insert(0, "* msim_spice deck")
    if lines[-1].strip().lower() != ".end":
        lines.append(".end")
    return "\n".join(lines) + "\n"


def normalise_commands(commands: Optional[Sequence[str]]) -> List[str]:
    """Validate Nutmeg command lines and return them in order.

    Only analysis/plotting commands are accepted: ``source`` and ``write`` are
    owned by this module, and a stray ``quit``/``shell`` would kill the engine.
    """
    if commands is None:
        return ["op"]
    if isinstance(commands, str):
        commands = [commands]
    out: List[str] = []
    for raw in commands:
        line = str(raw or "").strip()
        if not line:
            continue
        lowered = line.lower()
        head = lowered.split()[0]
        if head in ("source", "write", "quit", "shell", "cd", "exec", "system"):
            raise SpiceError(
                "BAD_ARGS",
                "command %r is not allowed here" % line,
                "msim_spice owns source/write; pass only analysis or plotting "
                "commands such as op, dc V1 0 5 1, tran 10u 1m, ac dec 10 10 1meg.",
            )
        if not any(lowered.startswith(p) for p in _ANALYSIS_PREFIXES):
            raise SpiceError(
                "BAD_ARGS",
                "unrecognised SPICE command: %r" % line,
                "expected one of: op, dc, ac, tran, tf, noise, pz, sens, disto, "
                "four, meas, let, print, plot, set, linearize.",
            )
        out.append(line)
    return out or ["op"]


def build_command_file(
    netlist_path: str, raw_paths: Sequence[str], commands: Sequence[str]
) -> str:
    """Assemble the run.txt Multisim reads.

    One ``write`` per analysis, so each analysis lands in its own raw file
    instead of overwriting the previous plot.
    """
    if len(raw_paths) != len(commands):
        raise SpiceError("INTERNAL", "one raw path per command is required")
    lines = ["source %s" % netlist_path]
    for command, raw in zip(commands, raw_paths):
        lines.append(command)
        lines.append("write %s" % raw)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# SPICE3 raw reader
# --------------------------------------------------------------------------
#
# Multisim writes the ASCII flavour of the SPICE3 raw format:
#
#   Title: * rc
#   Date: Fri Oct 30 20:45:07  2026
#   Plotname: DC Operating Point
#   Flags: real
#   No. Variables: 3
#   No. Points: 1
#   Variables:
#   \t0\tn1\tvoltage\tV(in)
#   ...
#   Values:
#    0\t0.000000000000000e+00
#   \t0.000000000000000e+00
#
# A complex scalar is written as "re,im". The first variable of an AC or
# transient plot is the scale (frequency / time); for the other plots it is the
# swept quantity.

_NUMBER = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")


def _parse_variables(block: str) -> List[dict]:
    variables: List[dict] = []
    for line in block.split("\n"):
        line = line.strip()
        if not line or not line[0].isdigit():
            continue
        fields = [field for field in re.split(r"\t+|\s{2,}", line) if field]
        if len(fields) < 3:
            continue
        plot = fields[3].strip() if len(fields) > 3 else ""
        variables.append(
            {
                "index": int(fields[0]),
                "name": fields[1].strip(),
                "type": fields[2].strip(),
                "plot": plot,
            }
        )
    variables.sort(key=lambda item: item["index"])
    return variables


def _column_label(variable: dict, first: bool) -> str:
    kind = (variable.get("type") or "").lower()
    if first and kind in ("frequency", "time"):
        return kind
    plot = (variable.get("plot") or "").strip()
    if plot:
        # drop the "grid=n" scale annotation Multisim appends to the axis name
        return re.sub(r"\s+grid=\d+\s*$", "", plot)
    return variable.get("name") or "v%d" % variable.get("index", 0)


def _parse_values(body: str) -> List[List[str]]:
    """Split the Values section into one list of raw scalar tokens per point.

    Multisim writes the first value of a point on a line that starts with the
    point index (``" 3\\t1.23e+00"``) and the remaining values on lines that
    start with a tab. So: a tab-led line continues the current point, anything
    else opens a new one and its leading token is the index.
    """
    blocks: List[List[str]] = []
    for line in body.split("\n"):
        if not line.strip():
            continue
        tokens = _NUMBER.findall(line)
        if not tokens:
            continue
        if line[:1] == "\t":
            if blocks:
                blocks[-1].extend(tokens)
            else:
                blocks.append(tokens)
            continue
        blocks.append(tokens[1:])
    return blocks


def parse_raw(data: bytes) -> Dict[str, Any]:
    """Parse a SPICE3 raw file into ``{plotname, columns, n_points, rows}``.

    ``rows`` holds one list per point, ordered like ``columns``. A complex plot
    (AC analysis) is reduced at parse time to magnitude and phase in degrees,
    because that is what a Bode plot needs and it keeps the columns to two per
    signal instead of four.
    """
    if not data:
        raise SpiceError("SPICE_ERROR", "raw file is empty")
    text = data.decode("latin-1", "replace")
    if "\nBinary:" in text or text.startswith("Binary:"):
        raise SpiceError(
            "SPICE_ERROR",
            "binary SPICE raw files are not supported",
            "Multisim writes ASCII raw files; this one appears to be binary.",
        )
    if "Values:" not in text or "Variables:" not in text:
        raise SpiceError(
            "SPICE_ERROR",
            "not a SPICE3 raw file",
            text[:300].strip() or "<empty file>",
        )

    head, _, rest = text.partition("Variables:")
    variables_block, _, body = rest.partition("Values:")
    pairs: Dict[str, str] = {}
    for line in head.split("\n"):
        key, sep, value = line.partition(":")
        if not sep:
            continue
        pairs[key.strip().lower()] = value.strip()

    variables = _parse_variables(variables_block)
    if not variables:
        raise SpiceError("SPICE_ERROR", "raw file declares no variables", head[:300])
    n_vars = len(variables)
    declared_vars = pairs.get("no. variables") or pairs.get("no. variables:") or ""
    if declared_vars:
        try:
            if int(float(declared_vars)) != n_vars:
                raise SpiceError(
                    "SPICE_ERROR",
                    "raw file variable count mismatch",
                    "header says %s, parsed %d" % (declared_vars, n_vars),
                )
        except ValueError:
            pass

    flags = (pairs.get("flags") or "").lower()
    complex_mode = "complex" in flags

    blocks = _parse_values(body)
    rows: List[List[float]] = []
    for raw_tokens in blocks:
        numbers: List[float] = []
        for token in raw_tokens:
            numbers.append(float(token))
        wanted = n_vars * (2 if complex_mode else 1)
        if len(numbers) < wanted:
            continue
        numbers = numbers[:wanted]
        if complex_mode:
            # scale column is complex but always real-valued; keep it scalar.
            # magnitude is reported in dB because these plots are Bode plots.
            row: List[float] = [numbers[0]]
            for index in range(1, n_vars):
                real = numbers[index * 2]
                imag = numbers[index * 2 + 1]
                magnitude = math.hypot(real, imag)
                row.append(20.0 * math.log10(magnitude) if magnitude > 0 else -400.0)
                row.append(math.degrees(math.atan2(imag, real)))
            rows.append(row)
        else:
            rows.append(numbers)

    if not rows:
        raise SpiceError(
            "SPICE_ERROR",
            "raw file has no data points",
            "flags=%r variables=%d" % (flags, n_vars),
        )

    columns: List[str] = []
    if complex_mode:
        columns.append(_column_label(variables[0], True))
        for variable in variables[1:]:
            label = _column_label(variable, False)
            columns.append(label + " (dB)")
            columns.append(label + " (phase deg)")
    else:
        for index, variable in enumerate(variables):
            columns.append(_column_label(variable, index == 0))

    declared_points = pairs.get("no. points") or ""
    if declared_points:
        try:
            expected = int(float(declared_points))
        except ValueError:
            expected = 0
        if expected and expected != len(rows):
            # keep the parsed rows: the values section is authoritative
            pass

    return {
        "plotname": pairs.get("plotname", ""),
        "title": pairs.get("title", ""),
        "flags": flags,
        "complex": complex_mode,
        "columns": columns,
        "n_points": len(rows),
        "rows": rows,
    }


# --------------------------------------------------------------------------
# presentation helpers
# --------------------------------------------------------------------------


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except Exception:
        return False


def sample_rows(rows: Sequence[Sequence[float]], max_points: int) -> Tuple[List[List[float]], int]:
    """Uniformly subsample to at most ``max_points`` rows, keeping both ends."""
    total = len(rows)
    if max_points <= 0 or total <= max_points:
        return [list(row) for row in rows], 1
    step = int(math.ceil(total / float(max_points)))
    kept = [list(row) for row in rows[::step]]
    if kept and rows and kept[-1] is not rows[-1]:
        last = list(rows[-1])
        if not kept or kept[-1] != last:
            kept.append(last)
    return kept, step


def summarise(parsed: Dict[str, Any]) -> List[dict]:
    """Per-column summary: count, first, last, min, max, mean (finite only)."""
    columns = parsed.get("columns") or []
    rows = parsed.get("rows") or []
    out: List[dict] = []
    for index, name in enumerate(columns):
        values = [row[index] for row in rows if index < len(row) and _finite(row[index])]
        entry: Dict[str, Any] = {"name": name, "count": len(values)}
        if values:
            entry["first"] = values[0]
            entry["last"] = values[-1]
            entry["min"] = min(values)
            entry["max"] = max(values)
            entry["mean"] = sum(values) / len(values)
        out.append(entry)
    return out


def write_csv(path: str, parsed: Dict[str, Any]) -> dict:
    """Write the plot as CSV with a header row of column names."""
    columns = parsed.get("columns") or []
    rows = parsed.get("rows") or []
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    scale = parsed.get("plotname", "")
    header = []
    for index, name in enumerate(columns):
        header.append(name if index else "%s grid=%d" % (name, len(rows)))
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(",".join(header) + "\n")
        for row in rows:
            handle.write(",".join("%.12g" % value if _finite(value) else str(value) for value in row) + "\n")
    return {"path": path, "bytes": os.path.getsize(path), "rows": len(rows), "columns": len(columns), "plotname": scale}


def _pick_series(columns: Sequence[str], series: Optional[Sequence[str]], x_name: str) -> List[str]:
    """Choose which columns to draw.

    An explicit ``series`` wins. Otherwise every column is drawn except the
    scale, with one exception: on a complex (AC) plot the magnitude columns are
    preferred over the phase columns, because mixing dB and degrees on one y
    axis produces a chart nobody can read.
    """
    if series:
        wanted = [name for name in series if name in columns and name != x_name]
        if wanted:
            return wanted
    rest = [name for name in columns if name != x_name]
    magnitudes = [name for name in rest if name.lower().endswith("(db)")]
    return magnitudes or rest or [columns[0]]


def _collect_curves(
    columns: Sequence[str], rows: Sequence[Sequence[float]], wanted: Sequence[str], x_name: str
) -> List[Tuple[str, List[Tuple[float, float]]]]:
    x_index = columns.index(x_name)
    curves: List[Tuple[str, List[Tuple[float, float]]]] = []
    for name in wanted:
        index = columns.index(name)
        points: List[Tuple[float, float]] = []
        for row in rows:
            if x_index < len(row) and index < len(row):
                x, y = row[x_index], row[index]
                if _finite(x) and _finite(y):
                    points.append((float(x), float(y)))
        if points:
            curves.append((name, points))
    return curves


def _write_bar_png(
    path: str,
    parsed: Dict[str, Any],
    title: Optional[str],
    width: int,
    height: int,
    raster: Any,
    values: Optional[Sequence[str]] = None,
) -> dict:
    """Draw a single-point plot (an ``op`` result) as a labelled bar chart.

    An operating point has one sample per variable, so a line chart through them
    is a meaningless rectangle. A bar per variable is what the numbers actually
    are.
    """
    columns = parsed.get("columns") or []
    rows = parsed.get("rows") or []
    if not columns or not rows:
        raise SpiceError("SPICE_ERROR", "nothing to plot: the raw file has no data")
    row = rows[0]
    wanted = [name for name in (values or columns) if name in columns]
    if not wanted:
        wanted = list(columns)
    wanted = wanted[:14]
    pairs = [(name, float(row[columns.index(name)])) for name in wanted]
    pairs = [(name, value) for name, value in pairs if _finite(value)]
    if not pairs:
        raise SpiceError("SPICE_ERROR", "no finite samples to plot")

    low = min(0.0, min(value for _, value in pairs))
    high = max(0.0, max(value for _, value in pairs))
    if high == low:
        high = low + 1.0

    pad_left, pad_right = 96, 24
    pad_top = 58 if title else 30
    pad_bottom = 84
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    canvas = raster.Canvas(width, height)
    grid = (232, 232, 232)
    axis = (120, 120, 120)
    label = (70, 70, 70)

    def py(value: float) -> float:
        return pad_top + plot_h - (value - low) / (high - low) * plot_h

    for tick in raster.nice_ticks(low, high, 6):
        y = py(tick)
        if pad_top - 1 <= y <= pad_top + plot_h + 1:
            canvas.line(pad_left, y, pad_left + plot_w, y, grid, 1)
            canvas.text_right(pad_left - 10, y - 5, raster.tick_label(tick), label, 2)

    slot = plot_w / float(len(pairs))
    bar = max(4.0, min(46.0, slot * 0.62))
    palette = [(31, 119, 180), (214, 39, 40), (44, 160, 44), (255, 127, 14)]
    for index, (name, value) in enumerate(pairs):
        centre = pad_left + slot * (index + 0.5)
        x0 = centre - bar / 2.0
        top, bottom = py(max(value, 0.0)), py(min(value, 0.0))
        canvas.fill_rect(x0, top, bar, max(1.0, bottom - top), palette[index % len(palette)])
        caption = name
        if canvas.text_width(caption, 2) > slot - 4:
            caption = caption[: max(3, int((slot - 6) / 12))]
        canvas.text_center(centre, pad_top + plot_h + 10, caption, label, 2)
        canvas.text_center(centre, top - 18, raster.tick_label(value), label, 2)

    canvas.fill_rect(pad_left, pad_top, 1, plot_h, axis)
    canvas.fill_rect(pad_left, pad_top + plot_h, plot_w, 1, axis)
    if title:
        canvas.text(pad_left, 12, title, (20, 20, 20), 3)
    canvas.text_center(pad_left + plot_w / 2.0, height - 22, "OPERATING POINT VALUE", label, 2)

    payload = canvas.png()
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(payload)
    return {
        "path": path,
        "bytes": len(payload),
        "width": width,
        "height": height,
        "kind": "bar",
        "series": [name for name, _ in pairs],
        "points": len(pairs),
        "x_column": None,
        "log_x": False,
        "plotname": parsed.get("plotname", ""),
    }


def write_plot_png(
    path: str,
    parsed: Dict[str, Any],
    series: Optional[Sequence[str]] = None,
    title: Optional[str] = None,
    x_column: Optional[str] = None,
    width: int = 900,
    height: int = 520,
    log_x: Optional[bool] = None,
    raster: Any = None,
) -> dict:
    """Render the plot as a PNG line chart.

    ``raster`` is the ``msim_raster`` module (imported by the caller so this
    module stays importable without it).
    """
    columns = parsed.get("columns") or []
    rows = parsed.get("rows") or []
    if not columns or not rows:
        raise SpiceError("SPICE_ERROR", "nothing to plot: the raw file has no data")
    if raster is None:
        import msim_raster as raster  # noqa: F401  (local import keeps this optional)
    if len(rows) < 2:
        return _write_bar_png(
            path, parsed, title, width, height, raster, values=series
        )
    x_name = x_column if x_column in columns else columns[0]
    wanted = _pick_series(columns, series, x_name)
    curves = _collect_curves(columns, rows, wanted, x_name)
    if not curves:
        raise SpiceError(
            "SPICE_ERROR",
            "no finite samples to plot",
            "requested series: %s" % ", ".join(wanted),
        )

    all_x = [point[0] for _, points in curves for point in points]
    all_y = [point[1] for _, points in curves for point in points]
    if log_x is None:
        log_x = x_name.lower() in ("frequency", "freq") and min(all_x) > 0
    if log_x and min(all_x) <= 0:
        log_x = False

    def tx(value: float) -> float:
        return math.log10(value) if log_x else value

    x_low, x_high = tx(min(all_x)), tx(max(all_x))
    y_low, y_high = min(all_y), max(all_y)
    if x_high == x_low:
        x_high = x_low + 1.0
    if y_high == y_low:
        pad = abs(y_low) * 0.1 or 1.0
        y_low, y_high = y_low - pad, y_high + pad
    head = (y_high - y_low) * 0.05
    y_low, y_high = y_low - head, y_high + head

    pad_left, pad_right = 96, 24
    pad_top = 58 if title else 30
    pad_bottom = 56
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    def px(value: float) -> float:
        return pad_left + (tx(value) - x_low) / (x_high - x_low) * plot_w

    def py(value: float) -> float:
        return pad_top + plot_h - (value - y_low) / (y_high - y_low) * plot_h

    canvas = raster.Canvas(width, height)
    grid = (232, 232, 232)
    axis = (120, 120, 120)
    label = (70, 70, 70)

    for tick in raster.nice_ticks(y_low, y_high, 6):
        y = py(tick)
        if pad_top - 1 <= y <= pad_top + plot_h + 1:
            canvas.line(pad_left, y, pad_left + plot_w, y, grid, 1)
            canvas.text_right(pad_left - 10, y - 5, raster.tick_label(tick), label, 2)
    def draw_x_ticks(ticks: Sequence[float]) -> None:
        """只在标签不会互相压住时才画刻度（20 点/decade 时会挤成一团）。"""
        last_right = -1.0
        for tick in ticks:
            x = px(tick)
            if not (pad_left - 1 <= x <= pad_left + plot_w + 1):
                continue
            text = raster.tick_label(tick)
            half = canvas.text_width(text, 2) / 2.0
            left = x - half
            if left < last_right + 10:
                continue
            canvas.line(x, pad_top, x, pad_top + plot_h, grid, 1)
            canvas.text_center(x, pad_top + plot_h + 10, text, label, 2)
            last_right = x + half

    if log_x:
        draw_x_ticks(raster.decade_ticks(x_low, x_high))
    else:
        draw_x_ticks(raster.nice_ticks(min(all_x), max(all_x), 6))

    canvas.fill_rect(pad_left, pad_top, 1, plot_h, axis)
    canvas.fill_rect(pad_left, pad_top + plot_h, plot_w, 1, axis)

    palette = [
        (31, 119, 180),
        (214, 39, 40),
        (44, 160, 44),
        (255, 127, 14),
        (148, 103, 189),
        (140, 86, 75),
        (23, 190, 207),
        (127, 127, 127),
    ]
    for index, (name, points) in enumerate(curves[:8]):
        colour = palette[index % len(palette)]
        canvas.polyline([(px(x), py(y)) for x, y in points], colour, 2)

    if title:
        canvas.text(pad_left, 12, title, (20, 20, 20), 3)
    legend_y = 40 if title else 10
    legend_x = pad_left
    for index, (name, _points) in enumerate(curves[:8]):
        colour = palette[index % len(palette)]
        span = 28 + canvas.text_width(name, 2) + 18
        if legend_x > pad_left and legend_x + span > width - 12:
            legend_x = pad_left
            legend_y += 16
        canvas.line(legend_x, legend_y + 5, legend_x + 20, legend_y + 5, colour, 3)
        canvas.text(legend_x + 26, legend_y, name, label, 2)
        legend_x += span
    legend_bottom = legend_y + 16
    canvas.text_center(pad_left + plot_w / 2.0, height - 22, x_name + (" (LOG SCALE)" if log_x else ""), label, 2)

    payload = canvas.png()
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(payload)
    return {
        "path": path,
        "bytes": len(payload),
        "width": width,
        "height": height,
        "series": [name for name, _ in curves],
        "points": sum(len(points) for _, points in curves),
        "x_column": x_name,
        "log_x": bool(log_x),
        "plotname": parsed.get("plotname", ""),
    }


__all__ = [
    "SpiceError",
    "normalise_netlist",
    "normalise_commands",
    "build_command_file",
    "parse_raw",
    "sample_rows",
    "summarise",
    "write_csv",
    "write_plot_png",
    "_write_bar_png",
]
