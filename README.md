# MandateVault

A spending mandate an autonomous agent cannot argue with.

Agents are being handed payment authority, and that authority is always written
in prose -- "buy compute for training runs and datasets, nothing else" -- while
the enforcement is always a hard cap. A cap cannot tell a GPU lease from a gift
card. Closing that gap needs a judgment about *meaning* at the moment of
spending, and there is nowhere trustworthy to put it: an oracle cannot read
intent, and off-chain review means trusting the operator the mandate exists to
constrain.

MandateVault puts that judgment on-chain, and splits it from the arithmetic:

| | judges | how |
|---|---|---|
| the code | **quantity** -- per-transaction cap, rolling period cap, denylist, allowlist | deterministic, no LLM |
| the model | **kind** -- "does this purpose fall under clause N?" | one LLM call, only if the numbers cleared |

Every request is screened deterministically first, and most settle there. Only a
request that passes every limit and still needs a judgment about purpose reaches
a model, so nondet cost is at most **one LLM call per spend** regardless of how
elaborate the mandate is.

## Why this needs GenLayer

The mandate is prose, so enforcing it requires natural-language judgment. The
money is on-chain, so that judgment has to be verifiable by parties who do not
trust each other. Those two requirements together are the whole reason this is
an intelligent contract rather than a cap plus a webhook: a webhook puts the
operator back in the loop, and a cap cannot read a memo.

## Fail closed

A denial costs a round trip and an owner override. A wrongful approval costs
money that does not come back. So every ambiguous, malformed, or hostile model
response resolves to a denial, and `canonicalize_verdict` is **total** -- it has
no raising path and no input for which it fails to produce a verdict.

## Three stages of a request

1. **Deterministic screen** (`screen_request`) -- seven ordered checks:
   amount validity, denylist, per-transaction cap, rolling period cap, allowlist
   auto-approval, empty mandate, then escalate. Ordered so a generous
   `auto_approve_under` cannot widen a cap: both caps are checked *before* the
   allowlist is consulted.
2. **Clause matching** (escalated requests only) -- one `exec_prompt` asking
   whether the stated purpose falls under one of the numbered active clauses.
3. **Coercion and settlement** -- the model's `clause_id` is checked against the
   mandate's own active ids, confidence is clamped and thresholded, and the
   result becomes an `approved`/`denied` outcome with a stable reason code.

## Consensus model

Validators **re-run the whole leader computation** -- including a real LLM call
of their own -- and then compare field by field:

- `decision` and `clause_id` -- exactly
- `confidence` -- within `confidence_tol`, and **only for approvals**

Nothing is trusted from the leader beyond the values being compared, per
GenLayer's rule that the leader's result is never trusted input. Confidence is
deliberately not compared on denials: the number changes no outcome there, so
comparing it would manufacture disagreement without buying safety.

## Prompt-injection containment

The memo is attacker-controlled text. Three properties bound what it can do:

1. **The model is asked to perceive, not to decide.** It never sees an amount, a
   balance, a cap, or a party name. Memo text has no lever to pull even if the
   model obeys it, because nothing in the prompt connects an answer to money.
2. **Untrusted spans are length-capped and fenced with per-call delimiters**
   derived from the request digest. A requester cannot close a fence they cannot
   predict. The salt is derived, not random, because every validator must build
   a byte-identical prompt.
3. **Clause text and request text are fenced separately and labelled with their
   trust level.** Clauses are owner-authored and only change through an
   owner-gated write. A memo cannot pass itself off as mandate authority.

`clause_id` is coerced against the mandate's own ids, so a request cannot cite
authority that does not exist. The worst a hostile memo achieves is one
miscategorization *inside limits that were already fixed deterministically*.

This is defense-in-depth, **not** a substitute for greyboxing: an injection that
every validator's model obeys would reach consensus. Bounding the blast radius
is the durable property, not preventing agreement.

## Deploying

```bash
genlayer deploy --contract mandate_vault.py --args \
  '["Cloud compute and GPU rental for model training or inference.","Purchase or licensing of datasets used for training."]' \
  500000000 2000000000 604800 10000000 75 20
```

`--args` is **variadic and positional**, not a keyword object: the CLI parses each
value separately -- JSON arrays and objects are decoded, everything else is
coerced as a scalar (ints become BigInt). So the seven values above land in
constructor order, and passing one `'{"per_tx_cap": ...}'` object instead would
arrive as a single dict in `clauses` and be rejected. Because the arguments are
positional, an earlier one cannot be skipped to reach a later one -- set
`period_seconds` explicitly if you want to override `min_confidence`.

All amounts are **integer base units**. There are no floats anywhere in the
deterministic path -- a float would make two validators disagree in the last bit
and turn a rounding artifact into a consensus failure.

| # | parameter | meaning |
|---|---|---|
| 1 | `clauses` | the mandate, in prose, as a JSON array. At least one, each non-empty. |
| 2 | `per_tx_cap` | largest single spend |
| 3 | `period_cap` | largest total across one rolling window |
| 4 | `period_seconds` | window length. Default `604800` (7 days). |
| 5 | `auto_approve_under` | an allowlisted payee at or under this settles with no LLM call. `0` disables. |
| 6 | `min_confidence` | below this, a clause match is downgraded to a denial |
| 7 | `confidence_tol` | allowed leader/validator spread, approvals only |

## API

### Views

| method | returns |
|---|---|
| `mandate()` | active clauses with their real ids |
| `limits()` | the configured limits |
| `spent_in_period()` | approved total inside the current window |
| `remaining_in_period()` | `period_cap` minus the above, floored at zero |
| `total_spends()` | number of recorded decisions |
| `get_spend(spend_id)` | one recorded decision |
| `simulate(payee, amount)` | the deterministic screen only -- **no LLM call**, no state change |

`simulate` is the cheap pre-flight: an agent can check whether a spend would
clear the numbers before paying for a clause judgment.

### Writes

| method | who | notes |
|---|---|---|
| `add_clause(text)` | owner | returns the new clause id |
| `revoke_clause(clause_id)` | owner | leaves a hole; ids are never reused or renumbered |
| `set_agent(agent, allowed)` | owner | who may spend |
| `set_allowlist(payee, allowed)` | owner | eligible for `auto_approve_under` |
| `set_denylist(payee, blocked)` | owner | checked first, beats everything |
| `request_spend(payee, amount, memo)` | agent | the mandate check |

Revocation deliberately leaves a hole -- active ids can be `[0, 2, 5]`. Those
real ids are what the prompt shows the model, because renumbering them `0, 1, 2`
would invite the model to cite an id the coercion step then rejects, turning
every post-revocation approval into a silent denial.

### Reason codes

Stable strings, written into stored history and safe to branch on:

`amount_not_positive`, `payee_denylisted`, `exceeds_per_tx_cap`,
`exceeds_period_cap`, `auto_approved_allowlist`, `mandate_empty`,
`needs_review`, `clause_match`, `no_clause_match`

## Layout

This contract pins the **`py-genlayer`** runner, which loads one module, and
`genlayer deploy --contract` reads a single path and performs no bundling -- so a
local `import mandate_core` has nothing to resolve against on-chain. GenLayer
does offer a `py-genlayer-multi` runner for contracts packaged across several
files; this project keeps the single-module runner and inlines instead, so the
deployed bytes stay one reviewable artifact.

| file | role |
|---|---|
| `mandate_vault.py` | **the deployable artifact.** Contract code, plus the two libraries inlined between `INLINE` markers. |
| `mandate_core.py` | deterministic engine -- caps, window, screening, coercion. Source of truth. |
| `mandate_prompts.py` | prompt construction. The trust boundary. Source of truth. |
| `build_contract.py` | splices the libraries into the marked regions |
| `test_mandate_core.py` | behavior |
| `test_contract_sync.py` | guards on the artifact itself |

Edit the library modules, never the marked regions. Then:

```bash
python build_contract.py
```

`test_contract_sync.py` fails if the checked-in contract is stale, so drift
cannot reach a reviewer or a deployment unnoticed. It also guards the failures
that are invisible from the repo root and only show up on-chain: a surviving
local import, non-ASCII bytes in the deploy path, duplicate top-level
definitions, and a missing runner pin.

## Testing

```bash
python -m pytest -q              # 129 tests
python build_contract.py --check # fails if the artifact is stale
genvm-lint check mandate_vault.py
```

`genvm-lint check` passes: 3 lint checks, validation ok, 13 methods (7 view, 6
write), 7 constructor params. It reports one informational notice (`I200`) that a
newer runner is available than the pinned one; the pin is intentional and the
runner is upgraded deliberately, not on notice.

### What is not verified here

`genlayer-test` does not import in this environment -- the installed `genlayer`
package is a stub containing only `__init__.py`, and `import genlayer_test`
raises `ModuleNotFoundError`. So the direct-mode and integration suites have not
been run, which means:

- `request_spend` has **never executed against a live runtime**
- **validator agreement is unverified** -- `verdicts_agree` is unit-tested
  against constructed verdicts, but leader/validator convergence on real model
  output has not been observed

The deterministic engine, the prompt builder, the coercion layer, and the
artifact guards are all covered by the 129 passing tests. The nondet path is
covered by construction and reasoning only. That gap is the first thing to close
on a machine with a working GenLayer test harness.

## Design decisions

**Block time is parsed, not trusted to the host.** The runtime hands the block
time over as a *string* -- calldata decodes to `None`/`int`/`str`/`bytes`/`list`/
`dict`, with no datetime among them. `parse_block_time` pins a missing offset to
**UTC** rather than letting the host's local zone decide. Without that, two
validators in two zones derive epoch seconds hours apart, the same request lands
inside one node's rolling window and outside another's, and the period cap stops
being a consensus-safe number. It uses `calendar.timegm` on a UTC timetuple to
stay integer-only and avoid `.timestamp()`'s float round trip. Unparseable input
raises: every validator sees the same string and fails identically, whereas a
fallback time would silently corrupt window accounting on one node.

**The window is read lazily and newest-first.** Each stored spend is a storage
read, so `period_spent_newest_first` consumes a backwards generator and stops at
the first record older than the cutoff. Cost tracks spends *in the window*, not
the length of all history.

**Membership is a keyed lookup, not a scan.** The allowlist and denylist are
`TreeMap`s queried directly by normalized key. An earlier version materialized
each table into a frozenset on every request -- one storage read per key, to
answer two membership questions.

**The amount is absent from the prompt.** Every size question is settled
deterministically before the model is consulted, so showing it a number would add
attack surface -- "this is only a small amount" is a sentence a memo could write
-- while buying nothing.

**Malformed model output denies rather than forcing validator rotation.** The
GenLayer guidance is to return `False` from a validator on LLM error so the
network rotates. MandateVault instead collapses unusable output to a canonical
denial. This is the fail-closed trade taken deliberately: a denial is cheap and
reversible by the owner, and a total `canonicalize_verdict` means there is no
input for which the contract has no answer. The cost is that a genuinely
transient model failure is recorded as a denial rather than retried.

**Addresses are parsed behind a user-error guard.** `Address()` raises on
malformed input; unguarded, that surfaces as a runtime fault rather than a
rejected transaction, which reads to a caller as "the contract is broken"
instead of "that address is wrong".

## Known gaps

- Validator agreement is unverified against a live runtime (see above).
- Errors are raised as plain `gl.vm.UserError` without the
  `[EXPECTED]`/`[EXTERNAL]`/`[TRANSIENT]`/`[LLM_ERROR]` classification prefixes
  the GenLayer guidance describes. Every raise here is in fact an `[EXPECTED]`
  caller error, so the prefixes would be uniform, but they are not present.
- The pinned runner is one version behind the latest available.
- History grows without bound. Reads are window-bounded, so this costs storage
  rather than per-request time, but there is no pruning.

