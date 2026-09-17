"""Hexagonal layering rules, enforced from import statements.

Layers (dependencies point inwards):
    adapters -> application -> ports -> domain

Composition roots (stargate/__init__.py, bootstrap.py, runtime.py) may import
anything. Deprecated shim modules are exempt. Rules are data (RULES, ALLOWLIST)
so a deliberate exception is a one-line, reviewable change.
"""

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

SRC = Path(__file__).parent.parent / "src"
PACKAGE = "stargate"
STDLIB = set(sys.stdlib_module_names)

COMPOSITION_ROOTS = {"stargate", "stargate.bootstrap", "stargate.runtime"}

# Old import paths kept for backwards compatibility; re-export only.
SHIMS = {
    "stargate.converter",
    "stargate.cli",
    "stargate.models",
    "stargate.models.unified",
    "stargate.models.mcp",
    "stargate.parsers",
    "stargate.parsers.base",
    "stargate.parsers.openapi",
    "stargate.parsers.postman",
    "stargate.parsers.manual",
    "stargate.generators",
    "stargate.generators.mcp_server",
    "stargate.generators.tools",
    "stargate.generators.resources",
    "stargate.utils",
    "stargate.utils.http",
    "stargate.utils.schema",
    "stargate.utils.security",
    "stargate.server",
    "stargate.server.runtime",
}


@dataclass(frozen=True)
class Rule:
    """third_party: allowed non-stdlib packages (None = anything); internal: layer prefixes."""

    third_party: frozenset[str] | None
    internal: tuple[str, ...]


RULES: dict[str, Rule] = {
    "stargate.domain": Rule(frozenset({"pydantic"}), ("stargate.domain",)),
    "stargate.ports": Rule(frozenset(), ("stargate.domain",)),
    "stargate.application": Rule(
        frozenset({"structlog"}),
        ("stargate.domain", "stargate.ports", "stargate.application"),
    ),
    "stargate.adapters": Rule(None, ("stargate.domain", "stargate.ports", "stargate.application")),
}

# Explicitly allowed edges that the generic rules would reject: (importer, imported).
ALLOWLIST: set[tuple[str, str]] = {
    ("stargate.adapters.cli", "stargate"),  # __version__
    ("stargate.adapters.cli", "stargate.bootstrap"),  # CLI is a driving adapter over the roots
    ("stargate.adapters.cli", "stargate.runtime"),
    ("stargate.adapters.cli", "stargate.adapters.mcp_wire"),  # --json-output projection
    ("stargate.adapters.parsers.base", "stargate.adapters.spec_loader"),  # deprecated parse_file
}


@dataclass(frozen=True)
class ImportEdge:
    importer: str
    imported: str
    module_level: bool
    line: int


def _module_name(path: Path) -> str:
    relative = path.relative_to(SRC).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_relative(importer: str, is_package: bool, level: int, module: str | None) -> str:
    base_parts = importer.split(".") if is_package else importer.split(".")[:-1]
    if level > 1:
        base_parts = base_parts[: len(base_parts) - (level - 1)]
    base = ".".join(base_parts)
    return f"{base}.{module}" if module else base


def _collect_edges() -> list[ImportEdge]:
    edges: list[ImportEdge] = []
    for path in sorted(SRC.rglob("*.py")):
        importer = _module_name(path)
        is_package = path.name == "__init__.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        top_level_ids = {id(node) for node in tree.body}
        for node in ast.walk(tree):
            module_level = id(node) in top_level_ids
            if isinstance(node, ast.Import):
                for alias in node.names:
                    edges.append(ImportEdge(importer, alias.name, module_level, node.lineno))
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    target = _resolve_relative(importer, is_package, node.level, node.module)
                else:
                    target = node.module or ""
                edges.append(ImportEdge(importer, target, module_level, node.lineno))
    return edges


def _layer_of(module: str) -> str | None:
    for layer in RULES:
        if module == layer or module.startswith(layer + "."):
            return layer
    return None


def _is_exempt(module: str) -> bool:
    return module in COMPOSITION_ROOTS or module in SHIMS


def _internal_allowed(importer: str, imported: str, rule: Rule) -> bool:
    if (importer, imported) in ALLOWLIST:
        return True
    if imported == importer or imported.startswith(importer + "."):
        return True  # a package may import its own submodules
    if any(imported == prefix or imported.startswith(prefix + ".") for prefix in rule.internal):
        return True
    # adapters.parsers.* share a base class
    if (
        importer.startswith("stargate.adapters.parsers.")
        and imported == "stargate.adapters.parsers.base"
    ):
        return True
    return False


ALL_EDGES = _collect_edges()
ALL_MODULES = sorted(
    {edge.importer for edge in ALL_EDGES} | {_module_name(p) for p in SRC.rglob("*.py")}
)


def test_every_module_is_classified():
    unclassified = [
        m
        for m in ALL_MODULES
        if not (_is_exempt(m) or _layer_of(m) or m.startswith("stargate.ports"))
    ]
    assert not unclassified, f"Modules outside the known layers/roots/shims: {unclassified}"


def test_layer_rules():
    violations: list[str] = []
    for edge in ALL_EDGES:
        if _is_exempt(edge.importer):
            continue
        layer = _layer_of(edge.importer)
        if layer is None:
            continue  # reported by test_every_module_is_classified
        rule = RULES[layer]
        top = edge.imported.split(".")[0]
        where = f"{edge.importer}:{edge.line}"
        if top == PACKAGE:
            if not _internal_allowed(edge.importer, edge.imported, rule):
                violations.append(f"{where} imports {edge.imported} (not allowed from {layer})")
        elif top in STDLIB or top == "__future__":
            continue
        elif rule.third_party is not None and top not in rule.third_party:
            violations.append(f"{where} imports third-party '{top}' (not allowed in {layer})")
    assert not violations, "Layer violations:\n  " + "\n  ".join(violations)


def test_no_import_cycles():
    graph: dict[str, set[str]] = {m: set() for m in ALL_MODULES}
    for edge in ALL_EDGES:
        if edge.module_level and edge.imported.startswith(PACKAGE) and edge.imported in graph:
            graph[edge.importer].add(edge.imported)

    white, grey, black = 0, 1, 2
    color = dict.fromkeys(graph, white)
    stack: list[str] = []
    cycles: list[list[str]] = []

    def visit(node: str) -> None:
        color[node] = grey
        stack.append(node)
        for nxt in sorted(graph[node]):
            if color[nxt] == grey:
                cycles.append(stack[stack.index(nxt) :] + [nxt])
            elif color[nxt] == white:
                visit(nxt)
        stack.pop()
        color[node] = black

    for module in graph:
        if color[module] == white:
            visit(module)

    assert not cycles, "Import cycles:\n  " + "\n  ".join(" -> ".join(c) for c in cycles)
