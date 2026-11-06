# Third-party notices

This project is MIT licensed (see [LICENSE](LICENSE)). It bundles or derives
from the following third-party components.

## worker/bin/blast.c, worker/bin/blast.h

PKWare DCL ("implode") decompressor.

```
Copyright (C) 2003-2015 Mark Adler

This software is provided 'as-is', without any express or implied
warranty.  In no event will the authors be held liable for any damages
arising from the use of this software.

Permission is granted to anyone to use this software for any purpose,
including commercial applications, and to alter it and redistribute it
freely, subject to the following restrictions:

1. The origin of this software must not be misrepresented; you must not
   claim that you wrote the original software. If you use this software
   in a product, an acknowledgment in the product documentation would be
   appreciated but is not required.
2. Altered source versions must be plainly marked as such, and must not be
   misrepresented as being the original software.
3. This notice may not be removed or altered from any source distribution.

Mark Adler
madler@alumni.caltech.edu
```

Part of zlib <https://github.com/madler/zlib>, zlib license. The source is
shipped unchanged so `blast_harness.exe` can be rebuilt from this repository:

```
cl /nologo /O2 /Fe:blast_harness.exe blast_harness.c blast.c
```

## worker/bin/blast_harness.c

Thin command-line wrapper written for this project. MIT, same as the rest of
this repository.

## Not affiliated with National Instruments

Multisim, NI Circuit Design Suite and National Instruments are trademarks of
National Instruments Corporation (now part of Emerson Electric). They are used
here only to identify the product this tool interoperates with (nominative
use); no trademark right is claimed.

This project is not affiliated with, or endorsed by, National Instruments. It
drives a Multisim installation you already own, through the documented
Automation API and the Windows message API. It does not contain or
redistribute any National Instruments software, model library, database file or
license material, and it does not bypass any licensing, activation or
serial-number check. Using it requires your own valid Multisim licence.

## File format compatibility (worker/msim_ms14.py)

`worker/msim_ms14.py` reads the `.ms14` container so a design can be inspected
without starting Multisim.

The compression envelope it reverses is unencrypted and has no access control:
there is no technical protection measure being circumvented, and no licensing
check is involved. The decompression algorithm itself is PKWare DCL
("implode"), published and implemented independently by Mark Adler in
`blast.c` (above) — it is not a National Instruments algorithm.

Some component values are stored in the file as process-local pointer numbers
(`_uc<hex>`) and cannot be recovered at all, which is consistent with the format
being an internal serialisation rather than a documented interchange format.

This capability exists for interoperability. If your Multisim licence terms or
your jurisdiction restrict reverse engineering beyond what is needed for
interoperability, judge for yourself whether to use it; the other eight tools do
not depend on it.

## Local data directories

The tools touch the following locations on your machine and nothing else:

- `C:\Program Files (x86)\National Instruments\Circuit Design Suite 14.3\` —
  read only, to launch `multisim.exe`.
- `C:\ProgramData\National Instruments\Circuit Design Suite 14.3\database\` —
  opened by Multisim itself; this project does not write there directly.
- `%USERPROFILE%\Documents\National Instruments\Circuit Design Suite 14.3\` —
  designs you save or open. `msim_circuit({ action: 'save_as' })` overwrites an
  existing file at the target path without prompting, so do not point it at your
  only copy.
- `MSIM_MS14_WORKDIR`, else `%LOCALAPPDATA%\msim-ms14-tmp`, else a `_ms14_tmp`
  directory beside the worker — scratch space for decoding, first writable one
  wins, cleaned up after use.
