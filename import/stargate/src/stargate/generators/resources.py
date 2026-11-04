"""Deprecated import path. Use stargate.application.generators.resources instead."""

import warnings

from stargate.application.generators.resources import ResourcesGenerator, generate_resources

warnings.warn(
    "stargate.generators.resources is deprecated; use stargate.application.generators.resources",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "ResourcesGenerator",
    "generate_resources",
]
