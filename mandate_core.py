"""Deterministic spend screening and verdict canonicalization.

Pure Python. No GenLayer imports, no I/O, no floats. Every function here runs
identically on every validator, which is what lets the vault settle most
requests without spending a single LLM call -- and what makes the ones that do
reach a model verifiable.

Two rules shape the whole module.

**The code judges quantity; the model judges kind.** Every limit that involves a
number -- per-transaction cap, rolling period cap, auto-approve threshold -- is
enforced here, before any model is consulted. The model is never asked whether
an amount is reasonable and is never shown one. That split is what bounds the
damage hostile memo text can do: at worst it causes a miscategorization inside
limits that were already fixed deterministically.

**Ambiguity denies.** A denial costs a round trip and an owner override. A
wrongful approval costs money that does not come back. Every coercion path here
resolves toward denial, and `canonicalize_verdict` is total -- no input, however
malformed or hostile, raises.

Amounts are integers in the token's smallest base unit. Floats are avoided
deliberately: consensus compares these numbers for exact equality, and integer
arithmetic removes any question of cross-node rounding drift.
"""

from __future__ import annotations

import calendar
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

# --- outcomes -------------------------------------------------------------

APPROVED = "approved"
DENIED = "denied"
ESCALATE = "escalate"

OUTCOMES = (APPROVED, DENIED, ESCALATE)

# --- error classification -------------------------------------------------

# Validators have to reach consensus on failures, not just on successes, so a
# raised message carries a class prefix telling the validator how to compare it.
#
# The whole vocabulary is declared even though this contract only ever raises
# `[EXPECTED]`. The prefixes are a protocol shared with validator code rather
# than private strings: a validator that meets an `[EXTERNAL]` or `[TRANSIENT]`
# message needs the comparison rule already defined, not inferred at the point
# of failure. `[LLM_ERROR]` names the rule `validator_fn` already implements by
# returning False on unusable leader output -- disagree, and force rotation.
ERROR_EXPECTED = "[EXPECTED]"  # deterministic business logic -- must match exactly
ERROR_EXTERNAL = "[EXTERNAL]"  # external 4xx, deterministic -- must match exactly
ERROR_TRANSIENT = "[TRANSIENT]"  # network/5xx, non-deterministic -- both agree
ERROR_LLM = "[LLM_ERROR]"  # model misbehavior -- always disagree, force rotation

ERROR_PREFIXES = (ERROR_EXPECTED, ERROR_EXTERNAL, ERROR_TRANSIENT, ERROR_LLM)

# Reason codes are part of the public surface: consumers branch on them and they
# are written into stored history, so they are stable strings rather than
# free-form prose.
REASON_NOT_POSITIVE = "amount_not_positive"
REASON_DENYLISTED = "payee_denylisted"
REASON_PER_TX_CAP = "exceeds_per_tx_cap"
REASON_PERIOD_CAP = "exceeds_period_cap"
REASON_AUTO_ALLOWLIST = "auto_approved_allowlist"
REASON_NO_MANDATE = "mandate_empty"
REASON_NEEDS_REVIEW = "needs_review"
REASON_CLAUSE_MATCH = "clause_match"
REASON_NO_CLAUSE_MATCH = "no_clause_match"

REASONS = frozenset({
    REASON_NOT_POSITIVE,
    REASON_DENYLISTED,
    REASON_PER_TX_CAP,
    REASON_PERIOD_CAP,
    REASON_AUTO_ALLOWLIST,
    REASON_NO_MANDATE,
    REASON_NEEDS_REVIEW,
    REASON_CLAUSE_MATCH,
    REASON_NO_CLAUSE_MATCH,
})


# Widest value the contract's `u256` storage fields can hold. Amounts and
# timestamps arrive as unbounded Python ints -- calldata decodes integers at
# arbitrary precision -- so the boundary that writes them to storage has to
# check the range itself. Without that check an out-of-range value faults inside
# `u256()`, which is an unclassified failure validators cannot compare, instead
# of a classified rejection every node derives identically.
U256_MAX = (1 << 256) - 1


def normalize_address(text: str) -> str:
    """Canonical comparison form for an address.

    Case-folded and stripped so that a checksummed hex string and its lowercase
    spelling land in the same allowlist bucket. Not validated as an address --
    the contract layer owns that, using the SDK's own parser.
    """
    if not isinstance(text, str):
        return ""
    return text.strip().casefold()


def error_class(message: object) -> str:
    """The classification prefix on a raised message, or `""` if unprefixed.

    Total by construction: a non-string, or a message from code that predates
    the prefixes, classifies as `""` and is treated as unknown by
    `errors_agree`. An unclassified failure must never read as agreement.
    """
    if not isinstance(message, str):
        return ""
    for prefix in ERROR_PREFIXES:
        if message.startswith(prefix):
            return prefix
    return ""


def errors_agree(leader_msg: object, validator_msg: object) -> bool:
    """Whether two failed executions represent the same failure.

    The comparison rule per class, following the runner's own semantics:

    - `[EXPECTED]` / `[EXTERNAL]` are deterministic. Every honest node derives
      the same message from the same state, so they must match exactly.
    - `[TRANSIENT]` is not reproducible. Two nodes that both hit a transient
      failure agree that the call failed, without agreeing on the text.
    - `[LLM_ERROR]` and anything unclassified disagree, which forces rotation
      rather than freezing an unexplained failure into consensus.
    """
    leader_class = error_class(leader_msg)
    if leader_class != error_class(validator_msg):
        return False
    if leader_class in (ERROR_EXPECTED, ERROR_EXTERNAL):
        return leader_msg == validator_msg
    if leader_class == ERROR_TRANSIENT:
        return True
    return False


@dataclass(frozen=True)
class Limits:
    """Deterministic spending bounds.

    per_tx_cap         - largest single spend, base units
    period_cap         - largest total across one rolling window
    period_seconds     - window length
    auto_approve_under - allowlisted payee at or under this settles with no LLM
    min_confidence     - below this, a clause match is downgraded to a denial
    confidence_tol     - allowed leader/validator spread, approvals only

    `auto_approve_under` is deliberately not constrained against `per_tx_cap`:
    `screen_request` checks both caps before it consults the allowlist, so a
    generous threshold cannot widen a cap even if it is misconfigured.
    """

    per_tx_cap: int
    period_cap: int
    period_seconds: int
    auto_approve_under: int = 0
    min_confidence: int = 75
    confidence_tol: int = 20

    def validate(self) -> None:
        if self.per_tx_cap < 1:
            raise ValueError("per_tx_cap must be >= 1")
        if self.period_cap < 1:
            raise ValueError("period_cap must be >= 1")
        if self.period_seconds < 1:
            raise ValueError("period_seconds must be >= 1")
        if self.auto_approve_under < 0:
            raise ValueError("auto_approve_under must be >= 0")
        if not 0 <= self.min_confidence <= 100:
            raise ValueError("min_confidence out of range")
        if not 0 <= self.confidence_tol <= 100:
            raise ValueError("confidence_tol out of range")


@dataclass(frozen=True)
class Screen:
    """Result of deterministic screening.

    `outcome` of ESCALATE means the numbers cleared and only the question of
    kind remains -- the single case that costs an LLM call.
    """

    outcome: str
    reason: str

    def is_settled(self) -> bool:
        return self.outcome != ESCALATE


def parse_block_time(raw: object) -> int:
    """Consensus block time as whole epoch seconds, UTC.

    The runtime hands the block time over as a *string* -- the message is plain
    calldata, whose decoded types are None/int/str/bytes/list/dict, with no
    datetime among them. Parsing it is therefore the contract's job, and the
    timezone is the part that matters: a timestamp with no offset, read with the
    host's local zone, makes two validators in two zones derive epoch seconds
    that differ by hours. The same request would then land inside one node's
    rolling window and outside another's, and the period cap would stop being a
    consensus-safe number. A missing offset is pinned to UTC rather than left to
    the host.

    Integer-only, like the rest of this module: `calendar.timegm` on a UTC
    timetuple avoids `.timestamp()`'s float round trip entirely.

    An int is accepted as-is so a future runner that sends epoch seconds
    directly needs no change here. Anything unparseable raises -- every
    validator sees the same string, so they all fail identically, and inventing
    a fallback time would silently corrupt the window accounting instead.
    """
    if isinstance(raw, bool):
        raise ValueError("block time must not be a bool")
    if isinstance(raw, int):
        return raw
    if not isinstance(raw, str):
        raise ValueError(f"block time must be a string or int, got {type(raw).__name__}")

    text = raw.strip()
    if not text:
        raise ValueError("block time is empty")
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"

    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        # A bare integer-as-string is still an unambiguous instant.
        try:
            return int(text)
        except ValueError:
            raise ValueError(f"unparseable block time: {raw!r}") from None

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return calendar.timegm(moment.astimezone(timezone.utc).timetuple())


def period_spent_newest_first(entries: Iterable[tuple[int, int]], now: int, window: int) -> int:
    """Total approved spend in the window, from a newest-first stream.

    Separated from `period_spent` so a caller holding data in storage can feed
    entries lazily and stop reading as soon as one falls outside the window.
    That is what makes the cost below proportional to the window rather than to
    the whole history -- a list-taking version cannot deliver that, because
    building the list has already paid for every record.

    The window is half-open: an entry exactly at the cutoff has aged out.
    """
    if window <= 0:
        return 0
    cutoff = now - window
    total = 0
    for ts, amount in entries:
        if ts <= cutoff:
            break
        total += amount
    return total


def period_spent(history: list[tuple[int, int]], now: int, window: int) -> int:
    """Total approved spend inside the window ending at `now`.

    `history` is (timestamp, amount) in append order, which is also timestamp
    order, so this walks backwards and stops at the first entry that falls
    outside. Convenience wrapper over `period_spent_newest_first` for callers
    that already hold a full list.

    The window is half-open: an entry exactly at the cutoff has aged out.
    """
    return period_spent_newest_first(reversed(history), now, window)


def screen_request(
    *,
    amount: int,
    payee_denylisted: bool,
    payee_allowlisted: bool,
    spent_in_period: int,
    clause_count: int,
    limits: Limits,
) -> Screen:
    """Decide a request on deterministic grounds alone.

    Membership arrives as two booleans rather than two sets on purpose. The
    caller holds the lists in storage, where a set costs one read per entry;
    answering "is this payee listed?" needs exactly two keyed lookups, and
    taking sets here would force every request to pay for the whole table. The
    caller also owns normalization, so the key it looks up is the key it stored.

    Check order is load-bearing, not incidental:

    1. amount validity -- a nonpositive spend is meaningless
    2. denylist -- an explicit block outranks everything, including an
       allowlist entry for the same address
    3. per-transaction cap
    4. period cap
    5. allowlist auto-approve -- *after* both caps, so the fast path can never
       settle a spend that a limit would have refused
    6. empty mandate -- nothing to authorize against, so nothing is authorized
    7. escalate -- numbers clear, kind is still open

    Returning ESCALATE is the only path that costs an LLM call.
    """
    limits.validate()

    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        return Screen(DENIED, REASON_NOT_POSITIVE)

    if payee_denylisted:
        return Screen(DENIED, REASON_DENYLISTED)

    if amount > limits.per_tx_cap:
        return Screen(DENIED, REASON_PER_TX_CAP)

    if spent_in_period + amount > limits.period_cap:
        return Screen(DENIED, REASON_PERIOD_CAP)

    if payee_allowlisted and amount <= limits.auto_approve_under:
        return Screen(APPROVED, REASON_AUTO_ALLOWLIST)

    if clause_count <= 0:
        return Screen(DENIED, REASON_NO_MANDATE)

    return Screen(ESCALATE, REASON_NEEDS_REVIEW)


# --- verdict layer --------------------------------------------------------

INSIDE = "inside"
OUTSIDE = "outside"
DECISIONS = (INSIDE, OUTSIDE)

# Every denial collapses to this exact dict. Two nodes that both refuse -- for
# different reasons, from different malformed responses -- still produce
# byte-identical verdicts, so agreement on a denial is never in question.
DENY_VERDICT = {"decision": OUTSIDE, "clause_id": None, "confidence": 0}


def canonicalize_verdict(
    raw: str | dict,
    allowed_clause_ids: frozenset[int],
    limits: Limits,
) -> dict:
    """Coerce a model response into a bounded, canonical verdict.

    Total function: every malformed, hostile, or out-of-range response becomes a
    definite verdict rather than an exception, so leader and validators always
    agree on the coercion itself.

    The security property that matters: `clause_id` must name a clause that is
    actually in the mandate. A model cannot cite authority that does not exist,
    so memo text cannot invent a justification -- it can only fail to match one.

    Accepts a JSON string or an already-parsed dict, because
    `gl.nondet.exec_prompt(..., response_format="json")` returns a dict.
    """
    limits.validate()

    if isinstance(raw, dict):
        parsed = raw
    else:
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return dict(DENY_VERDICT)
    if not isinstance(parsed, dict):
        return dict(DENY_VERDICT)

    decision = parsed.get("decision")
    if not isinstance(decision, str) or decision.strip().casefold() not in DECISIONS:
        return dict(DENY_VERDICT)
    if decision.strip().casefold() == OUTSIDE:
        return dict(DENY_VERDICT)

    confidence = parsed.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, int):
        return dict(DENY_VERDICT)
    confidence = max(0, min(100, confidence))
    if confidence < limits.min_confidence:
        return dict(DENY_VERDICT)

    # A bool is rejected rather than coerced: `True == 1` is true in Python, so
    # accepting it would let a sloppy response silently cite clause 1.
    clause_id = parsed.get("clause_id")
    if isinstance(clause_id, bool) or not isinstance(clause_id, int):
        return dict(DENY_VERDICT)
    if clause_id not in allowed_clause_ids:
        return dict(DENY_VERDICT)

    return {"decision": INSIDE, "clause_id": clause_id, "confidence": confidence}


def encode_verdict(verdict: dict) -> str:
    """Byte-stable encoding, so nodes that agree semantically agree bytewise."""
    return json.dumps(verdict, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def verdicts_agree(mine: dict, theirs: dict, limits: Limits) -> bool:
    """Compare two verdicts field by field.

    Total, like `canonicalize_verdict`, and for the same reason: `theirs` is the
    leader's calldata, which is untrusted input. A leader that reports
    `confidence` as a string would otherwise raise `TypeError` inside the
    validator -- an unclassified fault, not a disagreement -- so a malformed
    verdict has to resolve to False rather than escape.

    Confidence is compared only for approvals. On a denial the number changes
    nothing -- the spend is refused either way -- so comparing it would
    manufacture disagreement without buying any safety. Approvals are where the
    number gates the outcome, so that is where the tolerance applies.
    """
    if not isinstance(mine, dict) or not isinstance(theirs, dict):
        return False

    decision = mine.get("decision")
    if decision not in DECISIONS or decision != theirs.get("decision"):
        return False
    if decision == OUTSIDE:
        return True

    # An approval, so both sides must carry a well-formed clause id. `True`
    # equals `1` in Python, so a bool is rejected before the comparison rather
    # than allowed to pass as a citation of clause 1.
    mine_id, their_id = mine.get("clause_id"), theirs.get("clause_id")
    for value in (mine_id, their_id):
        if isinstance(value, bool) or not isinstance(value, int):
            return False
    if mine_id != their_id:
        return False

    mine_conf, their_conf = mine.get("confidence"), theirs.get("confidence")
    for value in (mine_conf, their_conf):
        if isinstance(value, bool) or not isinstance(value, int):
            return False
    return abs(mine_conf - their_conf) <= limits.confidence_tol


def settle(screen: Screen, verdict: dict) -> tuple[str, str, int | None, int]:
    """Fold a screen and a verdict into the stored outcome.

    Returns (outcome, reason, clause_id, confidence). Kept here rather than in
    the contract so the mapping is unit-testable without a runtime.
    """
    if screen.is_settled():
        conf = 100 if screen.outcome == APPROVED else 0
        return (screen.outcome, screen.reason, None, conf)

    if verdict["decision"] == INSIDE:
        return (APPROVED, REASON_CLAUSE_MATCH, verdict["clause_id"], verdict["confidence"])

    return (DENIED, REASON_NO_CLAUSE_MATCH, None, 0)


# --- content addressing ---------------------------------------------------


def mandate_digest(clauses: list[str]) -> str:
    """Content address of the mandate, in clause order.

    Order is preserved rather than sorted because clause ids are positions: a
    reordered mandate is a different mandate. Consumers can watch this value to
    detect that the rules they were audited against have changed.
    """
    payload = json.dumps(list(clauses), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def request_salt(*, mandate: str, payee: str, amount: int, memo: str) -> str:
    """Per-request fence salt for prompt construction.

    Derived from request content rather than randomness, because every validator
    must build a byte-identical prompt. Unpredictable to the requester, since it
    commits to the mandate digest they do not control.
    """
    material = f"{mandate}|{normalize_address(payee)}|{amount}|{memo}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
