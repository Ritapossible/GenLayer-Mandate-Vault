"""Reproduce a reviewer's validation sweep over the whole repository.

The submission requirement is not just "the contract lints clean" -- it is that
the *source set* is unambiguous: a validator enumerating Python files must find
exactly one contract candidate. A previous submission of a sibling project was
rejected because a helper module sitting beside the contract was picked up as a
candidate and reported "no contract class found" against a file that never
declared one.

So this does not lint `contracts/mandate_vault.py` and stop. It runs
`genvm-lint check` over *every* `.py` file in the repository and asserts the
shape of the result:

    exactly one file reports ok=true, and it is contracts/mandate_vault.py

A helper that starts passing is as much a failure as the contract that stops --
the first means something outside `contracts/` now looks like a contract.

Usage:

    python tools/verify_submission.py            # human-readable report
    python tools/verify_submission.py --json     # machine-readable

Exit code is 0 only when the invariant holds, so this is safe to gate a
submission on.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import sys
import sysconfig

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTRACT = "contracts/mandate_vault.py"

# Directory names that hold no reviewable source. A repo-local virtualenv is the
# one that matters: `pip install -r requirements.txt` into `.venv/` drops
# thousands of third-party files, some of which do not parse as current Python.
# Walking into it would be slow here and would raise `SyntaxError` in the layout
# guard that imports this set.
SKIP = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
        ".venv", "venv", "env", ".env", "node_modules", "site-packages",
        "build", "dist", ".tox", ".eggs"}


def find_linter() -> str:
    """Locate the `genvm-lint` executable.

    `pip install genvm-linter` drops the entry point in the interpreter's
    scripts directory, which is not always on PATH -- notably on Windows, where
    a `pip install` into a pythoncore install leaves `genvm-lint.exe` in a
    `Scripts` folder the shell does not search. Fall back to that path rather
    than telling the user the tool is missing when it is installed.
    """
    found = shutil.which("genvm-lint")
    if found:
        return found

    scripts = pathlib.Path(sysconfig.get_path("scripts"))
    for name in ("genvm-lint.exe", "genvm-lint"):
        candidate = scripts / name
        if candidate.exists():
            return str(candidate)

    sys.exit(
        "genvm-lint not found. Install the validator with:\n"
        "    pip install -r requirements.txt"
    )


def python_files() -> list[pathlib.Path]:
    """Every `.py` file a reviewer would see, repo-relative and sorted."""
    return sorted(
        p for p in ROOT.rglob("*.py")
        if not SKIP & set(p.relative_to(ROOT).parts)
    )


def check(linter: str, path: pathlib.Path) -> dict:
    """Run `genvm-lint check --json` on one file and return its verdict.

    The linter reports findings through its JSON payload rather than its exit
    code -- a file with errors still exits 0 -- so `ok` is read from the payload.
    A run that produces no parseable JSON is treated as not-ok rather than
    crashing the sweep, since an unreadable verdict is not a passing one.
    """
    rel = path.relative_to(ROOT).as_posix()
    proc = subprocess.run(
        [linter, "check", str(path), "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"file": rel, "ok": False,
                "detail": (proc.stdout + proc.stderr).strip()[:200] or "no output"}

    errors = [
        f"{e.get('code')}: {e.get('msg')}"
        for section in ("lint", "validate")
        for e in (payload.get(section) or {}).get("errors", [])
    ]
    validate = payload.get("validate") or {}

    return {
        "file": rel,
        "ok": bool(payload.get("ok")),
        "contract": validate.get("contract"),
        "methods": validate.get("methods"),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    linter = find_linter()
    results = [check(linter, p) for p in python_files()]
    passing = [r["file"] for r in results if r["ok"]]

    problems: list[str] = []
    if passing != [CONTRACT]:
        extra = [f for f in passing if f != CONTRACT]
        if CONTRACT not in passing:
            # An SDK that will not load is an environment problem, not a verdict
            # on the contract. On a cold cache the first check downloads the SDK
            # for the pinned runner, and reporting a failed download as "the
            # contract does not validate" would send a reviewer to debug code
            # that is fine.
            row = next(r for r in results if r["file"] == CONTRACT)
            if any("Failed to load SDK" in e for e in row.get("errors") or []):
                problems.append(
                    f"could not validate {CONTRACT}: the GenVM SDK for the pinned "
                    "runner failed to load. This is an environment or network "
                    "problem, not a contract error -- check connectivity and retry, "
                    "or pre-fetch with `genvm-lint download`."
                )
            else:
                problems.append(f"{CONTRACT} does not pass genvm-lint check")
        if extra:
            problems.append(
                f"files outside the contract path also validate as contracts: {extra}"
            )

    if args.json:
        print(json.dumps(
            {"ok": not problems, "contract_source": CONTRACT,
             "problems": problems, "results": results},
            indent=2,
        ))
        return 1 if problems else 0

    print(f"GenVM validation sweep - {len(results)} Python files\n")
    for r in sorted(results, key=lambda r: (not r["ok"], r["file"])):
        if r["ok"]:
            print(f"  PASS  {r['file']}")
            print(f"        contract={r['contract']}  methods={r['methods']}")
        else:
            reason = "; ".join(r.get("errors") or []) or r.get("detail") or "no contract class"
            print(f"  n/a   {r['file']}")
            print(f"        not a contract ({reason})")
    print()

    if problems:
        for p in problems:
            print(f"FAIL: {p}")
        return 1

    print(f"OK: exactly one contract source in this repository - {CONTRACT}")
    print("Every other Python file is development tooling and is never deployed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
