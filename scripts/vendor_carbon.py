"""Fetch Carbon web component entry bundles and every chunk they import, into one directory.

    python scripts/vendor_carbon.py truejoin/adapters/inbound/http/console/vendor/carbon \
        ui-shell button text-input textarea select checkbox radio-button tag notification modal toggle

The console loads the entries from /ui/vendor/carbon/<entry>.min.js; the chunks
are what they import. Remove the old files first when changing the version.
"""

import re
import sys
import urllib.request
from pathlib import Path

BASE = "https://1.www.s81c.com/common/carbon/web-components/version/v2.66.0/"
out = Path(sys.argv[1])
entries = sys.argv[2:]
out.mkdir(parents=True, exist_ok=True)
IMPORT = re.compile(r"""(?:from|import)\s*\(?\s*["'](\./[^"']+\.js)["']""")
todo, seen = [f"{e}.min.js" for e in entries], set()
while todo:
    name = todo.pop()
    if name in seen:
        continue
    seen.add(name)
    with urllib.request.urlopen(BASE + name, timeout=30) as r:
        text = r.read().decode("utf-8")
    (out / name).write_text(text, encoding="utf-8")
    todo += [m[2:] for m in IMPORT.findall(text) if m[2:] not in seen]
print(len(seen), "files", sum((out / n).stat().st_size for n in seen), "bytes")
