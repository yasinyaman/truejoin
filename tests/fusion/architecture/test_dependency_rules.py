"""Enforce the hexagonal dependency rule with an AST walk over ``trio/``.

Every ``import``/``from ... import`` (including lazy ones inside function
bodies) is classified as stdlib, first-party (``trio.*``) or third-party and
checked against the layer's allowance. One walker covers what Fusion's
walker, Warp's import-linter contracts and Stargate's architecture test used
to check separately.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[3] / "trio"
STDLIB = set(sys.stdlib_module_names)

# The pure layers may use pydantic: Warp's catalog on disk is pydantic JSON and
# Stargate's spec model is pydantic too; rewriting them buys nothing.
PURE = frozenset({"pydantic"})

# shortcut: the config value objects (DatabaseConfig, LLMSlot, ...) live in
# application.settings and ports/adapters import them. Phase 1 unified Settings
# without moving them; move them to the domain when ports stop taking whole
# config objects, then drop this prefix.
CONFIG = "trio.application.settings"

# layer package -> (allowed first-party prefixes, allowed third-party: a set, or True for any)
RULES: dict[str, tuple[tuple[str, ...], frozenset[str] | bool]] = {
    "trio.domain": (("trio.domain",), PURE),
    "trio.ports": (("trio.domain", "trio.ports", CONFIG), PURE),
    # shortcut: Stargate's application logs through structlog; swap for the
    # stdlib logger when the mcpgen services are touched in Phase 2.
    "trio.application": (("trio.domain", "trio.ports", "trio.application"), PURE | {"structlog"}),
    "trio.observability": (
        ("trio.domain", "trio.application", "trio.observability"),
        False,
    ),
    "trio.adapters.outbound": (
        ("trio.domain", "trio.ports", "trio.adapters.outbound", CONFIG),
        True,
    ),
    "trio.adapters.inbound": (
        (
            "trio.domain",
            "trio.ports",
            "trio.application",
            "trio.observability",
            "trio.adapters.inbound",
            "trio.bootstrap",
            "trio.runtime_mcpgen",
        ),
        True,
    ),
}

# Imported for a version string, by exact name: as a prefix, "trio" would allow everything.
EXACT: dict[str, frozenset[str]] = {
    "trio.adapters.inbound": frozenset({"trio", "trio.mcpgen"}),
}


def _module_name(path: Path) -> str:
    rel = path.relative_to(PACKAGE_ROOT.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    module = _module_name(path)
    package = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                found.append(f"{base}.{node.module}" if node.module else base)
            else:
                found.append(node.module or "")
    return found


def _matches(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def _layer_files(layer: str) -> list[Path]:
    directory = PACKAGE_ROOT.joinpath(*layer.split(".")[1:])
    return sorted(directory.rglob("*.py")) if directory.exists() else []


@pytest.mark.parametrize("layer", sorted(RULES))
def test_layer_imports_respect_dependency_rule(layer: str) -> None:
    allowed_prefixes, third_party = RULES[layer]
    violations: list[str] = []
    for path in _layer_files(layer):
        for name in _imports(path):
            top = name.split(".", 1)[0]
            if top in STDLIB:
                continue
            if top == "trio":
                if _matches(name, allowed_prefixes) or name in EXACT.get(layer, ()):
                    continue
                violations.append(f"{path.relative_to(PACKAGE_ROOT.parent)} -> {name}")
            elif third_party is True or (third_party and top in third_party):
                continue
            else:
                violations.append(f"{path.relative_to(PACKAGE_ROOT.parent)} -> {name}")
    assert not violations, "\n".join(violations)


def test_domain_and_ports_exist() -> None:
    assert _layer_files("trio.domain"), "trio/domain must exist"
    assert _layer_files("trio.ports"), "trio/ports must exist"
