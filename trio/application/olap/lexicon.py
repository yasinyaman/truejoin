"""Turning a question into a metric request by matching words, with no model.

Everything in the semantic path is deterministic except one step: turning a
sentence into a choice of measure, dimension and filter. This module does
that step with a vocabulary instead of a language model: the certified terms
of the catalog, which a model wrote once per table and a reviewer signed off.

It can only name measures, dimensions and values the semantic model has, so
it cannot invent a column. A dimension or a condition that belongs to a table
the measured one references is written ``table.column``; the join itself is
left to the keys. The audit trail is a list of matched words, and the same
question gets the same answer every time.

What it cannot do is generalise: a word nobody put in the vocabulary matches
nothing. The benchmark in ``bench/`` measures what that costs.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from trio.domain.catalog.terms import Term
from trio.domain.olap.metric_dsl import parse_metric

#: Turkish is written with characters the data does not always use — the
#: fixture stores ``basarili`` while a question says ``başarılı`` — so every
#: comparison happens on a folded form. ``ı`` and ``İ`` do not fold the way
#: ``str.lower`` and NFKD expect, hence the explicit pairs.
_FOLD = str.maketrans(
    {
        "ı": "i",
        "İ": "i",
        "ş": "s",
        "Ş": "s",
        "ğ": "g",
        "Ğ": "g",
        "ü": "u",
        "Ü": "u",
        "ö": "o",
        "Ö": "o",
        "ç": "c",
        "Ç": "c",
    }
)


def fold(text: str) -> str:
    """A comparable form: lower case, no diacritics, Turkish pairs handled."""
    folded = text.translate(_FOLD).lower()
    return "".join(c for c in unicodedata.normalize("NFKD", folded) if not unicodedata.combining(c))


def expand(text: str, glossary: Mapping[str, str]) -> tuple[str, list[str]]:
    """``text`` folded, with each abbreviation of ``glossary`` written out in full.

    Whole words only (``TL`` in ``TL ciro`` but not in ``ATLAS``), the longest
    first so ``KDV dahil`` wins over ``KDV``. Returns what was expanded, as
    ``short -> long``, for the reader of the answer.
    """
    folded = fold(text)
    used = []
    for short in sorted(glossary, key=lambda s: -len(s)):
        long, written = glossary[short], fold(glossary[short])
        pattern = re.compile(rf"(?<!\w){re.escape(fold(short))}(?!\w)")
        if fold(short) and pattern.search(folded):
            # Backslashes doubled, so the long form is not read as a group reference.
            folded = pattern.sub(written.replace("\\", "\\\\"), folded)
            used.append(f"{short} -> {long}")
    return folded, used


#: Words that name an aggregation, most specific first — ``en yuksek`` has to
#: beat ``yuksek``, and ``kac farkli`` has to beat ``kac``.
AGGREGATIONS: tuple[tuple[str, str], ...] = (
    ("count_distinct", "kac farkli"),
    ("count_distinct", "farkli"),
    ("count_distinct", "distinct"),
    ("count_distinct", "unique"),
    ("count_distinct", "benzersiz"),
    ("max", "en yuksek"),
    ("max", "en buyuk"),
    ("max", "en fazla"),
    ("max", "highest"),
    ("max", "largest"),
    ("max", "maximum"),
    ("max", "max"),
    ("min", "en dusuk"),
    ("min", "en kucuk"),
    ("min", "en az"),
    ("min", "lowest"),
    ("min", "smallest"),
    ("min", "minimum"),
    ("min", "min"),
    ("median", "medyan"),
    ("median", "median"),
    ("median", "ortanca"),
    ("avg", "ortalama"),
    ("avg", "average"),
    ("avg", "mean"),
    ("avg", "avg"),
    ("sum", "toplam tutar"),
    ("sum", "total amount"),
    ("sum", "toplami"),
    ("sum", "toplam"),
    ("sum", "total"),
    ("sum", "sum"),
    ("count", "kac adet"),
    ("count", "kac tane"),
    ("count", "sayisi"),
    ("count", "sayi"),
    ("count", "adedi"),
    ("count", "kac"),
    ("count", "how many"),
    ("count", "number of"),
    ("count", "count"),
)

#: Words that name a transform.
TRANSFORMS: tuple[tuple[str, str], ...] = (
    ("change_pct", "yuzde degisim"),
    ("change_pct", "yuzdesel degisim"),
    ("change_pct", "percentage change"),
    ("change_pct", "percent change"),
    ("change_pct", "month-over-month"),
    ("change_pct", "onceki aya gore"),
    ("cumsum", "kumulatif"),
    ("cumsum", "cumulative"),
    ("cumsum", "running total"),
    ("cumsum", "birikimli"),
    ("time_shift", "gecen yil ayni"),
    ("time_shift", "same period last"),
    ("rank", "siralama"),
    ("rank", "ranking"),
)

#: Words that name a time grain.
GRAINS: tuple[tuple[str, str], ...] = (
    ("month", "aylik"),
    ("month", "ay bazinda"),
    ("month", "monthly"),
    ("month", "per month"),
    ("month", "month"),
    ("month", "aya gore"),
    ("year", "yillik"),
    ("year", "yearly"),
    ("year", "annual"),
    ("year", "per year"),
    ("year", "year"),
    ("quarter", "ceyrek"),
    ("quarter", "quarterly"),
    ("quarter", "quarter"),
    ("week", "haftalik"),
    ("week", "weekly"),
    ("week", "week"),
    ("day", "gunluk"),
    ("day", "daily"),
    ("day", "gun bazinda"),
    ("day", "per day"),
)

#: Phrasings that introduce a breakdown. What follows one of these is a
#: dimension; ``X bazinda`` puts the dimension *before* the marker, which is
#: why both sides get searched.
BREAKDOWN = ("bazinda", "gore", "basina", "per ", "by ", "for each", "her ")


def _first_match(text: str, table: Sequence[tuple[str, str]]) -> str:
    """The value whose phrase appears in the text, longest phrase first."""
    for value, phrase in sorted(table, key=lambda pair: -len(pair[1])):
        if phrase in text:
            return value
    return ""


def _names_for(name: str, extra: Sequence[str] | None = None, owner: str = "") -> tuple[str, ...]:
    """A column's own name, its parts, and the words mapped to it.

    A name or a part that only repeats the table's name is left out:
    ``islem_turu`` in ``islemler`` is recognised by "tur", not by "islem",
    which every question about that table says anyway.
    """
    folded = fold(name)
    names = [folded, *(part for part in folded.split("_") if len(part) > 2)]
    if owner:
        own = _stem(fold(owner))
        names = [candidate for candidate in names if not candidate.startswith(own)]
    return (*names, *(fold(w) for w in (extra or ())))


#: How much of a word has to agree before it counts as the same word.
#: Turkish inflects in both directions around a schema name — a question says
#: ``tutarı`` where the column is ``tutar``, and ``müşteri`` where the table is
#: ``musteriler`` — so neither string reliably contains the other and matching
#: happens on a shared stem.
STEM = 5


def _stem(word: str) -> str:
    return word[:STEM] if len(word) > STEM else word


def _agreement(candidate: str, text: str) -> int:
    """How many characters the text and this name actually agree on.

    The stem is there to survive inflection, not to stand in for the whole
    name. Scoring a stem hit by the *candidate's* length was the bug: with
    ``STEM = 5`` the bare word "islem" matched ``islem_turu`` and ``islem_tarihi``
    and was credited 10 and 12, so "Toplam islem tutari" ranked two columns it
    never mentioned above ``tutar``, which it did. Measuring the shared prefix
    instead gives all three 5, which is the truth: one word was recognised.

    A name of several words counts only when every one of them is there, and
    then for all of them: "müşteri işlemleri" is not named by "müşteri sayısı",
    which scored it on its first word and so counted transactions when the
    question counted customers.
    """
    words = candidate.split()
    if len(words) > 1:
        each = [_agreement(word, text) for word in words if len(word) > 2]
        return sum(each) if each and all(each) else 0
    best = 0
    for found in re.finditer(rf"\b{re.escape(_stem(candidate))}\w*", text):
        word = found.group(0)
        shared = 0
        for mine, theirs in zip(candidate, word, strict=False):
            if mine != theirs:
                break
            shared += 1
        best = max(best, shared)
    return best


def _mentions(text: str, name: str, extra: Sequence[str] | None = None, owner: str = "") -> int:
    """How strongly the text names this column (of table ``owner``) or table."""
    best = 0
    for candidate in _names_for(name, extra, owner):
        if len(candidate) < 3:
            continue
        best = max(best, _agreement(candidate, text))
    return best


#: Aggregations that need a number to work on.
_NUMERIC_AGGS = frozenset({"sum", "avg", "median", "max", "min"})

#: What a count counts: "işlem sayısı", "kaç müşteri", "how many customers".
#: A distinct count is left out on purpose: there the noun is a column.
_COUNTED = (
    re.compile(r"\b(\w+) (?:sayisi|adedi)\b"),
    re.compile(r"\b(?:kac|how many|number of) (?!farkli|distinct|different|unique)(\w+)"),
)


def _counted(text: str) -> str:
    """The noun a count is of, or nothing."""
    for pattern in _COUNTED:
        found = pattern.search(text)
        if found:
            return found.group(1)
    return ""


class LexiconPlanner:
    """Turns a question into a semantic request by matching words.

    Nothing here can name a measure, dimension or value that the semantic
    model does not have: the vocabulary only attaches words to those.
    """

    def __init__(
        self,
        semantic: Any,
        tables: Sequence[str],
        synonyms: Mapping[str, Sequence[str]] | None = None,
        value_synonyms: Mapping[str, Sequence[str]] | None = None,
        values: Mapping[str, Mapping[str, Sequence[Any]]] | None = None,
        parents: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        """
        Args:
            synonyms: The words for each column and table name.
            value_synonyms: The same, per stored value (folded).
            values: ``table -> column -> stored values``: what a condition
                may compare against.
            parents: ``table -> the tables it references``, nearest first. A
                breakdown or a condition may come from one of those, written
                ``table.column``; the keys decide the join.
        """
        # Normalised on the way in, because a vocabulary is something a model
        # wrote and a person may edit: entries arrive with leading spaces,
        # accents, or as the name again.
        self._synonyms = _clean(synonyms or {})
        self._value_synonyms = _clean(value_synonyms or {})
        self._tables = list(tables)
        self._models = {table: semantic.model_for(table) for table in tables}
        self._filter_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._values = {
            table: {column: list(found) for column, found in (values or {}).get(table, {}).items()}
            for table in self._tables
        }
        self._parents = {
            table: [parent for parent in (parents or {}).get(table, ()) if parent in self._models]
            for table in self._tables
        }

    def _mentions(self, text: str, name: str, owner: str = "") -> int:
        """``_mentions`` with this planner's vocabulary applied."""
        return _mentions(text, name, self._synonyms.get(name), _bare(owner))

    def _columns(self, table: str) -> set[str]:
        model = self._models[table]
        return {m.name for m in model.measures} | {d.name for d in model.dimensions}

    # -- the parts of a question -------------------------------------------

    def _filters_for(self, table: str, text: str, prefix: str = "") -> list[dict[str, Any]]:
        """Conditions read straight out of the question's own words.

        The stored values are what makes this work without a model: the
        question says "başarılı", the column stores ``basarili``, and folding
        both makes that the same word. A value that is also a common word is
        skipped: matching every sentence containing "web" would be worse
        than matching none.
        """
        found = []
        for column, values in self._values[table].items():
            for value in values:
                folded = fold(str(value))
                if len(folded) < 4:
                    continue
                words = (folded, *(fold(w) for w in self._value_synonyms.get(folded, ())))
                if any(_value_asked_for(text, word) for word in words):
                    found.append({"column": prefix + column, "op": "eq", "value": value})
                    break
        return found

    def _conditions(self, table: str, text: str) -> list[dict[str, Any]]:
        """The conditions on ``table`` and on the tables it references.

        Memoised for the question being planned: ``plan`` reaches it several
        times for the same pair, and the answer depends on nothing else. A
        value matched on the table itself is not matched again further up: a
        key holds the same values on both sides of its join.
        """
        cached = self._filter_cache.get((table, text))
        if cached is None:
            cached = self._filters_for(table, text)
            matched = {str(condition["value"]) for condition in cached}
            for parent in self._parents[table]:
                for condition in self._filters_for(parent, text, prefix=f"{_bare(parent)}."):
                    if str(condition["value"]) not in matched:
                        matched.add(str(condition["value"]))
                        cached.append(condition)
            self._filter_cache[(table, text)] = cached
        return cached

    def _score(self, table: str, text: str) -> int:
        """How much of the question this table can account for.

        The table's own name counts: "kaç müşteri var" names no column at all,
        and without this the question would be answered against whichever
        table happened to be first. So does what it can reach: a question
        about transactions per segment is about the transactions.
        """
        own = self._columns(table)
        score = sum(self._mentions(text, name, table) for name in own)
        score += 3 * self._mentions(text, _bare(table))
        for parent in self._parents[table]:
            score += sum(self._mentions(text, name, parent) for name in self._columns(parent) - own)
        counted = _counted(text)
        if counted and self._mentions(counted, _bare(table)):
            # "işlem sayısı" counts transactions, whatever else is mentioned.
            score += 20
        # A matched value is strong evidence: few tables store it.
        return score + 10 * len(self._conditions(table, text))

    def _named_numeric(self, table: str, text: str) -> list[tuple[int, Any]]:
        """The numeric measures of ``table`` the question names, with how strongly."""
        return [
            (score, m)
            for m in self._models[table].measures
            if not m.is_row_count
            and m.numeric
            and not _looks_like_a_key(m.name)
            and (score := self._mentions(text, m.name, table))
        ]

    def _fact(self, text: str) -> str:
        """The table the question measures.

        A sum or an average is of a number, so when the question names one
        the choice is among the tables that hold it: "premium müşterilerin
        toplam tutarı" is about amounts, however often it says customer.
        """
        candidates = self._tables
        if _first_match(text, AGGREGATIONS) in _NUMERIC_AGGS:
            candidates = [t for t in self._tables if self._named_numeric(t, text)] or candidates
        return max(candidates, key=lambda name: self._score(name, text))

    def _measure_and_agg(self, table: str, text: str) -> tuple[str, str]:
        model = self._models[table]
        agg = _first_match(text, AGGREGATIONS)
        numeric = self._named_numeric(table, text)
        if numeric and agg not in ("count", "count_distinct", ""):
            return str(max(numeric, key=lambda pair: pair[0])[1].name), agg
        if numeric and agg == "" and not _counted(text):
            # A number named with no word for how ("şube bazında işlem tutarı"):
            # its own aggregation, a sum for an amount. Counting rows instead
            # answered a question nobody asked, broken down by the amount.
            measure = max(numeric, key=lambda pair: pair[0])[1]
            return str(measure.name), measure.default_agg.lower()
        if agg in ("", "count"):
            # "how many X" counts rows; nothing else needs naming.
            return "*", "count"
        if numeric:
            return str(max(numeric, key=lambda pair: pair[0])[1].name), agg
        named = [
            (score, m)
            for m in model.measures
            if not m.is_row_count and (score := self._mentions(text, m.name, table))
        ]
        if named and agg == "count_distinct":
            return str(max(named, key=lambda pair: pair[0])[1].name), agg
        return "*", "count"

    def _dimensions_for(self, table: str, text: str, measure: str) -> list[dict[str, Any]]:
        """What to break the measure down by.

        A dimension is only taken when the question asks for a breakdown
        ("kanal bazında", "per segment") or names a time grain. Without that
        test every mention of a column would become a GROUP BY, and "toplam
        tutar" would answer a question nobody asked.
        """
        model = self._models[table]
        grain = _first_match(text, GRAINS)
        chosen: list[dict[str, Any]] = []
        temporal = next((d for d in model.dimensions if d.temporal), None)
        if grain and temporal is not None:
            chosen.append({"column": temporal.name, "grain": grain})
        if not any(marker in text for marker in BREAKDOWN):
            return chosen
        filtered = {f["column"] for f in self._conditions(table, text)}
        own = self._columns(table)
        # (how strongly named, the table's own before a referenced one's, name, owner)
        scored = [
            (self._mentions(text, d.name, table), 1, d.name, table)
            for d in model.dimensions
            if not d.temporal
        ]
        for parent in self._parents[table]:
            scored += [
                (self._mentions(text, d.name, parent), 0, f"{_bare(parent)}.{d.name}", parent)
                for d in self._models[parent].dimensions
                if not d.temporal and d.name not in own
            ]
        for score, _, name, owner in sorted(scored, reverse=True):
            # Not the measure itself, and not something a filter has already
            # pinned to a single value. A name that reads like a key is only
            # rejected when nothing suggests it is a category: `sube_kodu` is
            # a branch code with three values and groups perfectly well, while
            # `islem_id` has one value per row and groups into nothing. Having
            # stored values is what draws that line.
            if not score or name == measure or name in filtered:
                continue
            column = name.rpartition(".")[2]
            if _looks_like_a_key(column) and column not in self._values[owner]:
                continue
            chosen.append({"column": name})
            break
        return chosen

    # -- the whole request --------------------------------------------------

    def plan(self, question: str) -> dict[str, Any]:
        """The request this question asks for, in the shape ``to_dsl`` takes."""
        text = fold(question)
        table = self._fact(text)
        measure, agg = self._measure_and_agg(table, text)
        transform = _first_match(text, TRANSFORMS)
        metric: dict[str, Any] = {"measure": measure, "aggregation": agg}
        if transform:
            metric["transform"] = transform
        dimensions = self._dimensions_for(table, text, measure)
        request: dict[str, Any] = {
            "table": table,
            "metrics": [metric],
            "dimensions": dimensions,
            "filters": list(self._conditions(table, text)),
        }
        limit = _limit_in(text)
        if limit:
            request["limit"] = limit
            # "the 5 highest" is an ordering as much as a limit, and the name
            # to order by is whatever the domain calls the chosen metric.
            request["order_by"] = parse_metric(f"{measure}:{agg}").output_name()
            request["descending"] = agg != "min"
        return request

    def coverage(self, question: str) -> list[dict[str, Any]]:
        """What each word of ``question`` was recognised as, in order; nothing for a word not.

        The same matching ``plan`` does (folded words, shared stems, the
        vocabulary's words for names and values), word by word, so a reader
        sees which words found nothing in the vocabulary: the part a question
        cannot be answered for, or the words a steward has yet to add.
        """
        # A suffix after an apostrophe ("İstanbul'daki") is grammar, not a word.
        text = re.sub(r"['\u2019]\w+", "", fold(question))
        words = re.findall(r"\w\w+", text)  # a lone letter ("e-posta") is no word
        found: dict[int, list[str]] = {i: [] for i in range(len(words))}

        def same(word: str, part: str) -> bool:
            # Inflected as the planner allows it: "toplamını" is "toplami", "değişimi" "degisim".
            return word == part or (len(part) >= 4 and word.startswith(part))

        def mark(phrase: str, what: str) -> None:
            """Every place the folded phrase's words occur in a row, as ``what``."""
            parts = phrase.split()
            for i in range(len(words) - len(parts) + 1):
                if all(same(w, p) for w, p in zip(words[i : i + len(parts)], parts, strict=True)):
                    for j in range(i, i + len(parts)):
                        found[j].append(what)

        for phrases, kind in (
            (AGGREGATIONS, "aggregation"),
            (TRANSFORMS, "transform"),
            (GRAINS, "grain"),
        ):
            for value, phrase in phrases:
                mark(phrase, f"{kind} {value}")
        for marker in BREAKDOWN:
            mark(marker.strip(), "breakdown")
        for superlative in re.finditer(_SUPERLATIVE, text):
            mark(superlative.group(0), "ordering")
        limit = _limit_in(text)
        if limit:
            mark(str(limit), "row limit")

        def names(word: str, candidate: str) -> bool:
            if len(candidate) < 3:
                return False
            shared = len(os.path.commonprefix([candidate, word]))
            return shared >= min(len(candidate), STEM)

        reach = list(
            dict.fromkeys([*self._tables, *(p for t in self._tables for p in self._parents[t])])
        )
        for i, word in enumerate(words):
            if word in STOPWORDS or word.isdigit():
                continue
            for table in reach:
                bare = _bare(table)
                if any(names(word, c) for c in _names_for(bare, self._synonyms.get(bare))):
                    found[i].append(f"table {table}")
                for column in sorted(self._columns(table)):
                    candidates = _names_for(column, self._synonyms.get(column), bare)
                    if any(" " not in c and names(word, c) for c in candidates):
                        found[i].append(f"column {table}.{column}")
                for column, stored in self._values[table].items():
                    for value in stored:
                        folded = fold(str(value))
                        spelled = (folded, *(fold(w) for w in self._value_synonyms.get(folded, ())))
                        if len(folded) >= 4 and any(
                            names(word, s) for s in spelled if " " not in s
                        ):
                            found[i].append(f"value {table}.{column} = {value}")
                            break
        for table in reach:  # a name of several words counts where all of them are
            for column in self._columns(table):
                for candidate in _names_for(column, self._synonyms.get(column), _bare(table)):
                    if " " in candidate and _agreement(candidate, text):
                        mark(candidate, f"column {table}.{column}")
        return [
            {"word": word, "read_as": list(dict.fromkeys(found[i]))}
            for i, word in enumerate(words)
            if found[i] or not (word in STOPWORDS or word.isdigit())
        ]


#: Words that carry no part of a request: asked about, never looked up.
STOPWORDS = frozenset(
    "ne nedir neler hangi hangisi bir ve ile icin olan bu su o da de mi mu midir var "
    "yok bana goster listele getir nasil ki gibi daha cok olarak "
    "yapmis yapan yapilan yapti edilen eden edilmis olmus oldu ver "
    "what which who is are was were be been the a an of in on at for and or with to "
    "me show list give get all there do does did done made we our have has had how "
    "much many each their its".split()
)


def _bare(table: str) -> str:
    """``source.table`` -> ``table``."""
    return table.rpartition(".")[2]


def _clean(lexicon: Mapping[str, Sequence[str]]) -> dict[str, tuple[str, ...]]:
    """Make a lexicon usable however it was written.

    A generated one arrives with leading spaces and accents, and with entries
    that are just the name again — ``tutar: ["tutar", " tutari"]``. A leading
    space alone would make an entry dead on arrival, because the match anchors
    on a word boundary. Echoes of the name are dropped rather than kept: the
    stem match already covers every inflection, so an entry that only repeats
    the name adds nothing and hides that nothing was contributed.
    """
    cleaned: dict[str, tuple[str, ...]] = {}
    for name, words in lexicon.items():
        stem = fold(str(name))[:STEM]
        kept = []
        for word in words:
            folded = fold(str(word)).strip()
            if len(folded) > 2 and not folded.startswith(stem) and folded not in kept:
                kept.append(folded)
        if kept:
            cleaned[name] = tuple(kept)
    return cleaned


def _value_asked_for(text: str, word: str) -> bool:
    """Whether this word is being used as a value rather than as a column.

    "şube bazında" asks to group *by* branch; it does not ask for the rows
    whose channel is ``sube``. A word immediately followed by a breakdown
    marker is naming a dimension, so it is not read as a value.
    """
    for found in re.finditer(rf"\b{re.escape(word)}\w*", text):
        rest = text[found.end() :].lstrip()
        if not any(rest.startswith(marker.strip()) for marker in BREAKDOWN):
            return True
    return False


def _looks_like_a_key(name: str) -> bool:
    """Whether a name reads as an identifier rather than something to group by."""
    lowered = fold(name)
    return lowered == "id" or lowered.endswith(("_id", "_key", "_code", "_kodu"))


#: Words that turn a number into a row count rather than part of the question.
#: The trailing \b is load-bearing: without it `top` matches the start of
#: `toplam` — "total", the commonest word in a Turkish analytics question — so
#: "son 3 ayin toplam tutari" read as a three-row cap.
_SUPERLATIVE = r"(?:en (?:yuksek|dusuk|fazla|az|buyuk|kucuk)|top|ilk|highest|lowest)\b"

#: The number has to be *next to* the superlative, give or take a qualifier —
#: "en yuksek tutarli 5 islem", "the 5 highest transactions". Taking the first
#: number anywhere in the sentence read "Son 7 gunde en yuksek islemleri" as a
#: limit of 7, where the 7 is a time window and no row cap was asked for.
_LIMIT_PATTERNS = (
    re.compile(rf"{_SUPERLATIVE}\s+(?:\w+\s+){{0,2}}?(\d{{1,3}})\b"),
    # Nothing between the two in this direction: "5 highest" is a row count,
    # "7 gunde en yuksek" is a time window that happens to precede one.
    re.compile(rf"\b(\d{{1,3}})\s+{_SUPERLATIVE}"),
)


def _limit_in(text: str) -> int:
    """The N in "en yüksek 5", "top 3", "the 5 highest" — and nowhere else."""
    for pattern in _LIMIT_PATTERNS:
        found = pattern.search(text)
        if found:
            return int(found.group(1))
    return 0


# -- the vocabulary a catalog's certified terms make ------------------------------


@dataclass(slots=True)
class Vocabulary:
    """The planner's three inputs, as the certified terms give them."""

    synonyms: dict[str, list[str]] = field(default_factory=dict)
    value_synonyms: dict[str, list[str]] = field(default_factory=dict)
    values: dict[str, dict[str, list[str]]] = field(default_factory=dict)

    def add(
        self,
        source: str,
        terms: Iterable[Term],
        hidden: Callable[[str], Collection[str]] = lambda table: (),
    ) -> None:
        """Take in the terms of one source.

        ``hidden(table)`` names the columns whose stored values must not be
        matched: for a caller they are masked for, whether a word in the
        question is one of them would be a way to guess at what is stored.
        """
        for term in terms:
            for table in term.tables:
                self.synonyms.setdefault(table, []).extend([term.name, *term.synonyms])
            for attribute in term.column_attributes:
                self.synonyms.setdefault(attribute.column, []).extend(attribute.synonyms)
                if not attribute.values or attribute.column in hidden(attribute.table):
                    continue
                table_values = self.values.setdefault(f"{source}.{attribute.table}", {})
                table_values[attribute.column] = list(attribute.values)
                for value, words in attribute.values.items():
                    self.value_synonyms.setdefault(fold(value), []).extend(words)


# -- from a planned request to query_metrics' arguments ---------------------------


def _metric_to_dsl(metric: Any) -> str:
    """``{'measure': 'tutar', 'aggregation': 'sum'}`` -> ``'tutar:sum'``."""
    if isinstance(metric, str):
        return metric
    if not isinstance(metric, Mapping):
        return str(metric)
    measure = str(metric.get("measure", "")).strip()
    agg = str(metric.get("aggregation", "")).strip()
    expression = f"{measure}:{agg}" if agg else measure
    transform = str(metric.get("transform", "")).strip()
    return f"{transform}({expression})" if transform else expression


def _dimension_to_dsl(dimension: Any) -> str:
    """``{'column': 'islem_tarihi', 'grain': 'month'}`` -> ``'islem_tarihi:month'``."""
    if isinstance(dimension, str):
        return dimension
    if not isinstance(dimension, Mapping):
        return str(dimension)
    column = str(dimension.get("column", "")).strip()
    grain = str(dimension.get("grain", "")).strip()
    return f"{column}:{grain}" if grain else column


def to_dsl(request: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise a planned (or model-written) request into ``query_metrics``' arguments.

    Accepts both shapes, the structured one and plain DSL strings, so
    everything that produces a request shares one execution path.
    """
    metrics = [_metric_to_dsl(m) for m in request.get("metrics") or []]
    dimensions = [_dimension_to_dsl(d) for d in request.get("dimensions") or []]
    order_by = request.get("order_by") or ""
    if isinstance(order_by, str) and order_by and request.get("descending"):
        order_by = f"-{order_by.lstrip('-')}"
    return {
        "table": request.get("table"),
        "metrics": [m for m in metrics if m],
        "dimensions": [d for d in dimensions if d],
        "filters": list(request.get("filters") or []),
        "order_by": order_by,
        "limit": request.get("limit", 100),
    }
