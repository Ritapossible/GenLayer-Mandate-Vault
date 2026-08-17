"""The deployed artifact, executed against the runner SDK it actually pins.

Every other test here reasons about the contract without running it.
`test_mandate_core.py` exercises the library modules, `test_contract_sync.py`
reads the artifact's AST, and `test_contract_constructor.py` runs `__init__`
against a hand-built stub whose storage is plain Python containers. None of
them touches the real storage layer, and that is exactly where the artifact was
broken:

    out.append((i, gl.storage.copy_to_memory(self.clauses[i])))

`DynArray[str]` hands back a decoded `str`, not a storage view -- `StrDesc.get`
returns an ordinary Python value. `copy_to_memory` reads `__type_desc__` off its
argument and asserts it is present, so on a primitive it raised
`AssertionError`, which is not a `gl.vm.UserError` and escaped as an
unclassified VM fault. Deployment itself succeeded; `mandate()` then failed the
moment GenLayer Studio's explorer read the freshly deployed contract, which is
what "genvm error on deploy" turned out to mean.

The stub in `test_contract_constructor.py` could not have caught it. It wires
`gl.storage` to raise on any access precisely because the constructor has no
business reaching the non-deterministic surface -- and the bug lives in
`_active_clauses`, which the constructor never calls.

So this file loads the real thing. `genvm-linter` already downloads the SDK for
the runner named in the artifact's `Depends` header and caches it under
`~/.cache/genvm-linter`; `genvm_linter.validate.sdk_loader.load_sdk` resolves
that header, puts the matching `py-lib-genlayer-std` on `sys.path`, and mocks
`_genlayer_wasi`. Storage runs on the SDK's own `InmemManager`, so slots,
descriptors, and record copying are the runner's real code paths rather than
stand-ins.

What this does *not* establish: there is no GenVM here. Consensus, gas, the
sandbox, and the actual model are absent, so `gl.vm.run_nondet_unsafe` and
`gl.nondet.exec_prompt` are substituted per test. What is under test is
everything between the calldata boundary and storage -- which is where a
storage-API misuse surfaces, and where nothing else in this suite was looking.

The module skips rather than fails when the SDK cannot be resolved (no
`genvm-linter`, or a first run with no network and a cold cache), so a checkout
without the tooling still runs the rest of the suite.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "contracts" / "mandate_vault.py"

OWNER_BYTES = bytes(range(20))
AGENT_BYTES = bytes(range(20, 40))
PAYEE = "0x" + "22" * 20
OTHER_PAYEE = "0x" + "44" * 20
BLOCKED_PAYEE = "0x" + "33" * 20

# Fixed so every deploy in this file lands at the same instant. The rolling
# window itself is covered exhaustively in `test_mandate_core.py`; what matters
# here is only that a stored `at` round-trips through a `u256` slot.
BLOCK_TIME = "2026-08-14T12:00:00+00:00"
# Derived here rather than through the contract's own parser, so a stored
# timestamp is checked against an independently computed instant.
BLOCK_TIME_EPOCH = int(
    datetime.datetime(2026, 8, 14, 12, 0, tzinfo=datetime.timezone.utc).timestamp()
)

CLAUSES = [
    "Cloud compute and GPU rental for model training or inference.",
    "Purchase or licensing of datasets used for training.",
]
PER_TX_CAP = 500_000_000
PERIOD_CAP = 2_000_000_000
PERIOD_SECONDS = 604_800
AUTO_APPROVE_UNDER = 10_000_000


# --- loading the artifact against the real SDK ----------------------------


def _purge_genlayer_modules() -> None:
    """Drop any `genlayer` already imported.

    The `genlayer` in site-packages is a zero-byte stub. If anything imported it
    first, the real SDK's `sys.path` entry would lose to the cached module and
    the contract would load against an empty namespace.
    """
    for name in [n for n in sys.modules if n == "genlayer" or n.startswith("genlayer.")]:
        del sys.modules[name]


class Runtime:
    """The loaded artifact plus the handles a test needs to drive it."""

    def __init__(self, module: types.ModuleType, gl, types_mod):
        self.module = module
        self.gl = gl
        self.Address = types_mod.Address
        self.UserError = gl.vm.UserError
        self.owner = self.Address(OWNER_BYTES)
        self.agent = self.Address(AGENT_BYTES)
        self.manager = None

    def bytes_used(self) -> int:
        """Total bytes across every slot of the current deployment.

        `InmemManager` keeps each slot as a `bytearray`, so summing their
        lengths is the storage footprint as the runner's own descriptors laid
        it out -- not an estimate from the field types.
        """
        assert self.manager is not None, "nothing deployed yet"
        return sum(len(memory) for _, memory in self.manager._parts.values())

    def set_sender(self, sender) -> None:
        """Rebind the message the contract reads.

        `gl.message` is a NamedTuple built once at import, so a test that needs
        a different caller replaces it wholesale rather than mutating it.
        """
        gl = self.gl
        gl.message_raw = dict(gl.message_raw, sender_address=sender, origin_address=sender)
        gl.message = gl.MessageType(
            contract_address=gl.message.contract_address,
            sender_address=sender,
            origin_address=sender,
            value=gl.message.value,
            chain_id=gl.message.chain_id,
        )

    def deploy(self, *args, **kwargs):
        """Run a real deployment: fresh storage, then the real `__init__`.

        A new `InmemManager` per call is what isolates one test's storage from
        the next -- the manager owns every slot, so replacing it is a wipe.
        """
        from genlayer.py.storage._internal.core import InmemManager
        from genlayer.py.storage.root import Root

        Root.MANAGER = self.manager = InmemManager()
        instance = Root.get().get_contract_instance(self.module.MandateVault)
        if not args and not kwargs:
            args = (CLAUSES, PER_TX_CAP, PERIOD_CAP, PERIOD_SECONDS, AUTO_APPROVE_UNDER)
        self.module.MandateVault.__init__(instance, *args, **kwargs)
        return instance


@pytest.fixture(scope="session")
def runtime() -> Runtime:
    """Load the artifact once per session.

    Once, because `gl.Contract.__init_subclass__` records the contract in a
    module global and refuses a second one -- re-executing the artifact raises
    "only one contract is allowed".
    """
    sdk_loader = pytest.importorskip(
        "genvm_linter.validate.sdk_loader",
        reason="the runner SDK comes from genvm-linter; see requirements.txt",
    )

    _purge_genlayer_modules()
    try:
        sdk_loader.load_sdk(CONTRACT)
    except Exception as exc:  # cold cache with no network, or a moved artifact
        pytest.skip(f"cannot resolve the pinned runner SDK: {exc}")

    import genlayer.gl as gl
    import genlayer.py.types as gltypes

    # `load_sdk` sets GENERATING_DOCS, which leaves `message`/`message_raw` as
    # placeholders because the real ones are decoded from fd 0. Supply them.
    owner = gltypes.Address(OWNER_BYTES)
    gl.message_raw = {
        "contract_address": gltypes.Address(bytes(20)),
        "sender_address": owner,
        "origin_address": owner,
        "stack": [],
        "value": 0,
        "datetime": BLOCK_TIME,
        "is_init": True,
        "chain_id": 61999,
        "entry_kind": 0,
        "entry_data": b"",
        "entry_stage_data": None,
    }
    gl.message = gl.MessageType(
        contract_address=gl.message_raw["contract_address"],
        sender_address=owner,
        origin_address=owner,
        value=gltypes.u256(0),
        chain_id=gltypes.u256(61999),
    )

    spec = importlib.util.spec_from_file_location("mandate_vault_on_sdk", CONTRACT)
    assert spec and spec.loader, f"cannot build an import spec for {CONTRACT}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return Runtime(module, gl, gltypes)


@pytest.fixture
def vault(runtime: Runtime):
    """A freshly deployed contract, with the owner as caller."""
    runtime.set_sender(runtime.owner)
    return runtime.deploy()


@pytest.fixture
def no_model(runtime: Runtime, monkeypatch: pytest.MonkeyPatch):
    """Make any model call an error.

    A deterministic path that quietly started consulting the model would be a
    real regression -- the whole cost argument rests on it not happening -- so
    the failure is made loud rather than left to a call count.
    """
    import genlayer.gl.nondet as nondet
    import genlayer.gl.vm as vm

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a deterministic path reached the model")

    monkeypatch.setattr(nondet, "exec_prompt", forbidden)
    monkeypatch.setattr(vm, "run_nondet_unsafe", forbidden)


@pytest.fixture
def model(runtime: Runtime, monkeypatch: pytest.MonkeyPatch):
    """Substitute the consensus round, running both halves of it.

    The leader function runs, and its result is handed to `validator_fn`
    wrapped in `gl.vm.Return` exactly as the runner does. The validator's answer
    is recorded rather than discarded, so a test can assert the two halves
    actually agreed instead of only that the outcome looked right.
    """
    import genlayer.gl.nondet as nondet
    import genlayer.gl.vm as vm

    calls: dict[str, object] = {"prompts": [], "agreed": None}

    def run_nondet_unsafe(leader_fn, validator_fn, /):
        leader_result = leader_fn()
        calls["agreed"] = validator_fn(vm.Return(leader_result))
        return leader_result

    monkeypatch.setattr(vm, "run_nondet_unsafe", run_nondet_unsafe)

    def install(response):
        def exec_prompt(prompt, **_kwargs):
            calls["prompts"].append(prompt)
            return response

        monkeypatch.setattr(nondet, "exec_prompt", exec_prompt)
        return calls

    return install


@pytest.fixture
def split_round(runtime: Runtime, monkeypatch: pytest.MonkeyPatch):
    """Run the consensus round with the two halves seeing different things.

    The `model` fixture feeds one response to both halves, which is the honest
    case and by construction can never show a leader/validator split. Here the
    leader's calldata is supplied directly -- it is whatever a leader *claims*,
    not something this code computed -- while `exec_prompt` answers the
    validator's own independent re-run. That is the shape of the round the
    rejection was about, and the only way to exercise `validator_fn` against a
    leader that did not run this contract's coercion.

    There is no GenVM here, so a `False` vote cannot actually rotate anything;
    the stub returns the leader's calldata regardless. The vote is therefore
    what these tests assert on, with the stored outcome recorded alongside it to
    show what agreement would have committed.
    """
    import genlayer.gl.nondet as nondet
    import genlayer.gl.vm as vm

    calls: dict[str, object] = {"agreed": None}

    def install(*, leader_claims, validator_sees):
        calldata = (
            leader_claims if isinstance(leader_claims, str) else json.dumps(leader_claims)
        )

        def run_nondet_unsafe(leader_fn, validator_fn, /):
            calls["agreed"] = validator_fn(vm.Return(calldata))
            return calldata

        def exec_prompt(prompt, **_kwargs):
            return validator_sees

        monkeypatch.setattr(vm, "run_nondet_unsafe", run_nondet_unsafe)
        monkeypatch.setattr(nondet, "exec_prompt", exec_prompt)
        return calls

    return install


# --- the regression ------------------------------------------------------


def test_mandate_reads_back_from_storage(vault, runtime: Runtime):
    """The exact call that faulted on-chain.

    `_active_clauses` passed each clause through `gl.storage.copy_to_memory`,
    which asserts on a primitive. Deployment succeeded and this raised
    `AssertionError` -- an unclassified VM fault, not a rejected transaction.
    """
    mandate = runtime.module.MandateVault.mandate(vault)

    assert [c["text"] for c in mandate["clauses"]] == CLAUSES
    assert [c["id"] for c in mandate["clauses"]] == [0, 1]
    assert mandate["revoked_ids"] == []
    assert mandate["digest"] == runtime.module.mandate_digest(CLAUSES)


def test_every_argument_free_view_survives_a_fresh_deploy(vault, runtime: Runtime):
    """What the explorer does on its own, without being asked.

    Studio reads a newly deployed contract by calling its zero-argument views.
    A fault in any one of them reads to a user as "the deployment failed", so
    the whole set is swept rather than only the view a test happened to pick.
    """
    contract = runtime.module.MandateVault
    views = ("mandate", "limits", "spent_in_period", "remaining_in_period", "total_spends")

    results = {name: getattr(contract, name)(vault) for name in views}

    assert results["total_spends"] == 0
    assert results["spent_in_period"] == 0
    assert results["remaining_in_period"] == PERIOD_CAP
    assert results["limits"]["per_tx_cap"] == PER_TX_CAP


# --- storage-backed behaviour --------------------------------------------


def test_revocation_leaves_a_hole_in_the_stored_ids(vault, runtime: Runtime):
    """Ids are positions and are never renumbered, so active ids can be [0, 2].

    Asserted against real storage because the property is about what survives a
    write, not about the list comprehension that reads it back.
    """
    contract = runtime.module.MandateVault

    assert contract.add_clause(vault, "Travel for on-site model evaluation.") == 2
    contract.revoke_clause(vault, 1)

    mandate = contract.mandate(vault)
    assert [c["id"] for c in mandate["clauses"]] == [0, 2]
    assert mandate["revoked_ids"] == [1]


def test_simulate_screens_without_consulting_the_model(vault, runtime: Runtime, no_model):
    """The cheap pre-flight stays free."""
    screen = runtime.module.MandateVault.simulate(vault, PAYEE, 1_000)

    assert screen == {"outcome": "escalate", "reason": "needs_review", "needs_review": True}


def test_denylist_beats_the_allowlist_through_storage(vault, runtime: Runtime, no_model):
    """Both tables are keyed lookups, and the key has to match what was stored.

    A normalization mismatch between setter and reader would make a denylisted
    payee read as unlisted -- a bypass, and one no AST check can see.
    """
    contract = runtime.module.MandateVault
    contract.set_allowlist(vault, BLOCKED_PAYEE, True)
    contract.set_denylist(vault, BLOCKED_PAYEE, True)

    screen = contract.simulate(vault, BLOCKED_PAYEE.upper().replace("0X", "0x"), 1_000)
    assert screen["outcome"] == "denied"
    assert screen["reason"] == "payee_denylisted"


def test_allowlisted_spend_settles_and_is_recorded(vault, runtime: Runtime, no_model):
    """The fast path: approved deterministically, and written to history."""
    contract = runtime.module.MandateVault
    contract.set_allowlist(vault, PAYEE, True)

    result = contract.request_spend(vault, PAYEE, 5_000, "gpu hours")

    assert result["outcome"] == "approved"
    assert result["reason"] == "auto_approved_allowlist"
    assert result["clause_id"] is None

    stored = contract.get_spend(vault, result["id"])
    assert stored["payee"] == PAYEE
    assert stored["amount"] == 5_000
    assert stored["memo"] == "gpu hours"
    assert stored["clause_id"] is None
    assert stored["at"] == BLOCK_TIME_EPOCH  # parsed, then through a u256 slot
    assert contract.spent_in_period(vault) == 5_000
    assert contract.remaining_in_period(vault) == PERIOD_CAP - 5_000


def test_escalated_spend_reaches_consensus_and_cites_a_clause(
    vault, runtime: Runtime, model
):
    """The full write path, both halves of the consensus round included."""
    contract = runtime.module.MandateVault
    calls = model({"decision": "inside", "clause_id": 0, "confidence": 90})

    result = contract.request_spend(
        vault, OTHER_PAYEE, 50_000_000, "rent H100s for a training run"
    )

    assert result["outcome"] == "approved"
    assert result["reason"] == "clause_match"
    assert result["clause_id"] == 0
    assert result["confidence"] == 90
    assert calls["agreed"] is True, "leader and validator disagreed on identical output"

    stored = contract.get_spend(vault, result["id"])
    assert stored["clause_id"] == 0
    assert stored["confidence"] == 90

    prompt = calls["prompts"][0]
    assert "Allowed clause ids: [0, 1]" in prompt
    assert "50000000" not in prompt, "the amount must never reach the model"


def test_a_clause_id_outside_the_mandate_becomes_a_denial(vault, runtime: Runtime, model):
    """Fail closed: a model cannot cite authority that does not exist."""
    contract = runtime.module.MandateVault
    model({"decision": "inside", "clause_id": 99, "confidence": 100})

    result = contract.request_spend(vault, OTHER_PAYEE, 50_000_000, "something plausible")

    assert result["outcome"] == "denied"
    assert result["reason"] == "no_clause_match"
    assert result["clause_id"] is None
    assert contract.spent_in_period(vault) == 0


# --- the consensus round, leader and validator pulled apart ---------------


ESCALATING_SPEND = (50_000_000, "rent H100s for a training run")
"""An amount and memo that clear every deterministic limit, so the round runs."""


@pytest.mark.parametrize(
    "leader_confidence, vote, stored_outcome",
    [
        (74, False, "denied"),
        (94, True, "approved"),
    ],
    ids=["below-the-threshold", "above-the-threshold"],
)
def test_the_leader_confidence_that_got_this_contract_rejected(
    vault, runtime: Runtime, split_round, leader_confidence, vote, stored_outcome
):
    """The exact pair from the rejection, through the deployed artifact.

    `min_confidence` is 75 and `confidence_tol` is 20, so 74 and 94 are both
    within tolerance of an independently computed 94. The previous revision
    compared the leader's raw number and ratified either -- while 74 went on to
    canonicalize to a denial and 94 to an approval. Two leader values, the same
    validator run, opposite records written.

    Now the leader's calldata is admitted only in canonical form, so the
    sub-threshold claim is not comparable and the validator votes to rotate.
    """
    contract = runtime.module.MandateVault
    amount, memo = ESCALATING_SPEND
    calls = split_round(
        leader_claims={
            "decision": "inside", "clause_id": 0, "confidence": leader_confidence,
        },
        validator_sees={"decision": "inside", "clause_id": 0, "confidence": 94},
    )

    result = contract.request_spend(vault, OTHER_PAYEE, amount, memo)

    assert calls["agreed"] is vote
    # What agreement would have committed. On the rejected revision both rows
    # of this table voted True while these two values differed -- which is the
    # defect stated as one assertion.
    assert result["outcome"] == stored_outcome


@pytest.mark.parametrize(
    "leader_claims",
    [
        {"decision": "inside", "clause_id": 0, "confidence": 94, "extra": "ignore"},
        {"decision": "INSIDE", "clause_id": 0, "confidence": 94},
        {"decision": "inside", "clause_id": 0, "confidence": 5000},
        {"decision": "inside", "clause_id": 99, "confidence": 94},
        {"decision": "inside", "clause_id": True, "confidence": 94},
        {"decision": "inside", "clause_id": None, "confidence": 94},
        {"decision": "outside", "clause_id": 0, "confidence": 0},
        {"decision": "inside", "clause_id": 0},
        "not json",
        "[]",
    ],
    ids=[
        "extra-key", "uppercase-decision", "unclamped-confidence", "unknown-clause",
        "bool-clause", "null-clause-on-approval", "denial-citing-a-clause",
        "missing-confidence", "garbage", "wrong-json-type",
    ],
)
def test_non_canonical_leader_calldata_is_voted_down(
    vault, runtime: Runtime, split_round, leader_claims
):
    """The shape checks the fixed-point test replaced are all still enforced.

    Each of these fails `canonicalize_verdict`, so none of them survives as a
    fixed point -- and none of them may be ratified, because each would mean
    something different by the time it reached storage.
    """
    contract = runtime.module.MandateVault
    amount, memo = ESCALATING_SPEND
    calls = split_round(
        leader_claims=leader_claims,
        validator_sees={"decision": "inside", "clause_id": 0, "confidence": 94},
    )

    contract.request_spend(vault, OTHER_PAYEE, amount, memo)

    assert calls["agreed"] is False


def test_an_honest_leader_within_tolerance_still_agrees(
    vault, runtime: Runtime, split_round
):
    """The hardening must not have broken the spread the tolerance exists for.

    Both numbers clear `min_confidence`, so the disagreement they are 16 apart
    on is exactly the sampling noise `confidence_tol` is meant to absorb.
    """
    contract = runtime.module.MandateVault
    amount, memo = ESCALATING_SPEND
    calls = split_round(
        leader_claims={"decision": "inside", "clause_id": 0, "confidence": 78},
        validator_sees={"decision": "inside", "clause_id": 0, "confidence": 94},
    )

    result = contract.request_spend(vault, OTHER_PAYEE, amount, memo)

    assert calls["agreed"] is True
    assert result["outcome"] == "approved"
    assert result["confidence"] == 78, "the stored number is the leader's"


def test_two_honest_denials_agree_without_comparing_confidence(
    vault, runtime: Runtime, split_round
):
    """A refusal is a refusal; the number gates nothing once the answer is no."""
    contract = runtime.module.MandateVault
    amount, memo = ESCALATING_SPEND
    calls = split_round(
        leader_claims={"decision": "outside", "clause_id": None, "confidence": 0},
        validator_sees={"decision": "outside", "clause_id": None, "confidence": 88},
    )

    result = contract.request_spend(vault, OTHER_PAYEE, amount, memo)

    assert calls["agreed"] is True
    assert result["outcome"] == "denied"
    assert result["reason"] == "no_clause_match"


def test_a_denial_is_still_written_to_history(vault, runtime: Runtime, no_model):
    """A refused spend is recorded, and does not consume the period cap."""
    contract = runtime.module.MandateVault

    result = contract.request_spend(vault, PAYEE, PER_TX_CAP + 1, "too large")

    assert result["outcome"] == "denied"
    assert result["reason"] == "exceeds_per_tx_cap"
    assert contract.total_spends(vault) == 1
    assert contract.spent_in_period(vault) == 0


# --- the calldata boundary, against the real storage layer ----------------


def test_an_unauthorized_sender_is_refused_as_a_classified_error(
    vault, runtime: Runtime, no_model
):
    """Owner-or-agent, and the refusal carries a comparable class prefix.

    The payee is allowlisted first so the authorized half of this test settles
    deterministically -- the subject here is who may call, not what the model
    would have said.
    """
    contract = runtime.module.MandateVault
    contract.set_allowlist(vault, PAYEE, True)
    runtime.set_sender(runtime.agent)

    with pytest.raises(runtime.UserError) as excinfo:
        contract.request_spend(vault, PAYEE, 1_000, "unauthorized")

    assert excinfo.value.message.startswith(runtime.module.ERROR_EXPECTED)

    runtime.set_sender(runtime.owner)
    contract.set_agent(vault, runtime.agent.as_hex, True)
    runtime.set_sender(runtime.agent)

    approved = contract.request_spend(vault, PAYEE, 1_000, "now authorized")
    assert approved["id"] == 0
    assert approved["reason"] == "auto_approved_allowlist"


@pytest.mark.parametrize(
    "amount",
    ["500", None, True, (1 << 256)],
    ids=["str", "none", "bool", "too-wide-for-u256"],
)
def test_a_bad_amount_is_refused_before_it_reaches_storage(
    vault, runtime: Runtime, no_model, amount
):
    """The guard the classification protocol exists for.

    Unguarded, each of these faults somewhere downstream -- a `TypeError` in a
    comparison, or an overflow inside `u256()` on the way to a slot -- and
    escapes with no class prefix for a validator to compare.
    """
    contract = runtime.module.MandateVault

    with pytest.raises(runtime.UserError) as excinfo:
        contract.request_spend(vault, PAYEE, amount, "memo")

    assert excinfo.value.message.startswith(runtime.module.ERROR_EXPECTED)
    assert contract.total_spends(vault) == 0


def test_a_truthy_non_bool_cannot_grant_authority(vault, runtime: Runtime):
    """`BoolDesc.set` writes `1 if val else 0`, so it takes any object.

    Without the boundary guard, `set_agent(addr, "false")` would *grant* that
    address spending authority. This is the one place that can be shown against
    the descriptor that actually does the writing.
    """
    contract = runtime.module.MandateVault

    with pytest.raises(runtime.UserError):
        contract.set_agent(vault, runtime.agent.as_hex, "false")

    runtime.set_sender(runtime.agent)
    with pytest.raises(runtime.UserError):
        contract.request_spend(vault, PAYEE, 1_000, "should not be authorized")


def test_the_calldata_boundary_takes_a_decoded_address(
    vault, runtime: Runtime, no_model
):
    """The form the CLI actually sends, which the contract used to refuse.

    GenLayer calldata has a native address type and `genlayer` CLI emits it by
    default -- a bare 40-hex argument auto-detects and is encoded `addr#...`, so
    the runner passes an `Address`, not a `str`. `_parse_address` guarded on
    `isinstance(raw, str)` alone, so the first live `request_spend` rolled back
    with `[EXPECTED] payee must be a string` against a valid address, and every
    address-taking method was unreachable from the standard tooling.

    Every other test in this suite passes hex strings, which is exactly why the
    defect survived them. This one passes the decoded object.
    """
    contract = runtime.module.MandateVault
    payee = runtime.Address(bytes(range(40, 60)))

    contract.set_allowlist(vault, payee, True)
    result = contract.request_spend(vault, payee, 5_000, "gpu hours")

    assert result["outcome"] == "approved"
    assert result["reason"] == "auto_approved_allowlist"

    stored = contract.get_spend(vault, result["id"])
    assert stored["payee"] == payee.as_hex


def test_an_address_reaches_the_same_key_in_either_spelling(
    vault, runtime: Runtime, no_model
):
    """A table written with one spelling must read back under the other.

    `_listed` keys on `normalize_address(addr.as_hex)`, so a decoded `Address`
    and its hex text have to land in the same bucket. If they did not, a payee
    denylisted through the CLI would read as unlisted when named as a string --
    a bypass, and the quietest possible one.
    """
    contract = runtime.module.MandateVault
    blocked = runtime.Address(bytes(range(60, 80)))

    contract.set_denylist(vault, blocked, True)  # written as an Address

    screen = contract.simulate(vault, blocked.as_hex, 1_000)  # read as a string
    assert screen["outcome"] == "denied"
    assert screen["reason"] == "payee_denylisted"


def test_a_malformed_address_is_a_rejected_transaction(vault, runtime: Runtime):
    """`Address()` raises its own exception; unguarded that is a VM fault."""
    with pytest.raises(runtime.UserError) as excinfo:
        runtime.module.MandateVault.simulate(vault, "not-an-address", 1_000)

    assert excinfo.value.message.startswith(runtime.module.ERROR_EXPECTED)


# --- what a stored spend actually costs ----------------------------------


SPEND_RECORD_OVERHEAD = 181
"""Fixed bytes per stored `Spend`, everything but its variable-length strings.

Two addresses, four `u256` slots, a bool, and the bookkeeping the `DynArray`
and the record descriptor add around them.
"""


@pytest.mark.parametrize("memo_chars", [0, 40, 200, 1000, 2000])
@pytest.mark.parametrize("settles_as", ["approved", "denied"])
def test_a_stored_spend_costs_its_overhead_plus_its_strings(
    vault, runtime: Runtime, no_model, memo_chars, settles_as
):
    """The figure the README quotes, against the real storage layout.

    A number in a README that nothing regenerates goes stale silently, so the
    claim is made falsifiable here: change the `Spend` record and this fails
    with the new cost rather than the README quietly becoming wrong.

    The shape is what matters for capacity planning. Every variable-length
    field is stored verbatim and charged byte for byte -- the memo, and also
    `outcome` and `reason`, which is why a denial is not uniformly cheaper than
    an approval but merely differently worded. Denials are recorded at full
    price on purpose: the audit trail is the reason the contract exists.
    """
    contract = runtime.module.MandateVault
    contract.set_allowlist(vault, PAYEE, True)
    memo = "x" * memo_chars
    amount = 5_000 if settles_as == "approved" else PER_TX_CAP + 1

    before = runtime.bytes_used()
    for _ in range(10):
        result = contract.request_spend(vault, PAYEE, amount, memo)
    per_spend = (runtime.bytes_used() - before) / 10

    assert result["outcome"] == settles_as
    assert per_spend == (
        SPEND_RECORD_OVERHEAD + memo_chars + len(result["outcome"]) + len(result["reason"])
    )
    assert contract.total_spends(vault) == 10


def test_the_worst_case_spend_is_the_one_the_readme_budgets_for(
    vault, runtime: Runtime, no_model
):
    """2212 bytes: the memo at its cap, under the longest reason code.

    `auto_approved_allowlist` is the longest string `settle` can write, so this
    is the ceiling a daily storage budget has to be divided by.
    """
    contract = runtime.module.MandateVault
    contract.set_allowlist(vault, PAYEE, True)
    longest_reason = max(runtime.module.REASONS, key=len)

    before = runtime.bytes_used()
    result = contract.request_spend(vault, PAYEE, 5_000, "x" * runtime.module.MAX_MEMO_CHARS)

    assert result["reason"] == longest_reason == "auto_approved_allowlist"
    assert runtime.bytes_used() - before == 2212


def test_a_deploy_writes_383_bytes_for_the_documented_mandate(runtime: Runtime):
    """The baseline the per-spend figure sits on top of.

    Measured for the two-clause mandate the README's deploy command uses, so
    the two numbers quoted there come from the same configuration.
    """
    runtime.set_sender(runtime.owner)
    runtime.deploy(CLAUSES, PER_TX_CAP, PERIOD_CAP, PERIOD_SECONDS, AUTO_APPROVE_UNDER)

    assert sum(len(c) for c in CLAUSES) == 113
    assert runtime.bytes_used() == 383


def test_a_misconfigured_deploy_fails_at_construction(runtime: Runtime):
    """The constructor's own guards, against the real storage instance."""
    runtime.set_sender(runtime.owner)

    with pytest.raises(runtime.UserError) as excinfo:
        runtime.deploy([], PER_TX_CAP, PERIOD_CAP)

    assert excinfo.value.message.startswith(runtime.module.ERROR_EXPECTED)
