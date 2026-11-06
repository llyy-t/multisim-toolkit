#!/usr/bin/env python3
"""msim_ms14.py -- 离线读取 Multisim 的 .ms14 设计文件。

.ms14 是一个压缩容器，不是 XML：

    offset 0   36 字节  魔数 b"MSMCompressedElectronicsWorkbenchXML"
    offset 36  u64      解压后总长度（小端）
    之后循环   u32      本块解压后长度
               u32      本块压缩后长度
               bytes    该块数据（PKWare DCL "implode" 流）

数据本身用 PKWare DCL implode 压缩，zlib/bz2/lzma 都读不了，所以解压在
worker/bin/blast_harness.exe 里做（Mark Adler 的 blast.c 加薄壳，见该目录源码）。
纯 Python 的 pwexplode.py 也能解，但慢了三个数量级，只留作测试对照。

这里不做 COM、不看可见窗口，因此对"Multisim 打不开的设计"同样有效。
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

MAGIC = b"MSMCompressedElectronicsWorkbenchXML"
HEADER_LEN = len(MAGIC) + 8
MAX_TOTAL_BYTES = 512 * 1024 * 1024
DESIGN_SUFFIXES = (".ms14", ".msm", ".ewb", ".ms11", ".ms12", ".ms13")

HERE = os.path.dirname(os.path.abspath(__file__))
DECODER = os.path.join(HERE, "bin", "blast_harness.exe")

# Byte-order mark, stripped if a .ms14 ever starts with one (some editors save
# XML that way).  The real container does not carry one, so this is defensive.
_BOM = b"\xef\xbb\xbf"


class Ms14Error(Exception):
    """带错误码的失败（与 worker 的 OpError 形状一致）。"""

    def __init__(self, code: str, message: str, detail: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


# --------------------------------------------------------------------------
# 解压
# --------------------------------------------------------------------------


def _decoder() -> str:
    if os.path.isfile(DECODER):
        return DECODER
    found = shutil.which("blast_harness")
    if found:
        return found
    raise Ms14Error(
        "INTERNAL",
        "the .ms14 decompressor is missing",
        "expected %s; rebuild it from bin/blast_harness.c + bin/blast.c with "
        "cl /nologo /O2 /Fe:blast_harness.exe" % DECODER,
    )


def _decoder_can_write(directory: str) -> bool:
    """让 blast_harness.exe 亲自往这个目录写一次。

    这是唯一可信的判据。父进程能不能写不算数，别的子进程能不能写也不算数：
    在这个桌面上，用 Python 解释器起的子进程能写 `%TEMP%`，但 blast_harness
    这个独立的 exe 写同一个目录会被 CreateFile 拒绝（error 5）。两者的权限
    边界不同，所以只能用真正要干活的程序去试。

    内容故意写垃圾字节，`blast()` 必然失败 —— 我们只关心输出文件建不建得
    出来，所以看文件存在与否，不看返回码。
    """
    if not directory:
        return False
    if not os.path.isfile(DECODER):
        return False
    probe_in = os.path.join(directory, ".decoder_probe.in")
    probe_out = os.path.join(directory, ".decoder_probe.out")
    try:
        with open(probe_in, "wb") as handle:
            handle.write(b"\x00\x01\x02\x03")
        try:
            os.remove(probe_out)
        except OSError:
            pass
        try:
            subprocess.run(
                [DECODER, probe_in, probe_out],
                capture_output=True,
                timeout=30,
            )
        except Exception:
            return False
        return os.path.isfile(probe_out)
    except OSError:
        return False
    finally:
        for path in (probe_in, probe_out):
            try:
                os.remove(path)
            except OSError:
                pass


_WORK_ROOT_CACHE: Optional[str] = None


def _work_root() -> str:
    """挑一个 blast_harness.exe 真能写进去的目录。

    顺序是按"装在哪里"排的。插件形态下模块在 `node_modules` 里，那里不该被
    写；skill 形态下用户可能把仓库放在任何位置；而受限环境（企业安全软件、
    受限令牌、桌面沙箱）里，只有某些位置对独立 exe 可写。所以每个候选都必须
    由 `_decoder_can_write()` 用解压器本身验证过。

    结果缓存：一个进程只探测一次，之后所有解码复用同一个目录。
    """
    global _WORK_ROOT_CACHE
    if _WORK_ROOT_CACHE:
        return _WORK_ROOT_CACHE

    candidates: List[str] = []

    override = os.environ.get("MSIM_MS14_WORKDIR") or ""
    if override:
        candidates.append(override)

    local = os.environ.get("LOCALAPPDATA") or ""
    if local:
        candidates.append(os.path.join(local, "msim-ms14-tmp"))

    candidates.append(os.path.join(os.path.dirname(HERE), "_ms14_tmp"))
    candidates.append(tempfile.gettempdir())

    for candidate in candidates:
        try:
            os.makedirs(candidate, exist_ok=True)
        except OSError:
            continue
        if _decoder_can_write(candidate):
            _WORK_ROOT_CACHE = candidate
            return candidate

    # 没有一个候选可用。返回第一个建得出来的目录，让 _explode 抛出带
    # error 5 的原始信息 —— 那里会提示用户设 MSIM_MS14_WORKDIR。不在这里
    # 抛，是因为调用方可能只是想知道路径，不该因为探测失败就整体失败。
    for candidate in candidates:
        if os.path.isdir(candidate):
            _WORK_ROOT_CACHE = candidate
            return candidate
    _WORK_ROOT_CACHE = tempfile.gettempdir()
    return _WORK_ROOT_CACHE


def _explode(payload: bytes, work: str, index: int) -> bytes:
    """把一块 PKWare DCL 数据交给 blast_harness 解压。"""
    src = os.path.join(work, "block%d.in" % index)
    dst = os.path.join(work, "block%d.out" % index)
    with open(src, "wb") as handle:
        handle.write(payload)
    process = subprocess.run(
        [_decoder(), src, dst],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if process.returncode != 0 or not os.path.isfile(dst):
        detail = (process.stderr or process.stdout or "").strip()[:2000]
        if "(error 5)" in detail:
            detail += (
                "\n\nThe decompressor could not create its output file. "
                "Set MSIM_MS14_WORKDIR to a directory this process can write."
            )
        raise Ms14Error(
            "DECODE_ERROR",
            "PKWare block %d could not be decompressed" % index,
            detail,
        )
    with open(dst, "rb") as handle:
        return handle.read()


def decode(path: str) -> Dict[str, Any]:
    """解开一个 .ms14，返回 {path, xml, blocks, compressed_bytes, declared_length, workdir}。"""
    target = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isfile(target):
        raise Ms14Error("PATH_MISSING", "file not found: %s" % target)
    with open(target, "rb") as handle:
        blob = handle.read()
    if blob.startswith(_BOM):
        blob = blob[len(_BOM) :]
    if blob[: len(MAGIC)] != MAGIC:
        raise Ms14Error(
            "BAD_ARGS",
            "%s is not a Multisim design file" % os.path.basename(target),
            "first bytes were %r; a .ms14 starts with %r"
            % (blob[:16].hex(), MAGIC.decode("ascii")),
        )
    declared = struct.unpack_from("<Q", blob, len(MAGIC))[0]
    if declared <= 0 or declared > MAX_TOTAL_BYTES:
        raise Ms14Error(
            "DECODE_ERROR",
            "implausible uncompressed length %d" % declared,
            "the file may be truncated or not a .ms14",
        )

    work_root = _work_root()
    work = tempfile.mkdtemp(prefix="msim_ms14_", dir=work_root)
    try:
        chunks: List[bytes] = []
        position = HEADER_LEN
        index = 0
        while position < len(blob):
            if position + 8 > len(blob):
                raise Ms14Error(
                    "DECODE_ERROR",
                    "truncated block header at byte %d" % position,
                    "file is %d bytes" % len(blob),
                )
            declen, complen = struct.unpack_from("<II", blob, position)
            position += 8
            end = position + complen
            if end > len(blob):
                raise Ms14Error(
                    "DECODE_ERROR",
                    "block %d claims %d bytes but only %d remain" % (index, complen, len(blob) - position),
                    None,
                )
            data = _explode(blob[position:end], work, index)
            if len(data) != declen:
                raise Ms14Error(
                    "DECODE_ERROR",
                    "block %d decompressed to %d bytes, header said %d" % (index, len(data), declen),
                    "the stream is corrupt or uses a different DCL variant",
                )
            chunks.append(data)
            position = end
            index += 1
        if index == 0:
            raise Ms14Error("DECODE_ERROR", "the file contains no compressed blocks", None)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    xml = b"".join(chunks)
    if len(xml) != declared:
        raise Ms14Error(
            "DECODE_ERROR",
            "decoded %d bytes but the header declares %d" % (len(xml), declared),
            None,
        )
    if not xml.lstrip()[:1] == b"<":
        raise Ms14Error(
            "DECODE_ERROR",
            "the decoded payload is not XML",
            "first bytes: %r" % xml[:32],
        )
    return {
        "path": target,
        "compressed_bytes": len(blob),
        "declared_length": declared,
        "decoded_bytes": len(xml),
        "blocks": index,
        "workdir": work_root,
        "xml": xml,
    }


# --------------------------------------------------------------------------
# 结构摘要
# --------------------------------------------------------------------------

_REFDES_UNIT = re.compile(r"!0?([A-Z]{1,3}\d{1,4})!0")
_REFDES_PLAIN = re.compile(r"^(?:&amp;)?(?:UNI|ASC)?!.*?([A-Z]{1,3}\d{1,4})!?$")


def _human_name(raw: str) -> Optional[str]:
    """从 `&UNI!0!0!0XLA1!0C:\\...\\_uc....ms14!01!0_uc...!0` 里抠出 XLA1。"""
    if not raw:
        return None
    text = raw.replace("&amp;", "&")
    match = _REFDES_UNIT.search(text)
    if match:
        return match.group(1)
    match = re.search(r"\b([A-Z]{1,3}\d{1,4})\b", text)
    return match.group(1) if match else None


def _attributes(element: ET.Element) -> Dict[str, str]:
    return {key: (value or "") for key, value in element.attrib.items()}


def _walk(element: ET.Element):
    yield element
    for child in element:
        yield from _walk(child)


def _asc(text: str) -> str:
    """Multisim 把文本存成 `&ASC<字面> <修饰>`，如 `&ASC&s` / `&ASC&d`。"""
    value = (text or "").strip()
    if value.startswith("&ASC"):
        return value[4:]
    return value


_INTERNAL_CODE = re.compile(r"&?UNI\d*_uc[0-9a-f]+")
_UC_TAIL = re.compile(r"_uc[0-9a-f]+")
_HAS_LETTER = re.compile(r"[A-Za-z]")


def _human_value(text: str) -> Optional[str]:
    """把显示文本还原成人能读的形式；判不出来时返回 None。

    两种前缀都要处理：

    - `&ASC<文本>` —— 直接就是文本。
    - `&UNI<文本>_uc<hex>` —— 尾部是 Multisim 的单位/格式指针（不可逆），
      去掉后剩下的 `<文本>` 才是值。但 `<文本>` 也可能为空
      （`&UNI_uc103a9` 表示"用默认格式显示"），此时无法还原。
    """
    raw = (text or "").replace("&amp;", "&").strip()
    if not raw:
        return None
    if raw.startswith("&UNI"):
        body = raw[4:]
        code = _UC_TAIL.search(body)
        if code:
            body = body[: code.start()] + body[code.end():]
        body = body.replace("\\n", " ").replace("\r", " ").replace("\n", " ")
        body = re.sub(r"\s{2,}", " ", body).strip()
        return body or None
    if raw.startswith("&ASC"):
        raw = raw[4:]
    raw = _INTERNAL_CODE.sub("", raw)
    raw = raw.replace("\\n", " ").replace("\r", " ").replace("\n", " ")
    raw = re.sub(r"\s{2,}", " ", raw).strip()
    return raw or None


def _looks_encoded(text: Optional[str]) -> bool:
    """值里只剩 `&UNI`/`_uc` 这类内部标记时，说明它没被还原成可读文本。"""
    if not text:
        return True
    lowered = text.lower()
    return "&uni" in lowered or "_uc" in lowered or "&asc" in lowered


def _param_values(root: ET.Element) -> Dict[str, List[str]]:
    """位号 -> CiaParamList.parameters 里的可读值列表。

    `CiaParamList` 有两条平行数组：`<doubles>`（历史遗留的初值）和 `<parameters>`
    （真正用于显示的字符串）。二者可能不一致（实测 R11 的 double 仍是 1000，而
    parameters 已经从 `1k` 改成 `2.2k`），**要以 parameters 为准**。
    """
    values: Dict[str, List[str]] = {}
    # CiComponent 定义体全是平铺的兄弟节点，需要知道"当前正在解析哪个位号"。
    current: Optional[str] = None
    for element in _walk(root):
        if element.tag == "CiComponent":
            current = _asc(element.attrib.get("LocalName") or "").replace("&", "").strip() or None
            continue
        if element.tag == "CiaParamList" and current:
            bucket: List[str] = []
            for params in element.findall("parameters"):
                for item in params.findall("Item"):
                    text = _asc(item.attrib.get("Value") or "").strip()
                    if text:
                        bucket.append(text)
            if bucket:
                values.setdefault(current, bucket)
    return values


def _primary_param(params: List[str]) -> Optional[str]:
    """从参数数组里挑出"真正的值"。

    Multisim 把整个参数表按位置存下来，绝大多数位置是 `0` / `27`（TNOM）之类的默认值。
    器件的值通常是**唯一非零的那个**；有多个非零值时取第一个（例如源的
    `1` 与 `5k` 会同时非零，第一位是幅度）。
    """
    numeric: List[tuple] = []
    for index, text in enumerate(params):
        try:
            number = float(text)
        except ValueError:
            continue
        if number != 0.0:
            numeric.append((index, text))
    if numeric:
        return numeric[0][1]
    return None


_INSTRUMENT_KINDS = {
    "XMM": "multimeter",
    "XSC": "oscilloscope",
    "XFG": "function generator",
    "XWM": "wattmeter",
    "XBP": "bode plotter",
    "XFC": "frequency counter",
    "XWG": "word generator",
    "XLC": "logic converter",
    "XLA": "logic analyzer",
    "XIV": "IV analyzer",
    "XDA": "distortion analyzer",
    "XSA": "spectrum analyzer",
    "XNA": "network analyzer",
    "XCP": "current probe",
}


def _instrument_kind(refdes: str) -> Optional[str]:
    match = re.match(r"(X[A-Z]{2})", refdes.upper())
    return _INSTRUMENT_KINDS.get(match.group(1)) if match else None


def _instruments(root: ET.Element) -> List[Dict[str, Any]]:
    """抽仪器：`CompLongName="&UNIXLA1#1/_uc<hex>:"` 给出位号，属性表给出设置。"""
    found: Dict[str, Dict[str, Any]] = {}
    node_class = root.find(".//InstrumentsData")
    if node_class is None:
        return []
    for node in node_class.iter("CSourceSymbolCollectNode"):
        long_name = _asc(node.attrib.get("CompLongName") or "")
        match = re.search(r"UNI(X[A-Z]{2}\d+)", long_name)
        if not match:
            continue
        refdes = match.group(1)
        entry = found.setdefault(
            refdes,
            {"refdes": refdes, "kind": _instrument_kind(refdes), "settings": {}},
        )
        for element in node.iter("Element"):
            key = _asc(element.attrib.get("Key") or "")
            if not key.startswith("NI_EWB_"):
                continue
            data = element.find("CDataElement")
            if data is None:
                continue
            name = key[len("NI_EWB_"):]
            # 只留能读懂、且不含大数组的设置项
            if "PATTERNS" in name or "COLORS" in name or "TRACEDATAS" in name:
                continue
            raw = data.attrib.get("Data")
            if raw is not None and len(raw) <= 64:
                entry["settings"][name] = raw
    return sorted(found.values(), key=lambda item: (_refdes_key(item["refdes"]), item["refdes"]))


def _components(root: ET.Element) -> List[Dict[str, Any]]:
    """按 CIITSymbolComp 抽出器件：位号来自子节点 CIITSymTextCompName 的 Output。"""
    params = _param_values(root)
    found: Dict[str, Dict[str, Any]] = {}
    for symbol in root.iter("CIITSymbolComp"):
        refdes = None
        value = None
        model = None
        for child in symbol.iter():
            tag = child.tag
            output = child.attrib.get("Output") or ""
            if tag == "CIITSymTextCompName" and output:
                refdes = _asc(output).replace("&", "").strip()
            elif tag == "CIITSymTextCompValue" and output:
                value = _human_value(output)
            elif tag == "CIITSymTextCompModel" and output:
                model = _human_value(output)
        if not refdes:
            continue
        entry = found.setdefault(
            refdes,
            {
                "refdes": refdes,
                "class": symbol.attrib.get("Class"),
                "type": symbol.attrib.get("Type") or None,
                "sections": int(symbol.attrib.get("NumSections") or 1),
                "value": None,
                "model": None,
                "parameters": [],
            },
        )
        if value and not entry["value"]:
            entry["value"] = value
        if model and not entry["model"]:
            entry["model"] = model

    # 用 CiaParamList 校正/补齐值。symbol 上的 Output 只是"显示文本"，
    # 值被改过之后它可能残留旧内容；parameters 才是权威来源。
    for refdes, entry in found.items():
        bucket = params.get(refdes) or []
        if not bucket:
            continue
        entry["parameters"] = bucket
        primary = _primary_param(bucket)
        current = entry.get("value")
        if primary and _looks_encoded(current):
            entry["value"] = primary
    return sorted(found.values(), key=lambda item: (_refdes_key(item["refdes"]), item["refdes"]))


def _refdes_key(name: str):
    match = re.match(r"([A-Za-z_]+)(\d+)", name)
    if not match:
        return (name, 0)
    return (match.group(1), int(match.group(2)))


def inspect(xml_bytes: bytes) -> Dict[str, Any]:
    """从解压后的 XML 里抽出可读的结构：版本、器件、仪器、计数。"""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise Ms14Error("DECODE_ERROR", "the decoded XML could not be parsed", str(exc)) from exc

    document: Dict[str, Any] = {
        "root": root.tag,
        "schema": root.attrib.get("Schema"),
        "creator": {},
        "counts": {},
    }
    for element in root.iter():
        if element.tag == "history":
            for child in element:
                if child.tag == "creator":
                    document["creator"] = {
                        key: child.attrib.get(key)
                        for key in ("application", "date", "type", "versionMajor", "versionMinor", "versionPatch")
                    }
                    break
            break

    tags: Dict[str, int] = {}
    classes: Dict[str, int] = {}

    for element in _walk(root):
        tags[element.tag] = tags.get(element.tag, 0) + 1
        kind = element.attrib.get("Class")
        if kind:
            classes[kind] = classes.get(kind, 0) + 1

    components = _components(root)
    instruments = _instruments(root)
    # 仪器在原理图里也是 CIITSymbolComp（位号 X…），但真正的仪器对象在 <InstrumentsData>。
    # 两处合并成一张表，值/类别以 <InstrumentsData> 为准。
    by_refdes = {item["refdes"]: item for item in components}
    for instrument in instruments:
        symbol = by_refdes.get(instrument["refdes"])
        if symbol is not None:
            symbol["kind"] = instrument["kind"]
            symbol["settings"] = instrument["settings"]

    document["components"] = components
    document["instruments"] = instruments
    document["instrument_count"] = len(instruments)
    placed = sum(1 for item in components if not _instrument_kind(item["refdes"]))
    document["counts"] = {
        "items": tags.get("Item", 0),
        "wires": classes.get("CODLineComp", 0),
        "symbols": sum(count for kind, count in classes.items() if "SymbolComp" in kind),
        "pins": classes.get("CIITPinSymbolComp", 0),
        "parts": placed,
        "instruments": len(instruments),
    }
    document["top_tags"] = dict(sorted(tags.items(), key=lambda pair: -pair[1])[:15])
    document["top_classes"] = dict(sorted(classes.items(), key=lambda pair: -pair[1])[:15])
    return document


def summary(path: str, include_xml: bool = False, xml_limit: int = 200000) -> Dict[str, Any]:
    """解码 + 摘要，供 msim_design 工具直接返回。"""
    decoded = decode(path)
    document = inspect(decoded["xml"])
    payload: Dict[str, Any] = {
        "path": decoded["path"],
        "compressed_bytes": decoded["compressed_bytes"],
        "decoded_bytes": decoded["decoded_bytes"],
        "blocks": decoded["blocks"],
        "declared_length_ok": decoded["declared_length"] == decoded["decoded_bytes"],
        **document,
    }
    if include_xml:
        text = decoded["xml"].decode("utf-8", errors="replace")
        payload["xml"] = text[:xml_limit]
        payload["xml_truncated"] = len(text) > xml_limit
        payload["xml_chars"] = len(text)
    return payload


__all__ = ["Ms14Error", "decode", "inspect", "summary", "MAGIC", "HEADER_LEN", "DESIGN_SUFFIXES"]
