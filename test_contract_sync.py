"""Guards on the deployable artifact.

These do not test behavior -- `test_mandate_core.py` does that. They test the
properties that make `mandate_vault.py` deployable at all, each one standing in
for a failure that is invisible from the repo root and only shows up on-chain.
"""

from __future__ import annotations

import ast
import builtins
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


# Everything `from genlayer import *` is expected to supply. Anything else that
# the contract references without defining or importing is a missing import.
# Spelled out rather than probed, because the installed `genlayer` package here
# is a stub: importing it proves nothing about what the on-chain runner exports.
SDK_NAMES = frozenset({
    "gl",
    "Address",
    "DynArray",
    "TreeMap",
    "allow_storage",
    "u256",
})


def test_contract_has_no_unresolved_names():
    """Every free name in the contract resolves to a builtin, an import, a
    definition in the file, or a documented SDK export.

    This is the guard for the failure mode the inlining creates. `library_body`
    strips each module's import prologue, so the contract relies on one
    hand-written block covering the union. The test suite imports the library
    modules -- which still carry their own imports -- so a name missing only
    from the contract passes every behavioral test and then raises `NameError`
    on-chain, inside whichever method first touches it.
    """
    tree = ast.parse(_source())

    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Import):
            bound |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            bound |= {a.asname or a.name for a in node.names if a.name != "*"}
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)

    used = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }

    unresolved = sorted(used - bound - SDK_NAMES - set(dir(builtins)))
    assert not unresolved, (
        f"contract references names it never imports: {unresolved}. "
        "Add them to the import block in mandate_vault.py."
    )


def test_contract_prologue_covers_library_imports():
    """The contract imports at least what every inlined module imports.

    `test_contract_has_no_unresolved_names` catches a name that is actually
    referenced; this catches the same drift one step earlier, at the point where
    a library module gains an import, so the two failures read differently.
    """
    contract_bound = {
        (a.asname or a.name).split(".")[0]
        for node in ast.parse(_source()).body
        if isinstance(node, ast.Import)
        for a in node.names
    } | {
        a.asname or a.name
        for node in ast.parse(_source()).body
        if isinstance(node, ast.ImportFrom) and node.module != "__future__"
        for a in node.names
        if a.name != "*"
    }

    for name in LIBRARIES:
        tree = ast.parse((ROOT / f"{name}.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Import):
                for a in node.names:
                    want = (a.asname or a.name).split(".")[0]
                    assert want in contract_bound, f"{name} imports {want}, contract does not"
            elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
                for a in node.names:
                    want = a.asname or a.name
                    assert want in contract_bound, f"{name} imports {want}, contract does not"


def test_contract_parses_and_pins_a_runner():
    """First line must carry the `Depends` pin the GenVM reads."""
    src = _source()
    ast.parse(src)
    first = src.splitlines()[0]
    assert first.startswith("# {") and "py-genlayer:" in first, first
