"""Guards on the deployable artifact.

These do not test behavior -- `test_mandate_core.py` does that. They test the
properties that make `contracts/mandate_vault.py` deployable at all, each one
standing in for a failure that is invisible from the repo root and only shows up
on-chain -- or, in the case of `test_contracts_dir_holds_exactly_one_contract`,
one that shows up in a reviewer's validation report.
"""

from __future__ import annotations

import ast
import builtins
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "contracts" / "mandate_vault.py"
SRC = ROOT / "src"
BUILDER = ROOT / "tools" / "build_contract.py"
LIBRARIES = ("mandate_core", "mandate_prompts")


def _source() -> str:
    return CONTRACT.read_text(encoding="utf-8")


def test_contracts_dir_holds_exactly_one_contract():
    """`contracts/` contains one file and it is the vault.

    A validator pointed at this repo enumerates contract candidates by path. Any
    second `.py` under `contracts/` -- a helper, an `__init__.py`, a scratch copy
    -- becomes a candidate too, and since it declares no `gl.Contract` subclass
    the validation report carries an error against a file that was never meant to
    be a contract. The source set then reads as "not lint-clean" even though the
    deployable artifact is fine.

    That is a packaging failure, not a code failure, which is exactly why it
    needs a test: nothing about the contract's own correctness would catch it.
    """
    contracts_dir = ROOT / "contracts"
    found = sorted(p.name for p in contracts_dir.glob("*.py"))
    assert found == ["mandate_vault.py"], (
        f"contracts/ must hold exactly one file, the contract itself; found {found}. "
        "Helper modules belong in src/, build tooling in tools/, tests in tests/."
    )
    assert not [p for p in contracts_dir.rglob("*.py") if p.parent != contracts_dir], (
        "contracts/ must have no subdirectories containing Python files"
    )


def test_only_the_contract_declares_a_gl_contract():
    """No module outside `contracts/` subclasses `gl.Contract`.

    The mirror of the test above: that one keeps stray files out of the contract
    directory, this one keeps stray contract classes out of everywhere else. A
    `gl.Contract` subclass sitting in `src/` or `tools/` would make a scanner
    reasonably conclude the repo ships two contracts.
    """
    # Same skip set the submission sweep uses, imported rather than restated so
    # the two cannot drift. It matters most for a repo-local `.venv/`: without
    # it this walks site-packages and `ast.parse` raises on the first vendored
    # file that is not valid current-Python, turning a layout guard into a
    # spurious error on a reviewer's fresh clone.
    from verify_submission import SKIP

    offenders: list[str] = []
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if SKIP & set(rel.parts) or rel.parts[0] == "contracts":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                # `gl.Contract` -> Attribute(value=Name('gl'), attr='Contract')
                if isinstance(base, ast.Attribute) and base.attr == "Contract":
                    offenders.append(f"{rel.as_posix()}:{node.name}")
    assert not offenders, f"gl.Contract subclasses outside contracts/: {offenders}"


def test_contract_is_in_sync_with_libraries():
    """The checked-in contract matches what the build would produce.

    Without this, editing a library module and forgetting to rebuild ships a
    contract whose logic silently lags its own tests.
    """
    result = subprocess.run(
        [sys.executable, str(BUILDER), "--check"],
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
        tree = ast.parse((SRC / f"{name}.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Import):
                for a in node.names:
                    want = (a.asname or a.name).split(".")[0]
                    assert want in contract_bound, f"{name} imports {want}, contract does not"
            elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
                for a in node.names:
                    want = a.asname or a.name
                    assert want in contract_bound, f"{name} imports {want}, contract does not"


# The helpers every int- or bool-annotated public parameter must pass through.
# `_require_u256` is `_require_int` plus an upper bound, so either satisfies the
# int case; the bound itself is a storage concern, not a type concern.
INT_COERCERS = frozenset({"_require_int", "_require_u256"})
BOOL_COERCERS = frozenset({"_require_bool"})


def _public_methods(tree: ast.Module):
    """Yield the methods reachable from calldata: the constructor and anything
    carrying a `@gl.public.*` decorator."""
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef):
            continue
        for fn in cls.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if fn.name == "__init__":
                yield fn
                continue
            for dec in fn.decorator_list:
                # `gl.public.view` / `gl.public.write` -> Attribute(Attribute(Name))
                if (
                    isinstance(dec, ast.Attribute)
                    and isinstance(dec.value, ast.Attribute)
                    and dec.value.attr == "public"
                ):
                    yield fn
                    break


def _coercion_linenos(fn: ast.AST, name: str, wanted: frozenset[str]) -> list[int]:
    """Lines where `name` is rebound by a coercion call taking itself."""
    return [
        node.lineno
        for node in ast.walk(fn)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == name
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id in wanted
        and node.value.args
        and isinstance(node.value.args[0], ast.Name)
        and node.value.args[0].id == name
    ]


def test_public_int_and_bool_params_are_coerced():
    """Every int/bool parameter of a callable-from-calldata method passes
    through a coercion helper before anything else reads it.

    An annotation is documentation, not enforcement. The runner calls the method
    with decoded calldata as-is -- `meth2call(instance, *args, **kwargs)` -- and
    neither `@gl.public.view` nor `@gl.public.write` wraps the function, so an
    `amount: int` parameter arrives as whatever the caller encoded.

    Both failure modes this prevents are invisible from the repo root. An
    arithmetic comparison against a `str` raises `TypeError`, which escapes as an
    unclassified VM error -- the one thing the error-class protocol exists to
    rule out, since validators have no rule for comparing a failure that carries
    no prefix. And `BoolDesc.set` stores `1 if val else 0`, reading raw
    truthiness, so an unguarded `set_agent(addr, "false")` would *grant* that
    address spending authority.

    Asserted structurally rather than behaviorally because the claim is
    universal. `test_contract_runtime.py` can now execute the artifact against
    the real runner SDK and does check a handful of these rejections, but a
    behavioral test only ever covers the parameters someone remembered to
    write a case for. This one fails on a parameter nobody thought about --
    including one added later.

    String parameters are out of scope -- they are guarded too, but by
    `_parse_address`, length limits, and `isinstance` checks whose correct form
    differs per call site, so there is no single shape to assert.
    """
    tree = ast.parse(_source())
    problems: list[str] = []
    checked = 0

    for fn in _public_methods(tree):
        args = fn.args
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            if arg.arg == "self" or not isinstance(arg.annotation, ast.Name):
                continue
            if arg.annotation.id == "int":
                wanted = INT_COERCERS
            elif arg.annotation.id == "bool":
                wanted = BOOL_COERCERS
            else:
                continue

            checked += 1
            name = arg.arg
            reads = [
                node.lineno
                for node in ast.walk(fn)
                if isinstance(node, ast.Name)
                and node.id == name
                and isinstance(node.ctx, ast.Load)
            ]
            coerced_at = _coercion_linenos(fn, name, wanted)

            if not coerced_at:
                problems.append(
                    f"{fn.name}({name}: {arg.annotation.id}) is never coerced; "
                    f"expected `{name} = {sorted(wanted)[0]}({name}, ...)`"
                )
            elif reads and min(reads) < min(coerced_at):
                # The coercion call reads the parameter itself, so the earliest
                # read must be the coercion. An earlier one means something
                # touched the raw calldata value first.
                problems.append(
                    f"{fn.name}({name}) is read at line {min(reads)}, "
                    f"before it is coerced at line {min(coerced_at)}"
                )

    assert not problems, "unguarded calldata parameters:\n  " + "\n  ".join(problems)
    # A refactor that dropped the annotations would empty the loop and pass.
    assert checked >= 13, f"expected at least 13 int/bool params, found {checked}"


def test_coercion_helpers_raise_classified_errors():
    """The helpers fail as `[EXPECTED]` user errors, not bare exceptions.

    A guard that rejected bad calldata by raising `ValueError` would trade one
    unclassified failure for another and buy nothing -- and the test above would
    still pass, because it only checks that a coercion happened.
    """
    tree = ast.parse(_source())
    helpers = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in (INT_COERCERS | BOOL_COERCERS)
    }
    missing = sorted((INT_COERCERS | BOOL_COERCERS) - set(helpers))
    assert not missing, f"coercion helpers not defined in contract: {missing}"

    for name, fn in sorted(helpers.items()):
        raises = [n for n in ast.walk(fn) if isinstance(n, ast.Raise)]
        assert raises, f"{name} never raises"
        for node in raises:
            exc = node.exc
            assert (
                isinstance(exc, ast.Call)
                and isinstance(exc.func, ast.Attribute)
                and exc.func.attr == "UserError"
            ), f"{name} raises something other than gl.vm.UserError"
            msg = exc.args[0] if exc.args else None
            assert isinstance(msg, ast.JoinedStr) and any(
                isinstance(v, ast.FormattedValue)
                and isinstance(v.value, ast.Name)
                and v.value.id == "ERROR_EXPECTED"
                for v in msg.values
            ), f"{name} raises a message not prefixed with ERROR_EXPECTED"


def test_contract_parses_and_pins_a_runner():
    """First line must carry the `Depends` pin the GenVM reads."""
    src = _source()
    ast.parse(src)
    first = src.splitlines()[0]
    assert first.startswith("# {") and "py-genlayer:" in first, first
