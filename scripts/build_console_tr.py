"""Write console/tr.js from the translation parts in scripts/console_tr.

    python scripts/build_console_tr.py scripts/console_tr

The settings' help is keyed by path in tr_c and written here under its English
text, so the dictionary follows the wording in ``domain/settings_catalog.py``.
Their labels and groups are keyed by the English ``split_label`` writes, and
the build fails when a setting's label or group has no Turkish.
"""

import json
import runpy
import sys
from pathlib import Path

from truejoin.application.settings import Settings
from truejoin.application.settings_service import model_paths
from truejoin.domain import settings_catalog as catalog

parts = Path(sys.argv[1])
a = runpy.run_path(str(parts / "tr_a.py"))["A"]
b = runpy.run_path(str(parts / "tr_b.py"))["B"]
c = runpy.run_path(str(parts / "tr_c.py"))
d = runpy.run_path(str(parts / "tr_d.py"))["D"]
# The guide's words are its own: one that tr_a or tr_b already has would be said twice.
assert not set(d) & (set(a) | set(b)), sorted(set(d) & (set(a) | set(b)))
words = {**a, **b, **c["SECTIONS"], **d}
for path, text in c["HELP"].items():
    words[catalog.HELP[path]] = text
for field, text in c["SLOT"].items():
    words[catalog._SLOT_HELP[field]] = text
for prefix, text in c["GROUPS"].items():
    words[catalog.GROUP_HELP[prefix]] = text
missing = (set(catalog.HELP) - set(c["HELP"])) | (set(catalog._SLOT_HELP) - set(c["SLOT"]))
assert not missing, missing
# Every label and group the Settings tab shows, as split_label writes them.
shown = [catalog.split_label(path) for path in model_paths(Settings)]
labels, groups = {label for _, label in shown}, {group for group, _ in shown if group}
unsaid = (labels - set(c["LABELS"])) | (groups - set(c["GROUP_NAMES"]))
assert not unsaid, sorted(unsaid)
words |= {**c["LABELS"], **c["GROUP_NAMES"]}
out = Path(__file__).resolve().parent.parent / "truejoin/adapters/inbound/http/console/tr.js"
body = json.dumps(
    dict(sorted(words.items(), key=lambda kv: kv[0].lower())), ensure_ascii=False, indent=1
)
out.write_text(
    '"use strict";\n// The console in Turkish: English text -> Turkish. Written by scripts/build_console_tr.py;\n'
    "// a missing entry shows the English. Values from the server are never looked up here.\n"
    f"const TR = {body};\n",
    encoding="utf-8",
)
print(len(words), "entries")
