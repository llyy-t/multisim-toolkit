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
National Instruments Corporation.

This project is not affiliated with, or endorsed by, National Instruments. It
drives a Multisim installation you already own, through the documented
Automation API and the Windows message API. It does not contain or
redistribute any National Instruments software.
