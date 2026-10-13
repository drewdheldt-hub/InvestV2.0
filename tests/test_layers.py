"""Layers depend downwards only, checked on every run so the rule cannot drift.

    app -> execution, portfolio, research -> evidence -> data
ops and agents may use any layer's public functions, but only execution may import the broker library.
"""

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "src" / "investtree"

# what each layer must never import
FORBIDDEN = {
    "data": {"evidence", "research", "portfolio", "execution", "app", "agents", "ops"},
    "evidence": {"research", "portfolio", "execution", "app", "agents", "ops"},
    "research": {"portfolio", "execution", "app", "agents"},
    "portfolio": {"research", "app", "agents"},
    "execution": {"research", "app", "agents"},
}


def imports(path: Path) -> set[str]:
    """investtree layers and third-party roots a module imports."""
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level >= 2:                        # from ..layer import x
                out.add(mod.split(".")[0])
            elif mod.startswith("investtree."):
                out.add(mod.split(".")[1])
            elif node.level == 0:
                out.add("ext:" + mod.split(".")[0])
        elif isinstance(node, ast.Import):
            out |= {"ext:" + a.name.split(".")[0] for a in node.names}
    return out


def test_layers_only_depend_downwards() -> None:
    bad = []
    for layer, forbidden in FORBIDDEN.items():
        for f in (PKG / layer).rglob("*.py"):
            hit = imports(f) & forbidden
            if hit:
                bad.append(f"{f.relative_to(PKG)} imports {sorted(hit)}")
    assert not bad, "\n".join(bad)


def test_only_execution_talks_to_the_broker_library() -> None:
    bad = [str(f.relative_to(PKG)) for f in PKG.rglob("*.py")
           if "ext:ib_async" in imports(f) and f.parent.name != "execution"]
    assert not bad, f"ib_async imported outside execution/: {bad}"
