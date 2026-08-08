"""Guards on the deployable artifact.

These do not test behavior -- `test_mandate_core.py` does that. They test the
properties that make `mandate_vault.py` deployable at all, each one standing in
for a failure that is invisible from the repo root and only shows up on-chain.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
CONTRACT = ROOT / "mandate_vault.py"
LIBRARIES = ("mandate_core", "mandate_prompts")


def _source() -> str:
    return CONTRACT.read_text(encoding="utf-8")


def test_contract_is_in_sync_with_libraries():
    """The checked-in contract matches what the build would produce.

    Without this, editing a library module and forgetting to rebuild ships a
    contract whose logic silently lags its own tests.
    """
    result = subprocess.run(
        [sys.executable, "build_contract.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_contract_imports_no_local_modules():
    """No `import mandate_core`-style import survives in the contract.

    This is the failure the whole inlining exists to prevent: such an import
    resolves fine from the repo root and fails on-chain, where neither the repo
    nor PYTHONPATH exists.
    """
    tree = ast.parse(_source())
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name in LIBRARIES]
        elif isinstance(node, ast.ImportFrom):
            if node.module in LIBRARIES or node.level:
                found.append(node.module or ".")
    assert not found, f"contract imports local modules: {found}"


def test_contract_is_pure_ascii():
    """The deploy path carries no raw non-ASCII bytes.

    Toolchain steps read the contract with the platform codec, which is cp1252
    on Windows and raises on bytes above 127.
    """
    raw = CONTRACT.read_bytes()
    offenders = sorted({b for b in raw if b > 127})
    assert not offenders, f"non-ASCII bytes in contract: {offenders}"


def test_contract_has_no_duplicate_top_level_definitions():
    """Inlining twice, or inlining a name the contract also defines, would
    shadow one definition with another and the last one would silently win."""
    tree = ast.parse(_source())
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"duplicate top-level definitions: {dupes}"


def test_contract_parses_and_pins_a_runner():
    """First line must carry the `Depends` pin the GenVM reads."""
    src = _source()
    ast.parse(src)
    first = src.splitlines()[0]
    assert first.startswith("# {") and "py-genlayer:" in first, first
