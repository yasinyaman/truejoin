"""Port: reading a semantic model written for another tool."""

from typing import Protocol

from truejoin.domain.catalog.semantic_draft import SemanticDraft


class SemanticModelReader(Protocol):
    """Reads one format (Apache Ossie, dbt MetricFlow) into a draft."""

    def read(self, text: str) -> SemanticDraft:
        """The model in ``text`` (YAML or JSON).

        Raises:
            ValueError: When ``text`` is not a model of this format at all,
                saying what it lacks. What it holds that a catalog cannot is
                listed in the draft's ``skipped``, not raised.
        """
        ...
