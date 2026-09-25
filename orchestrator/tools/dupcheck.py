#!/usr/bin/env python3
"""Duplicate-formula and cross-import checker (BUILD.md S1, 02b S12, test TS-01-07).

Fails (`exit 1`) if:
  1. A module outside `opengrid.core` defines a function whose name matches one of the "core formula"
     names (SoC step, capability/limit checks, product rounding, Ed25519 sign/verify, trace hashing) --
     a strong signal it was re-implemented instead of imported.
  2. Any file under `opengrid` imports `ogsim`, or any file under `ogsim` imports `opengrid`
     (BUILD.md S1: "ogsim must never import opengrid, and opengrid must never import ogsim").

This is an AST-based heuristic, not a full re-implementation detector -- it catches the "someone wrote
`def soc_step(...)` in allocator.py instead of importing `opengrid.core.physics.soc_step`" class of
mistake, per 02b S12's rule that every such function has exactly one implementation.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
SIM_ROOT = Path(__file__).resolve().parents[2] / "integration-sims" / "src"

# Function/method names owned exclusively by opengrid.core.* (02b S12 table). A definition of any of
# these names anywhere else under `opengrid` is presumed to be a forbidden re-implementation.
CORE_OWNED_NAMES = frozenset(
    {
        "soc_step",
        "hub_capability",
        "bank_capability",
        "recharge_headroom",
        "apply_ramp_limit",
        "check_reserve_floor",
        "check_hub_power",
        "check_bank_kva",
        "check_hub_ramp",
        "check_fleet_ramp_cap",
        "check_feeder_ramp_ceiling",
        "check_one_buyer",
        "check_commitment_lock",
        "derive_variable_kind",
        "round_quantity",
        "canonicalize_json",
        "sign_payload",
        "verify_payload",
        "record_hash",
        "verify_chain",
        "checkpoint_hash",
    }
)

CORE_PACKAGE_PREFIX = "opengrid.core"


class DupCheckError(Exception):
    pass


def _module_name_for(path: Path, root: Path) -> str:
    """`root` is a `src/` directory whose immediate children are top-level packages (e.g. `opengrid`),
    so `rel.parts` already starts with the package name -- do not prepend it again."""
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _iter_python_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(root.rglob("*.py"))


def _parse(path: Path) -> ast.Module:
    """Parse a Python file, tolerating a UTF-8 BOM (some editors write one)."""
    return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))


def check_no_duplicate_core_formulas(src_root: Path = SRC_ROOT) -> list[str]:
    violations: list[str] = []
    for path in _iter_python_files(src_root):
        module_name = _module_name_for(path, src_root)
        if module_name.startswith(CORE_PACKAGE_PREFIX):
            continue
        tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in CORE_OWNED_NAMES:
                violations.append(
                    f"{path}:{node.lineno}: re-implements core-owned function '{node.name}' "
                    f"outside opengrid.core -- import it from opengrid.core instead"
                )
    return violations


def _imports_forbidden_package(tree: ast.AST, forbidden_prefix: str) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == forbidden_prefix or alias.name.startswith(forbidden_prefix + "."):
                    lines.append(node.lineno)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (node.module == forbidden_prefix or node.module.startswith(forbidden_prefix + "."))
        ):
            lines.append(node.lineno)
    return lines


def check_no_cross_imports(src_root: Path = SRC_ROOT, sim_root: Path = SIM_ROOT) -> list[str]:
    violations: list[str] = []
    for path in _iter_python_files(src_root):
        tree = _parse(path)
        for lineno in _imports_forbidden_package(tree, "ogsim"):
            violations.append(f"{path}:{lineno}: opengrid module imports ogsim (forbidden, BUILD.md S1)")
    for path in _iter_python_files(sim_root):
        tree = _parse(path)
        for lineno in _imports_forbidden_package(tree, "opengrid"):
            violations.append(f"{path}:{lineno}: ogsim module imports opengrid (forbidden, BUILD.md S1)")
    return violations


def main() -> int:
    violations = check_no_duplicate_core_formulas() + check_no_cross_imports()
    if violations:
        print("dupcheck FAILED:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1
    print("dupcheck OK: no duplicated core formulas, no ogsim<->opengrid cross-imports.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
