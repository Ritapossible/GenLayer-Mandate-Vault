"""Behavioral coverage for `MandateVault.__init__`.

Every other behavioral test in this suite exercises `src/mandate_core.py`,
because that is where the deterministic engine lives. The constructor is the one
piece of real logic that exists *only* in the deployed artifact: it validates the
mandate and the limits before either reaches storage, and nothing under `src/`
can stand in for it. `test_contract_sync.py` asserts the constructor's *shape*
-- that its int parameters pass through a coercion helper -- but never runs it.

That gap was not theoretical. A deploy to GenLayer Studio
(tx `0x6b3acdf6623e01cbc10f9a857448ffc3c141b09d813e3ed3680780bafc1b4d27`) was
submitted with an empty constructor form. The decoded calldata was
`clauses=[]`, `per_tx_cap=0`, `period_cap=0` and the signature defaults for the
rest, and the GenVM returned `[EXPECTED] mandate must have at least one clause`.
The contract behaved exactly as designed -- but nothing here asserted that it
would, so the guard's correctness rested on reading it.

Importing the contract takes a stub SDK. The installed `genlayer` package is
empty (`dir(genlayer) == []`), which is why the rest of the artifact's
properties are asserted structurally over the AST in `test_contract_sync.py`.
The constructor, though, touches a small and well-understood slice of the SDK --
`gl.vm.UserError`, `gl.message.sender_address`, the two `gl.public` decorators
that run at class creation, and the storage type names -- so that slice is
stubbed and the real constructor is called against it.

The stub is deliberately not a GenVM emulator. Storage is stood up as plain
Python containers and `__init__` runs against a bare namespace, so what is under
test is the validation logic and the order it runs in, not the runner's storage
machinery. Anything outside that slice -- `gl.nondet`, `gl.eq_principle`,
`gl.storage` -- is wired to raise, so a constructor that started reaching into
the non-deterministic surface would fail here loudly rather than silently pass.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "contracts" / "mandate_vault.py"


# --- the stub SDK ---------------------------------------------------------


class _Unstubbed:
    """Attribute bag that fails loudly on anything the constructor should not
    touch, so the blast radius of the stub stays visible."""

    def __init__(self, name: str, **members: object):
        self._name = name
        self.__dict__.update(members)

    def __getattr__(self, attr: str):
        raise AssertionError(
            f"the constructor reached {self._name}.{attr}, which this stub does "
            "not provide -- these tests cover deterministic validation only"
        )


def _build_sdk() -> types.ModuleType:
    """A `genlayer` module exporting just what importing the contract needs."""

    class UserError(Exception):
        """Stands in for `gl.vm.UserError`, the classified rejection."""

    class Address:
        """Minimal stand-in: the constructor only stores `sender_address`."""

        def __init__(self, raw: str):
            if not isinstance(raw, str) or not raw.startswith("0x") or len(raw) != 42:
                raise ValueError(f"not an address: {raw!r}")
            self.as_hex = raw

        def __eq__(self, other: object) -> bool:
            return isinstance(other, Address) and other.as_hex == self.as_hex

        def __hash__(self) -> int:
            return hash(self.as_hex)

        def __repr__(self) -> str:
            return f"Address({self.as_hex})"

    class u256(int):
        """An int that refuses what a `u256` slot cannot hold.

        The real storage descriptor faults on an out-of-range write. Mirroring
        that here is what makes `test_caps_are_bounded_by_u256` meaningful: the
        contract's own `_require_u256` has to reject the value *before* this
        would, and if the guard were removed this stub would raise a bare
        `OverflowError` instead of a classified `UserError`.
        """

        def __new__(cls, value: object = 0):
            as_int = int(value)  # type: ignore[call-overload]
            if not 0 <= as_int <= (1 << 256) - 1:
                raise OverflowError(f"{as_int} does not fit a u256 slot")
            return super().__new__(cls, as_int)

    class DynArray(list):
        def __class_getitem__(cls, _item):
            return cls

    class TreeMap(dict):
        def __class_getitem__(cls, _item):
            return cls

    def allow_storage(cls):
        return cls

    def _undecorated(fn):
        """`gl.public.view` / `gl.public.write` only set attributes on the
        function; neither wraps it. Matching that keeps the calldata boundary
        honest -- a parameter really does arrive as whatever the caller sent."""
        return fn

    module = types.ModuleType("genlayer")
    module.gl = _Unstubbed(
        "gl",
        Contract=type("Contract", (), {}),
        vm=_Unstubbed("gl.vm", UserError=UserError),
        message=_Unstubbed("gl.message", sender_address=Address("0x" + "11" * 20)),
        public=_Unstubbed("gl.public", view=_undecorated, write=_undecorated),
    )
    module.Address = Address
    module.DynArray = DynArray
    module.TreeMap = TreeMap
    module.allow_storage = allow_storage
    module.u256 = u256
    return module


def _load_contract(sdk: types.ModuleType) -> types.ModuleType:
    """Import `contracts/mandate_vault.py` against the stub SDK.

    Loaded under its own module name and with the real (empty) `genlayer`
    restored afterwards, so nothing here leaks into the AST-based tests.
    """
    previous = sys.modules.get("genlayer")
    sys.modules["genlayer"] = sdk
    try:
        spec = importlib.util.spec_from_file_location("mandate_vault_under_test", CONTRACT)
        assert spec and spec.loader, f"cannot build an import spec for {CONTRACT}"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("genlayer", None)
        else:
            sys.modules["genlayer"] = previous


SDK = _build_sdk()
VAULT = _load_contract(SDK)

UserError = SDK.gl.vm.UserError
OWNER = SDK.gl.message.sender_address
EXPECTED = VAULT.ERROR_EXPECTED
MAX_CLAUSE_CHARS = VAULT.MAX_CLAUSE_CHARS
U256_MAX = VAULT.U256_MAX

VALID_CLAUSE = "Purchase compute for model training runs, including GPU leases."

# Every numeric constructor parameter, with a value that is valid for it, so a
# single parameter can be perturbed while the rest stay well-formed.
NUMERIC_PARAMS = {
    "per_tx_cap": 1_000,
    "period_cap": 5_000,
    "period_seconds": 604_800,
    "auto_approve_under": 100,
    "min_confidence": 75,
    "confidence_tol": 20,
}


class _Storage:
    """Stand-in for the contract instance.

    The runner builds storage slots from the class annotations before `__init__`
    runs; none of that machinery is what these tests are about. Everything the
    constructor writes is either a plain attribute or an append to one of these
    two sequences.
    """

    def __init__(self):
        self.clauses: list[str] = []
        self.revoked: list[bool] = []


def construct(**overrides: object) -> _Storage:
    """Run the real constructor against a storage stand-in.

    Only `clauses` and the two required caps are supplied, so every other
    parameter comes from the contract's own signature defaults rather than a
    copy of them restated here.
    """
    args: dict[str, object] = {
        "clauses": [VALID_CLAUSE],
        "per_tx_cap": NUMERIC_PARAMS["per_tx_cap"],
        "period_cap": NUMERIC_PARAMS["period_cap"],
    }
    args.update(overrides)
    store = _Storage()
    VAULT.MandateVault.__init__(store, **args)  # type: ignore[arg-type]
    return store


def rejects(**overrides: object) -> str:
    """Assert the constructor refuses these arguments, and return the reason.

    The classification is asserted on every rejection rather than in one place:
    an unprefixed failure is the single thing the error-class protocol exists to
    rule out, because a validator has no rule for comparing it.
    """
    with pytest.raises(UserError) as caught:
        construct(**overrides)
    message = str(caught.value)
    assert message.startswith(EXPECTED), (
        f"rejection is unclassified: {message!r} does not start with {EXPECTED}"
    )
    return message


# --- the deploy that actually failed --------------------------------------


def test_the_failed_studio_deploy_is_reproduced():
    """The exact calldata from the rejected deploy, decoded from the receipt.

    Pinned positionally and in full, because this is the whole argument vector
    the GenVM saw -- not a reconstruction of the one field that happened to
    fail first.
    """
    store = _Storage()
    with pytest.raises(UserError) as caught:
        VAULT.MandateVault.__init__(store, [], 0, 0, 604800, 0, 75, 20)

    assert str(caught.value) == f"{EXPECTED} mandate must have at least one clause"
    assert store.clauses == [] and store.revoked == [], (
        "a rejected deploy must write no state"
    )


# --- the mandate ----------------------------------------------------------


def test_clauses_must_not_be_empty():
    """A vault with no clauses can never approve anything, so it is refused at
    construction rather than deployed as an inert contract."""
    assert rejects(clauses=[]) == f"{EXPECTED} mandate must have at least one clause"


@pytest.mark.parametrize(
    "clauses",
    [
        pytest.param("Buy compute for training runs.", id="bare-string"),
        pytest.param(None, id="none"),
        pytest.param(42, id="int"),
        pytest.param({"0": "Buy compute."}, id="dict"),
        pytest.param(("Buy compute.",), id="tuple"),
    ],
)
def test_clauses_must_be_a_list(clauses):
    """Calldata decodes to `None | int | str | bytes | list | dict`, so a
    non-list is reachable -- notably a bare string, which is what the Studio
    deploy form sends when the field is typed as text instead of a JSON array.

    A string is the dangerous one: it is iterable, so without the `isinstance`
    check the loop below would happily store one clause per character.
    """
    assert rejects(clauses=clauses) == f"{EXPECTED} mandate must have at least one clause"


@pytest.mark.parametrize(
    "clause",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
        pytest.param("\t\n", id="tab-newline"),
        pytest.param(None, id="none"),
        pytest.param(7, id="int"),
        pytest.param(["nested"], id="list"),
    ],
)
def test_clause_text_must_be_a_non_empty_string(clause):
    """A blank clause authorizes nothing but still counts toward the mandate
    being non-empty, so it would defeat the check above."""
    assert rejects(clauses=[clause]) == f"{EXPECTED} clause text must be non-empty"


def test_a_bad_clause_is_caught_anywhere_in_the_list():
    """The scan covers every clause, not just the first."""
    assert rejects(clauses=[VALID_CLAUSE, VALID_CLAUSE, ""]) == (
        f"{EXPECTED} clause text must be non-empty"
    )


def test_clause_length_boundary():
    """`MAX_CLAUSE_CHARS` is inclusive.

    The bound is what keeps a mandate from growing past what the review prompt
    can carry, so both sides of it are pinned -- a change to the constant should
    have to come here and say so.
    """
    construct(clauses=["c" * MAX_CLAUSE_CHARS])
    assert rejects(clauses=["c" * (MAX_CLAUSE_CHARS + 1)]) == f"{EXPECTED} clause too long"


# --- the limits -----------------------------------------------------------


@pytest.mark.parametrize("field", sorted(NUMERIC_PARAMS))
@pytest.mark.parametrize(
    "value",
    [
        pytest.param("1000", id="numeric-string"),
        pytest.param(None, id="none"),
        pytest.param(1.5, id="float"),
        pytest.param([1000], id="list"),
        pytest.param(True, id="bool"),
    ],
)
def test_numeric_params_reject_non_integers(field, value):
    """Type is checked before value, and the failure stays classified.

    This is the ordering the constructor's own comment calls out: `validate()`
    compares (`per_tx_cap < 1`), and comparing a `str` to an `int` raises
    `TypeError` -- an unclassified VM error that no validator has a rule for.
    So the coercion has to run first, and `pytest.raises(UserError)` inside
    `rejects` is what pins that down.

    `True` is in the list because `bool` subclasses `int`: accepting one would
    let a cap of `True` read as a cap of exactly one base unit.
    """
    assert rejects(**{field: value}) == f"{EXPECTED} {field} must be an integer"


@pytest.mark.parametrize("field", sorted(NUMERIC_PARAMS))
def test_numeric_params_are_bounded_by_u256(field):
    """A value too wide for its slot is refused with a classified error.

    `validate()` has no opinion about width, so without this guard a cap of
    `2**256` would clear every range check and then fault inside `u256()` at the
    point of the write -- past the point where the failure can be explained.
    """
    assert rejects(**{field: U256_MAX + 1}) == f"{EXPECTED} {field} exceeds u256 range"
    construct(**{field: NUMERIC_PARAMS[field]})  # the valid value still passes


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        pytest.param({"per_tx_cap": 0}, "per_tx_cap must be >= 1", id="per-tx-zero"),
        pytest.param({"per_tx_cap": -1}, "per_tx_cap must be >= 1", id="per-tx-negative"),
        pytest.param({"period_cap": 0}, "period_cap must be >= 1", id="period-zero"),
        pytest.param({"period_cap": -5}, "period_cap must be >= 1", id="period-negative"),
        pytest.param({"period_seconds": 0}, "period_seconds must be >= 1", id="window-zero"),
        pytest.param(
            {"auto_approve_under": -1}, "auto_approve_under must be >= 0", id="auto-negative"
        ),
        pytest.param({"min_confidence": 101}, "min_confidence out of range", id="conf-high"),
        pytest.param({"min_confidence": -1}, "min_confidence out of range", id="conf-negative"),
        pytest.param({"confidence_tol": 101}, "confidence_tol out of range", id="tol-high"),
        pytest.param({"confidence_tol": -1}, "confidence_tol out of range", id="tol-negative"),
    ],
)
def test_limits_out_of_range_are_refused(overrides, reason):
    """`Limits.validate()` speaks `ValueError` because it is runtime-independent;
    the constructor restates it as a classified user error. This asserts the
    specific message survives that translation rather than being flattened."""
    assert rejects(**overrides) == f"{EXPECTED} {reason}"


def test_zero_caps_fail_even_with_a_valid_mandate():
    """The second half of the failed deploy.

    `clauses=[]` masked it, so fixing only the mandate would have surfaced this
    next. Worth its own test: a cap of zero is what an untouched deploy form
    sends, and it is the difference between a vault that denies everything and
    one that was configured.
    """
    assert rejects(clauses=[VALID_CLAUSE], per_tx_cap=0, period_cap=0) == (
        f"{EXPECTED} per_tx_cap must be >= 1"
    )


def test_auto_approve_under_may_exceed_the_per_tx_cap():
    """Deliberately unconstrained, per `Limits`: `screen_request` checks both
    caps before it consults the allowlist, so a generous threshold cannot widen
    a cap even when it is misconfigured. Pinned so a future "tightening" has to
    argue with this test rather than quietly reverse the reasoning."""
    store = construct(per_tx_cap=1_000, auto_approve_under=10_000)
    assert int(store.auto_approve_under) == 10_000


# --- the accepted deploy --------------------------------------------------


def test_valid_deploy_writes_the_mandate_and_the_limits():
    clauses = [VALID_CLAUSE, "Purchase datasets used for training or evaluation."]
    store = construct(
        clauses=clauses,
        per_tx_cap=500_000_000,
        period_cap=2_000_000_000,
        period_seconds=86_400,
        auto_approve_under=10_000_000,
        min_confidence=80,
        confidence_tol=15,
    )

    assert store.owner == OWNER, "the deployer is the owner"
    assert list(store.clauses) == clauses
    assert list(store.revoked) == [False, False], "clauses start live"
    assert int(store.per_tx_cap) == 500_000_000
    assert int(store.period_cap) == 2_000_000_000
    assert int(store.period_seconds) == 86_400
    assert int(store.auto_approve_under) == 10_000_000
    assert int(store.min_confidence) == 80
    assert int(store.confidence_tol) == 15


def test_stored_limits_are_narrowed_to_u256():
    """The fields are declared `u256`, so the constructor has to write that type
    and not the raw Python int it validated."""
    store = construct()
    for field in NUMERIC_PARAMS:
        assert isinstance(getattr(store, field), SDK.u256), f"{field} was not narrowed"


def test_signature_defaults_are_the_documented_ones():
    """A minimal deploy gets a 7-day window, no auto-approval, and the 75/20
    confidence pair -- the values the decoded Studio calldata showed the runner
    filling in. Changing them changes what an unspecified deploy means."""
    store = construct()
    assert int(store.period_seconds) == 604_800
    assert int(store.auto_approve_under) == 0
    assert int(store.min_confidence) == 75
    assert int(store.confidence_tol) == 20


def test_revoked_tracks_clauses_one_for_one():
    store = construct(clauses=[VALID_CLAUSE] * 5)
    assert len(store.revoked) == len(store.clauses) == 5
