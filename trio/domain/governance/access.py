"""Who is asking, and what that lets them see: the scope every analytical query runs under.

The table routes apply a caller's row rules and masks themselves, one table
at a time. SQL has no such seam, so the same two things travel with the query
instead: an :class:`AccessScope` says which rows and which masks apply, and a
:class:`TableView` is one table as that caller is allowed to see it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from trio.domain.governance.masking import CatalogMasking
from trio.domain.governance.row_policy import EMPTY_POLICY, PolicyError, RowPolicy

#: The store function that the ``hash`` mask calls; keyed, so it is registered
#: by whoever holds the key rather than spelled out in SQL.
PSEUDONYM_FUNCTION = "trio_pseudonym"


@dataclass(frozen=True)
class AccessScope:
    """One caller's restrictions.

    ``masking`` is per source (database), because the labels masks key on come
    from that database's approved catalog. ``masked`` says the caller is
    subject to masking at all; it is separate from ``masking`` so that a
    source nobody built labels for is refused rather than served raw.
    """

    actor: str | None = None
    tenant: str | None = None
    roles: tuple[str, ...] = ()
    policy: RowPolicy = EMPTY_POLICY
    masking: Mapping[str, CatalogMasking] = field(default_factory=dict)
    masked: bool = False
    request_id: str = ""

    @property
    def is_restricted(self) -> bool:
        """Whether anything at all is withheld from this caller."""
        return self.masked or not self.policy.is_empty

    def conditions_for(self, table: str) -> tuple[tuple[str, str, Any], ...]:
        """The row conditions on ``table`` (a bare name, as row rules are written)."""
        return tuple(self.policy.conditions_for(table))

    def masks_for(self, source: str, table: str) -> dict[str, str]:
        """``column -> strategy`` for one table.

        Raises:
            PolicyError: When the caller is masked and nothing is known about
                this source. No labels is not the same as nothing to mask.
        """
        if not self.masked:
            return {}
        masking = self.masking.get(source)
        if masking is None or not masking.semantic_types:
            raise PolicyError(
                f"Column masking applies to this caller, but '{source}' has no masking "
                f"labels in this process, so its tables cannot be served masked."
            )
        return masking.masks_for(table, self.roles)


UNRESTRICTED = AccessScope()
"""Sees everything: an SDK caller, or a key with no row rules and no masks."""


@dataclass(frozen=True)
class TableView:
    """One table as a caller may see it: where it lives, which rows, which masks.

    ``columns`` are the ones the stored table really has, in order; a slice
    may hold fewer than the source table does.
    """

    name: str
    columns: tuple[str, ...]
    conditions: tuple[tuple[str, str, Any], ...] = ()
    masks: Mapping[str, str] = field(default_factory=dict)

    @property
    def masked_columns(self) -> tuple[str, ...]:
        """The columns of this view that are actually masked."""
        return tuple(column for column in self.columns if column in self.masks)
