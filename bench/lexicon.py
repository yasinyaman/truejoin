"""The benchmark's own vocabulary for the banking fixture, over the product's planner.

The planner lives in ``trio.application.olap.lexicon`` and takes its words from
the catalog's certified terms. The arms here predate that: they use a
vocabulary written by hand (below) or by a model (``lexicon_gen``), and read
the stored values off the data. Keeping both is what lets "a person wrote the
words", "a model wrote them" and "the catalog holds them" be compared.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from trio.application.olap.lexicon import LexiconPlanner as _Planner
from trio.application.olap.lexicon import fold

__all__ = ["SYNONYMS", "VALUE_SYNONYMS", "LexiconPlanner", "fold"]

#: Business words for columns a bank's schema spells differently. This is
#: operator configuration, not inference: deciding that "ciro" means `tutar`
#: is the same decision as defining a measure, and belongs to whoever owns
#: the data rather than to whatever is reading the question.
SYNONYMS: Mapping[str, tuple[str, ...]] = {
    "tutar": ("ciro", "hacim", "amount", "revenue", "volume", "value", "miktar"),
    "islem": ("transaction", "islemler", "transactions"),
    "musteri": ("customer", "musteriler", "customers", "client"),
    "kanal": ("channel", "kanallar"),
    "durum": ("status", "state"),
    "segment": ("segmentler", "tier"),
    "sube_kodu": ("sube", "branch", "subeler", "branches"),
    "islem_turu": ("tur", "type", "turu", "islem tipi"),
    "para_birimi": ("currency", "doviz", "para"),
    "islem_tarihi": ("tarih", "date", "zaman", "time"),
    "acilis_tarihi": ("acilis", "opening", "opened"),
}


#: Words for the values themselves. Turkish questions match the stored value
#: after folding — "başarılı" is ``basarili`` — but an English question asking
#: about successful transactions has no word in common with the data at all.
#: Like the column synonyms, this is the operator saying what their codes mean.
VALUE_SYNONYMS: Mapping[str, tuple[str, ...]] = {
    "basarili": ("successful", "success", "completed"),
    "iptal": ("cancelled", "canceled", "cancellation"),
    "havale": ("wire", "remittance"),
    "odeme": ("payment", "paid"),
    "transfer": ("transfers",),
    "mobil": ("mobile",),
    "web": (),
    "sube": ("branch office",),
    "premium": (),
    "standart": ("standard",),
    "temel": ("basic",),
}


class LexiconPlanner(_Planner):
    """The planner with the hand-written vocabulary and live values as defaults."""

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
            synonyms: Defaults to the hand-written table; a generated
                vocabulary is passed in here.
            values: ``table -> column -> values``, as a request schema already
                carries. Left out, they are discovered, one DSL round trip
                per dimension.
        """
        if values is None:
            from bench.constrain import dimension_values

            values = {
                table: {
                    d.name: found
                    for d in semantic.model_for(table).dimensions
                    if (found := dimension_values(semantic, table, d))
                }
                for table in tables
            }
        super().__init__(
            semantic,
            tables,
            SYNONYMS if synonyms is None else synonyms,
            VALUE_SYNONYMS if value_synonyms is None else value_synonyms,
            values,
            parents,
        )
