"""Port: parse a raw specification document into the unified API model."""

from typing import Any, ClassVar, Protocol

from stargate.domain.api import UnifiedAPISchema


class SpecParser(Protocol):
    """Turns one input format (OpenAPI, Postman, manual, ...) into UnifiedAPISchema.

    Implementations are pure: they receive an already-loaded dict and never
    touch files or the network (see SpecLoader for that).
    """

    format_name: ClassVar[str]
    warnings: list[str]
    errors: list[str]

    def can_parse(self, spec: dict[str, Any]) -> bool:
        """Returns True if this parser recognizes the document."""
        ...

    def parse(self, spec: dict[str, Any]) -> UnifiedAPISchema:
        """Converts the document; collects non-fatal issues in warnings/errors."""
        ...
