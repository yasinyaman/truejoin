"""Deprecated import path. Use stargate.adapters.cli instead."""

import warnings

from stargate.adapters.cli import main

warnings.warn(
    "stargate.cli is deprecated; use stargate.adapters.cli",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["main"]

if __name__ == "__main__":
    main()
