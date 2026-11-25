"""Find the console's interface strings, and wrap the translated ones in ``tr``.

    python scripts/console_i18n.py list   # every literal that reads like interface text
    python scripts/console_i18n.py wrap   # wrap the literals the dictionary (tr.js) knows
    python scripts/console_i18n.py check  # exit 1 if a wrapped literal has no translation

A literal is wrapped where it stands: ``"Save"`` becomes ``tr("Save")`` and
`` `Remove ${x}?` `` becomes `` tr`Remove ${x}?` `` (key ``Remove {0}?``). Only
literals are ever translated, never a value that came from the server, so a
column called "Name" stays "Name".
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

CONSOLE = Path(__file__).resolve().parent.parent / "truejoin/adapters/inbound/http/console"
FILES = [
    "console.js",
    "results.js",
    "catalog.js",
    "ontology.js",
    "guide.js",
    "docs.js",
    "audit.js",
    "keys.js",
    "users.js",
    "databases.js",
    "settings.js",
    "carbon.js",
]
#: A "/" after one of these starts a regular expression, not a division.
_BEFORE_REGEX = set("(,=:[!&|?{};+-*%<>~^\n")


@dataclass
class Literal:
    start: int
    end: int
    key: str  # the text, with each ${...} of a template as {0}, {1}, ...
    template: bool
    wrapped: bool  # already tr("...") or tr`...`
    object_key: bool  # {"aria-label": ...}: a name, not text
    compared: bool = False  # x === "console": a value the code tests, not text
    children: list[Literal] = field(default_factory=list)


def scan(src: str) -> list[Literal]:
    """Every string and template literal of ``src``, nested ones included."""
    found: list[Literal] = []
    _code(src, 0, len(src), found, stop_at_brace=False)
    return found


def _prev_significant(src: str, i: int) -> str:
    i -= 1
    while i >= 0 and src[i] in " \t\r\n":
        i -= 1
    return src[i] if i >= 0 else "\n"


def _next_significant(src: str, i: int) -> str:
    while i < len(src) and src[i] in " \t\r\n":
        i += 1
    return src[i] if i < len(src) else ""


def _code(src: str, i: int, end: int, found: list[Literal], stop_at_brace: bool) -> int:
    depth = 0
    while i < end:
        c = src[i]
        two = src[i : i + 2]
        if two == "//":
            i = src.find("\n", i)
            i = end if i < 0 else i
        elif two == "/*":
            i = src.index("*/", i) + 2
        elif c == '"' or c == "'":
            j = i + 1
            while src[j] != c:
                j += 2 if src[j] == "\\" else 1
            j += 1
            if c == '"':
                found.append(_literal(src, i, j, json.loads(src[i:j]), False))
            i = j
        elif c == "`":
            i = _template(src, i, found)
        elif c == "/" and (
            _prev_significant(src, i) in _BEFORE_REGEX
            or src[max(0, i - 7) : i].rstrip().endswith("return")
        ):
            j = i + 1
            in_class = False
            while src[j] != "/" or in_class:
                if src[j] == "\\":
                    j += 1
                elif src[j] == "[":
                    in_class = True
                elif src[j] == "]":
                    in_class = False
                j += 1
            i = j + 1
        elif c == "{":
            depth += 1
            i += 1
        elif c == "}":
            if stop_at_brace and depth == 0:
                return i
            depth -= 1
            i += 1
        else:
            i += 1
    return i


def _template(src: str, start: int, found: list[Literal]) -> int:
    i = start + 1
    key: list[str] = []
    children: list[Literal] = []
    n = 0
    while src[i] != "`":
        if src[i] == "\\":
            key.append(src[i : i + 2])
            i += 2
        elif src[i : i + 2] == "${":
            i = _code(src, i + 2, len(src), children, stop_at_brace=True) + 1
            key.append("{" + str(n) + "}")
            n += 1
        else:
            key.append(src[i])
            i += 1
    literal = _literal(src, start, i + 1, _unescape("".join(key)), True)
    literal.children = children
    found.append(literal)
    found.extend(children)
    return i + 1


def _unescape(text: str) -> str:
    return text.replace("\\n", "\n").replace("\\`", "`").replace("\\$", "$").replace("\\\\", "\\")


def _literal(src: str, start: int, end: int, key: str, template: bool) -> Literal:
    before = src[:start].rstrip()
    # tr( or tr` as a name of its own: not the end of get(, fact( or createElement(.
    call = src[:start] if template else before
    wrapped = bool(re.search(r"(?<![\w$.])tr\($" if not template else r"(?<![\w$.])tr$", call))
    object_key = _next_significant(src, end) == ":" and _prev_significant(src, start) in "{,"
    compared = bool(re.search(r"[=!]==?\s*$", src[max(0, start - 6) : start])) or bool(
        re.match(r"\s*[=!]==?", src[end : end + 6])
    )
    return Literal(start, end, key, template, wrapped, object_key, compared)


def reads_like_text(key: str) -> bool:
    if not re.search(r"[A-Za-z]{2}", key):
        return False
    if " " not in key.strip() and re.fullmatch(r"[a-z0-9_.:#/\-\[\]=\"'{}*,>()]*", key):
        return False  # an identifier, a class, a selector, a path
    return not key.startswith(("/", "#", ".", "http", "truejoin-", "cds-", "input[", "M", "(max-"))


def dictionary() -> dict[str, str]:
    """The Turkish dictionary, read out of tr.js (a JSON object after ``const TR =``)."""
    text = (CONSOLE / "tr.js").read_text(encoding="utf-8")
    return json.loads(text[text.index("{") : text.rindex("}") + 1])


def wrap(src: str, known: set[str]) -> str:
    edits = []
    for lit in scan(src):
        if lit.wrapped or lit.object_key or lit.compared or lit.key not in known:
            continue
        edits.append(
            (lit.start, "tr" if lit.template else "tr(", lit.end, "" if lit.template else ")")
        )
    for start, before, end, after in sorted(edits, reverse=True):
        src = src[:end] + after + src[end:]
        src = src[:start] + before + src[start:]
    return src


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "list"
    if command == "list":
        seen: dict[str, None] = {}
        for name in FILES:
            for lit in scan((CONSOLE / name).read_text(encoding="utf-8")):
                if not lit.object_key and reads_like_text(lit.key):
                    seen.setdefault(lit.key)
        for key in sorted(seen, key=str.lower):
            print(json.dumps(key, ensure_ascii=False))
        return 0
    known = set(dictionary())
    if command == "wrap":
        for name in FILES:
            path = CONSOLE / name
            src = path.read_text(encoding="utf-8")
            path.write_text(wrap(src, known), encoding="utf-8")
        return 0
    missing = sorted(
        {
            lit.key
            for name in FILES
            for lit in scan((CONSOLE / name).read_text(encoding="utf-8"))
            if lit.wrapped and lit.key not in known
        }
    )
    for key in missing:
        print("no translation:", json.dumps(key, ensure_ascii=False))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
