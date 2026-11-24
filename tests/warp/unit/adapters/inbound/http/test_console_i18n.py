"""The console's Turkish dictionary covers what the scripts and the settings say."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[6] / "scripts"))

import console_i18n  # noqa: E402

from truejoin.domain import settings_catalog as catalog  # noqa: E402

WORDS = console_i18n.dictionary()


def _literals():
    for name in console_i18n.FILES:
        yield from console_i18n.scan((console_i18n.CONSOLE / name).read_text(encoding="utf-8"))


def test_every_wrapped_text_has_a_translation():
    missing = sorted({lit.key for lit in _literals() if lit.wrapped and lit.key not in WORDS})
    assert missing == []


def test_no_text_the_dictionary_knows_is_left_unwrapped():
    # A literal the code compares (row.source === "console") is a value, not text.
    left = sorted(
        {
            lit.key
            for lit in _literals()
            if lit.key in WORDS and not (lit.wrapped or lit.object_key or lit.compared)
        }
    )
    assert left == [], "run: python scripts/console_i18n.py wrap"


def test_a_translation_keeps_the_values_of_its_text():
    for english, turkish in WORDS.items():
        # A value may be dropped (Turkish has no plural "s"), never invented.
        assert set(re.findall(r"\{\d+\}", turkish)) <= set(re.findall(r"\{\d+\}", english)), english


def test_the_settings_sections_and_help_are_translated():
    said = {title for _, title in catalog.SECTIONS} | {catalog.STARTUP}
    said |= set(catalog.HELP.values()) | set(catalog.GROUP_HELP.values())
    said |= set(catalog._SLOT_HELP.values())
    assert sorted(said - set(WORDS)) == []


def test_the_settings_labels_and_groups_are_translated():
    from truejoin.application.settings import Settings
    from truejoin.application.settings_service import model_paths

    shown = [catalog.split_label(path) for path in model_paths(Settings)]
    said = {label for _, label in shown} | {group for group, _ in shown if group}
    assert sorted(said - set(WORDS)) == []


def test_no_word_of_the_dictionary_is_also_an_element_id():
    # `wrap` wraps every literal the dictionary knows, so a word like "key" would
    # turn $("key") into $(tr("key")). Short console words are capitalised instead.
    identifiers = sorted(word for word in WORDS if re.fullmatch(r"[a-z][a-z0-9_-]*", word))
    ids = set()
    for name in console_i18n.FILES:
        ids |= set(re.findall(r'\$\("([^"]+)"\)', (console_i18n.CONSOLE / name).read_text("utf-8")))
    assert sorted(set(identifiers) & ids) == []


def test_the_page_marks_its_own_text_and_the_dictionary_has_it():
    page = (console_i18n.CONSOLE / "index.html").read_text(encoding="utf-8")
    texts = re.findall(r"<[^>]*\bdata-t(?:\s|>)[^>]*>([^<]+)<", page)
    texts += re.findall(r'aria-label="([^"]+)"[^>]*data-t-label', page)
    texts += re.findall(r'placeholder="([^"]+)"[^>]*data-t-placeholder', page)
    assert len(texts) > 30
    assert sorted({text.strip() for text in texts if text.strip()} - set(WORDS)) == []


def test_the_scanner_reads_nested_templates_and_skips_comparisons():
    src = 'const a = x === "console" ? el("p", {}, `Remove ${b ? `the ${c}` : "it"}?`) : fact("Status");'
    found = {lit.key: lit for lit in console_i18n.scan(src)}
    assert found["console"].compared and not found["Status"].compared
    assert "Remove {0}?" in found and "the {0}" in found
    assert console_i18n.wrap(src, {"Status", "Remove {0}?", "console"}) == (
        'const a = x === "console" ? el("p", {}, tr`Remove ${b ? `the ${c}` : "it"}?`) : fact(tr("Status"));'
    )
