"""K8 import isolation (TS-06-23's static precondition): `opengrid.safestop` must import only
`opengrid.core`, `opengrid.platform`, `opengrid.trace` (and itself) -- never `opengrid.engine`,
`opengrid.guardian`, `opengrid.ledger` or `opengrid.allocator`. This is what lets `og-safestop` keep
working when `og-engine` and `og-guardian` are both down.
"""

from __future__ import annotations

import ast
from pathlib import Path

SAFESTOP_ROOT = Path(__file__).resolve().parents[3] / "src" / "opengrid" / "safestop"

ALLOWED_OPENGRID_PREFIXES = ("opengrid.core", "opengrid.platform", "opengrid.trace", "opengrid.safestop")
FORBIDDEN_MODULES = ("opengrid.engine", "opengrid.guardian", "opengrid.ledger", "opengrid.allocator")


def _opengrid_imports(tree: ast.Module) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names if alias.name.startswith("opengrid"))
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("opengrid"):
            modules.append(node.module)
    return modules


def _all_safestop_files() -> list[Path]:
    assert SAFESTOP_ROOT.exists(), f"expected {SAFESTOP_ROOT} to exist"
    return sorted(SAFESTOP_ROOT.rglob("*.py"))


def test_safestop_package_has_python_files():
    assert _all_safestop_files(), "no .py files found under opengrid.safestop"


def test_safestop_imports_only_core_platform_trace():
    violations: list[str] = []
    for path in _all_safestop_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _opengrid_imports(tree):
            if not module.startswith(ALLOWED_OPENGRID_PREFIXES):
                violations.append(f"{path}: imports {module!r}")
    assert not violations, "opengrid.safestop imports outside core/platform/trace:\n" + "\n".join(violations)


def test_safestop_never_imports_forbidden_modules():
    violations: list[str] = []
    for path in _all_safestop_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _opengrid_imports(tree):
            if any(module == f or module.startswith(f + ".") for f in FORBIDDEN_MODULES):
                violations.append(f"{path}: imports forbidden module {module!r}")
    assert not violations, "opengrid.safestop imports engine/guardian/ledger/allocator:\n" + "\n".join(
        violations
    )
