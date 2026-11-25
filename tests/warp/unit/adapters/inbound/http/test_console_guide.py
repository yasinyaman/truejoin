"""The console's user guide (console/guide.js) says what the screens say.

The guide is prose, so most of it cannot be tested. What can: a label it puts
in bold is a label a screen shows, in both languages; an address it links to is
a place the console has; every screen has a section. Rename a button, move a
page or add a screen, and one of these fails until the guide is brought along.
"""

import re
import sys
from collections import Counter
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[6] / "scripts"))

import console_i18n  # noqa: E402

from truejoin.adapters.inbound.http.app import create_app  # noqa: E402
from truejoin.application.settings import RuntimeEnv  # noqa: E402

CONSOLE = console_i18n.CONSOLE
GUIDE = (CONSOLE / "guide.js").read_text(encoding="utf-8")
WORDS = console_i18n.dictionary()
TEXTS = [lit.key for lit in console_i18n.scan(GUIDE) if lit.wrapped]

BOLD = re.compile(r"\*\*(.+?)\*\*")
LINK = re.compile(r"\[[^\]]+\]\((#[^)\s]+)\)")
#: A label that reads the same in every language, so the dictionary has no entry for it.
SAME_IN_BOTH = {"SQL"}
#: What /ui/access answers with, and "manage": any page of Administration.
NEEDS = {"read", "query", "update", "create", "audit", "admin", "manage"}
ADMIN_PAGES = ("databases", "keys", "users", "audit", "settings")


def _labels() -> dict[str, str]:
    """Each label the console shows, with its Turkish: a text of the dictionary,
    without the space around it or the count after it ("All {0}")."""
    labels: dict[str, str] = {}
    for english, turkish in WORDS.items():
        labels.setdefault(english.strip(), turkish.strip())
        if english.endswith(" {0}") and turkish.endswith(" {0}"):
            labels.setdefault(english[:-4].strip(), turkish[:-4].strip())
    return labels


def _pages() -> set[str]:
    scripts = "\n".join(
        (CONSOLE / name).read_text(encoding="utf-8")
        for name in console_i18n.FILES
        if name.endswith(".js")
    )
    return set(re.findall(r'^page\("(\w+)"', scripts, flags=re.M))


def test_the_guide_is_served_loaded_before_docs_and_under_the_i18n_checks():
    client = TestClient(create_app(env=RuntimeEnv()))
    assert client.get("/ui/guide.js").status_code == 200
    page = client.get("/ui").text
    assert page.index('src="/ui/guide.js"') < page.index('src="/ui/docs.js"')
    assert "guide.js" in console_i18n.FILES
    # Docs opens on the guide's first chapter, and a chapter's sections have addresses.
    docs = client.get("/ui/docs.js").text
    assert 'page: "guide-start"' in docs
    assert 'href: hashOf("docs", c.id, x.id)' in docs
    assert "open([section, part])" in docs


def test_the_guide_has_text_and_every_text_has_its_turkish():
    assert len(TEXTS) > 100
    assert [text for text in TEXTS if text not in WORDS] == []


def test_a_label_in_bold_is_a_label_a_screen_shows_in_both_languages():
    labels = _labels()
    said = 0
    for text in TEXTS:
        english = BOLD.findall(text)
        unknown = [label for label in english if label not in labels and label not in SAME_IN_BOTH]
        assert unknown == [], f"{unknown} in bold, but no screen says that: {text}"
        # The Turkish text names the same labels, as the Turkish screens spell them.
        expected = Counter(labels.get(label, label) for label in english)
        assert Counter(BOLD.findall(WORDS[text])) == expected, text
        said += len(english)
    assert said > 60


def test_an_address_the_guide_links_to_is_a_place_and_the_same_in_both_languages():
    chapters = set(re.findall(r'id: "(guide-[\w-]+)"', GUIDE))
    places = {"#query", "#catalog", "#admin", "#docs", "#docs/mcp-connect", "#docs/mcp-tools"}
    places |= {f"#admin/{name}" for name in ADMIN_PAGES} | {f"#docs/{c}" for c in chapters}
    linked = 0
    for text in TEXTS:
        addresses = LINK.findall(text)
        assert set(addresses) <= places, text
        assert Counter(LINK.findall(WORDS[text])) == Counter(addresses), text
        linked += len(addresses)
    assert linked > 10


def test_every_screen_has_a_section_of_the_guide():
    covered = set(re.findall(r'covers: "(\w+)"', GUIDE))
    assert covered == _pages() == {"query", "catalog", "docs", *ADMIN_PAGES}


def test_chapters_and_sections_are_named_once_and_ask_for_a_known_permission():
    chapter, chapters, seen = None, [], set()
    for name in re.findall(r'\bid: "([\w-]+)"', GUIDE):
        if name.startswith("guide-"):
            chapter = name
            chapters.append(name)
        assert (chapter, name) not in seen, (chapter, name)
        seen.add((chapter, name))
    assert chapters == [
        "guide-start",
        "guide-query",
        "guide-catalog",
        "guide-admin",
        "guide-agents",
    ]
    assert set(re.findall(r'needs: "(\w+)"', GUIDE)) <= NEEDS
    # Administration's sections are its pages, each behind its page's permission.
    for name in ADMIN_PAGES:
        needs = "audit" if name == "audit" else "admin"
        assert re.search(
            rf'id: "{name}",\s+title: [^\n]+\n\s+needs: "{needs}",\s+covers: "{name}"', GUIDE
        )


def test_a_text_is_shown_without_inner_html_and_links_stay_inside_the_console():
    assert "innerHTML" not in GUIDE
    # Only an address of this console becomes a link: "#…", never a URL.
    assert r"\[([^\]]+)\]\((#[^)\s]+)\)" in GUIDE
    assert not re.search(r"\]\((?!#)", "\n".join(TEXTS))
