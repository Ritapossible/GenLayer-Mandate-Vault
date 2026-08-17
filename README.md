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

## Contract source

**The intelligent contract is `contracts/mandate_vault.py`, and it is the only
one.** `contracts/` holds that single file and nothing else.

Every other Python file in this repository is development tooling -- the library
modules the contract is generated from, the generator, and the tests. None of
them declares a `gl.Contract`, none is deployable, and none is submitted as a
contract:

| path | contains a contract? |
|---|---|
| `contracts/mandate_vault.py` | **yes** -- `class MandateVault(gl.Contract)`, 13 public methods |
| `src/`, `tools/`, `tests/` | no -- helper modules, build tooling, test suite |

Two tests in `tests/test_contract_sync.py` hold that line: one fails if any file
other than the vault appears under `contracts/`, the other fails if a
`gl.Contract` subclass appears anywhere outside it. Run
`python tools/verify_submission.py` to reproduce the validation sweep -- see
[Validation evidence](#validation-evidence).

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
- `confidence` -- within `confidence_tol`, and **only for approvals**, and only
  once both numbers are already inside the approval bucket

Nothing is trusted from the leader beyond the values being compared, per
GenLayer's rule that the leader's result is never trusted input. Confidence is
deliberately not compared on denials: the number changes no outcome there, so
comparing it would manufacture disagreement without buying safety.

### Compare after coercion, never before

The leader's calldata is admitted only in **canonical** form -- a value
`canonicalize_verdict` leaves unchanged (`canonical_leader_verdict`). That is
the same coercion the agreed result passes through on its way to storage, so
"the validator agreed" and "this is what gets written" are the same statement.
Anything else is not comparable and the validator votes to rotate.

This is load-bearing, and getting it wrong is what got an earlier revision of
this contract rejected. `confidence` was compared as the leader reported it,
*before* the `min_confidence` threshold. With a minimum of 75 and a tolerance of
20, a leader reporting **74** and a leader reporting **94** are both within
tolerance of an independently computed 94 -- so the validator ratified either,
and the contract then stored a denial for the first and an approval for the
second. Same request, same validator run, opposite records.

Two changes close it, and either would have:

- `canonical_leader_verdict` rejects a leader value the coercion would move, so
  a sub-threshold claim never reaches the comparison.
- `verdicts_agree` applies `confidence_tol` strictly inside the approval bucket
  -- both numbers must already clear `min_confidence`. The tolerance absorbs
  sampling spread among answers that all mean "approve"; it must never carry
  agreement across the line that decides whether the spend happens.

Both are kept, so the invariant does not depend on a single call site
remembering to canonicalize first. The rule generalizes past this contract: when
a validator's comparison sits on one side of a threshold and the stored value
sits on the other, tolerance leaks across the boundary. Compare the bucket the
outcome is decided in, not the raw number.

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

POSIX shells -- bash, zsh, Git Bash, WSL:

```bash
genlayer deploy --contract contracts/mandate_vault.py --args \
  '["Cloud compute and GPU rental for model training or inference.","Purchase or licensing of datasets used for training."]' \
  500000000 2000000000 604800 10000000 75 20
```

Windows PowerShell 5.1 needs both the `.cmd` shim named explicitly and the
stop-parsing token -- see below for why:

```powershell
genlayer.cmd --% deploy --contract contracts/mandate_vault.py --args "[\"Cloud compute and GPU rental for model training or inference.\",\"Purchase or licensing of datasets used for training.\"]" 500000000 2000000000 604800 10000000 75 20
```

`genlayer.cmd`, not `genlayer`: PowerShell resolves the bare name to npm's
`genlayer.ps1`, which forwards `$args` into its own `node` call. That second hop
re-quotes the array after `--%` has stopped applying, so the token silently
fails to help. `cmd.exe` takes the same escaped argument without `--%`, and
PowerShell 7 passes the POSIX form through unchanged.

`--args` is **variadic and positional**, not a keyword object: the CLI parses each
value separately -- JSON arrays and objects are decoded, everything else is
coerced as a scalar (ints become BigInt). So the seven values above land in
constructor order, and passing one `'{"per_tx_cap": ...}'` object instead would
arrive as a single dict in `clauses` and be rejected. Because the arguments are
positional, an earlier one cannot be skipped to reach a later one -- set
`period_seconds` explicitly if you want to override `min_confidence`.

That per-value decode is also what makes the clause array shell-sensitive. The
CLI runs `JSON.parse` on each argument and **forwards anything that fails to
parse as a scalar string** rather than reporting it, so the array only arrives
as an array if its double quotes reach the CLI intact. Windows PowerShell 5.1
hands a native command `'["a","b"]'` as `[a,b]` -- the quotes are consumed as
delimiters -- and the constructor then sees one string where it wanted a list:

```
[EXPECTED] clauses must be an array of strings, got str
```

That is the diagnosis, not a mandate problem: the clauses were typed, the shell
ate the quotes. `[EXPECTED] mandate must have at least one clause` is the other
failure -- a deploy that genuinely supplied none, which is what an empty Studio
deploy form sends.

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

### Deployed instance

The command above, run against GenLayer Studio. **This is the submitted
deployment**, and it carries the fix described under [Compare after coercion,
never before](#compare-after-coercion-never-before):

| | |
|---|---|
| network | `studionet` -- GenLayer Studio Network, chain id `61999` |
| contract | `0xdAf45c47d23e62de5F9423939b86358F150485b9` |
| deploy tx | `0xf387eb19e3be2bbf1ba946122459e16828eaba217e2ed90b052b9829f9279d54` |
| owner | `0xaA34e14a0e0B2fdD8Ad10F06bC0907fA0b1D02Bd` |
| mandate digest | `71762c397f33980e614ba2db36440eaaafcfb9fa8b9c32eb4a9daf45d3bd044f` |

The deploy settled `ACCEPTED` / `MAJORITY_AGREE` in a single round, three of five
round validators voting `AGREE` and none rotating.

Deployed with the two example clauses above and the limits in the same command,
read back and confirmed on-chain:

```bash
genlayer call 0xdAf45c47d23e62de5F9423939b86358F150485b9 mandate
genlayer call 0xdAf45c47d23e62de5F9423939b86358F150485b9 limits
```

`limits` reads back `min_confidence: 75` and `confidence_tol: 20` -- the exact
configuration in which the rejected revision would ratify a leader confidence of
74 and then store the spend as denied.

The digest is the mandate's content address. It changes if a clause is added or
revoked, so the value above pins the mandate this contract was deployed with --
watch it to detect that the rules you were audited against have moved.

#### Prior deployment (superseded)

`0x2334E39DFB3b3746A412b24455B630e5FC711239`, deploy tx
`0x37d21153db91acc65d8dfbc2e1796ec1b775033fa731595efade794d1943b8aa`. Kept for
provenance only: its `validator_fn` compares the leader's raw `confidence`
before the `min_confidence` threshold, so it carries the defect that got the
submission rejected. **Do not treat it as current.** Its mandate digest is
identical to the one above -- the digest content-addresses clause text, and no
clause changed, so it does not distinguish the two and the contract address is
what tells them apart.

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

| path | role |
|---|---|
| `contracts/mandate_vault.py` | **the deployable artifact, and the only contract.** Contract code, plus the two libraries inlined between `INLINE` markers. |
| `src/mandate_core.py` | deterministic engine -- caps, window, screening, coercion. Source of truth. |
| `src/mandate_prompts.py` | prompt construction. The trust boundary. Source of truth. |
| `tools/build_contract.py` | splices the libraries into the marked regions |
| `tools/verify_submission.py` | lints every `.py` and asserts only the vault is a contract |
| `tests/test_mandate_core.py` | behavior |
| `tests/test_contract_sync.py` | guards on the artifact itself, and on the layout |
| `tests/test_contract_constructor.py` | `__init__` against a hand-built stub SDK |
| `tests/test_contract_runtime.py` | the artifact executed on the **real** pinned runner SDK |

The split between `contracts/` and everything else is load-bearing, not
cosmetic. A validator enumerating this repository finds exactly one candidate,
so it never lints a helper module and reports "no contract class found" against
a file that was never meant to declare one. The library modules stay out of
`contracts/` for that reason alone -- they are the contract's *sources*, not
contracts.

Edit the library modules, never the marked regions. Then:

```bash
python tools/build_contract.py
```

`tests/test_contract_sync.py` fails if the checked-in contract is stale, so drift
cannot reach a reviewer or a deployment unnoticed. It also guards the failures
that are invisible from the repo root and only show up on-chain: a surviving
local import, **a name the contract references but never imports**, non-ASCII
bytes in the deploy path, duplicate top-level definitions, and a missing runner
pin.

That second guard exists because the inlining creates a specific trap.
`tools/build_contract.py` strips each library module's import prologue, and the
contract carries one hand-written block covering the union. The test suite
imports `mandate_core.py` and `mandate_prompts.py`, which still have their own
imports -- so a name missing only from the contract passes every behavioral test
and then raises `NameError` on-chain. The guard resolves every free name in the
generated artifact against builtins, its own definitions, its imports, and the
documented SDK exports.

## Testing

```bash
pip install -r requirements.txt

python -m pytest -q                             # 272 tests
python tools/build_contract.py --check          # fails if the artifact is stale
python tools/verify_submission.py               # lints every .py in the repo
genvm-lint check contracts/mandate_vault.py
genvm-lint typecheck contracts/mandate_vault.py --all
```

`genvm-lint check` passes: 3 lint checks, validation ok, 13 methods (7 view, 6
write), 7 constructor params. It reports one informational notice (`I200`) that a
newer runner is available than the pinned one; the pin is intentional and the
runner is upgraded deliberately, not on notice.

### Validation evidence

`genvm-lint check` on the contract alone would not answer the question a reviewer
actually asks, which is whether the *source set* is unambiguous. So
`tools/verify_submission.py` runs the validator over every `.py` file in the
repository and asserts the shape of the whole result -- exactly one file
validates as a contract, and it is `contracts/mandate_vault.py`. It exits
non-zero otherwise, including if a helper module ever starts passing.

The contract's own report, `genvm-lint check contracts/mandate_vault.py --json`:

```json
{"ok":true,"lint":{"ok":true,"passed":3},"validate":{"ok":true,
 "contract":"MandateVault","methods":13,"view_methods":7,"write_methods":6,
 "ctor_params":7}}
```

That is the submitted contract, and it validates. The sweep below is the
corroborating half -- it shows that nothing else in the repository is a second
candidate. Recorded with `genvm-linter==0.11.0`:

```
GenVM validation sweep - 7 Python files

  PASS  contracts/mandate_vault.py
        contract=MandateVault  methods=13
  n/a   src/mandate_core.py
        not a contract (E101: Failed to load SDK: No module named 'genlayer.py')
  n/a   src/mandate_prompts.py
        not a contract (E101: Failed to load SDK: No module named 'genlayer.py')
  n/a   tests/test_contract_sync.py
        not a contract (E101: Failed to load SDK: No module named 'genlayer.py')
  n/a   tests/test_mandate_core.py
        not a contract (E101: Failed to load SDK: No module named 'genlayer.py')
  n/a   tools/build_contract.py
        not a contract (E101: Failed to load SDK: No module named 'genlayer.py')
  n/a   tools/verify_submission.py
        not a contract (E101: Failed to load SDK: No module named 'genlayer.py')

OK: exactly one contract source in this repository - contracts/mandate_vault.py
Every other Python file is development tooling and is never deployed.
```

**The per-file message on the six tooling rows is environment-dependent, and the
assertion is not on that text.** Pointing a *contract* validator at a file that
is not a contract is an error by construction; which error depends on how far
the validator gets. With no SDK resolved for those files -- they carry no
`Depends` header -- it reports `E101: Failed to load SDK`, as above. On a machine
with an SDK already cached it gets one step further and reports `E105: no
contract class found`. Both mean the same thing, and neither is a defect in
those files: they are build tooling and tests, they were never contracts, and
they are not part of the submitted contract source set.

What `verify_submission.py` asserts is the *shape* of the sweep -- exactly one
`PASS`, and it is `contracts/mandate_vault.py` -- so it holds either way. It
exits non-zero if the vault stops validating or if anything else starts.

`genvm-lint typecheck` runs Pyright against the SDK the linter downloads for the
pinned runner, so it resolves `gl.*` for real rather than treating it as `Any`.
`--all` disables the linter's own suppressions; the artifact is clean under it.
That is a type check, not an execution -- see below for what it does and does not
establish.

### What is not verified here

There are two copies of the SDK in play here, and conflating them would overstate
what has been checked. The *importable* `genlayer` in `site-packages` is a
zero-byte `__init__.py`, and `import genlayer_test` raises `ModuleNotFoundError`,
so the direct-mode and integration suites shipped with the SDK have not been run.
But `genvm-lint` downloads the **real** SDK for the pinned runner into
`~/.cache/genvm-linter`, and that copy is executable: `test_contract_runtime.py`
resolves it through `genvm_linter.validate.sdk_loader`, mocks `_genlayer_wasi`,
and runs the artifact on the SDK's own `InmemManager`. Slots, type descriptors,
and record copying are the runner's real code paths.

That closes the gap this section used to describe, and it closed it by finding
something. `_active_clauses` passed each clause through
`gl.storage.copy_to_memory`, which asserts on `__type_desc__` and therefore
faults on a primitive -- and `DynArray[str]` returns a decoded `str`. Deployment
succeeded; the first view call raised `AssertionError`, an unclassified VM fault,
the moment the Studio explorer read the contract. Nothing that reasons about the
artifact could have seen it: the AST guards check names and shapes, and the
constructor test's stub wires `gl.storage` to raise on any access.

What remains unverified is the part that needs a network, not a runtime:

- **consensus is simulated, not observed.** `test_contract_runtime.py` runs both
  halves of the round -- the leader function, then `validator_fn` against
  `gl.vm.Return(leader_result)` exactly as the runner passes it. Its `model`
  fixture feeds one response to both halves, which is the honest case; its
  `split_round` fixture supplies the leader's calldata directly while
  `exec_prompt` answers the validator's own re-run, so the two halves can be
  driven apart on purpose. That second fixture exists because the first one
  cannot fail: a round where both halves see the same response can never show a
  leader/validator split, and the confidence-threshold defect above lived
  exactly there. What neither can do is run two nodes against two real model
  calls, so convergence on genuine model output is still unobserved.
- **no GenVM.** Gas, the sandbox, cloudpickle serialization of the leader and
  validator closures, and greyboxing are all absent.
- **no model.** `gl.nondet.exec_prompt` is substituted per test, so the prompt is
  asserted rather than answered.

One item that used to sit on that list was settled by reading the SDK rather than
by running it. `simulate`, `spent_in_period` and `remaining_in_period` all reach
`_now()`, which reads `gl.message_raw["datetime"]`, and it was unclear whether a
read-only call carries a block time at all. It does: `MessageRawType` declares
`datetime: str` as a required key, `message_raw` is decoded once from fd 0 for
every entry kind, and the neighbouring `stack: list[Address]` field is documented
as the stack of *view* method calls -- so views travel through the same message as
writes. There is no path that omits it.

A passing type check is also what caught nothing here: it is a guard against
signature drift, not evidence the consensus logic is right. The one diagnostic it
did raise was a narrowing failure in `verdicts_agree`, where a loop-based guard
was correct at runtime but unprovable to the checker; that guard is now written
per-variable so the checker can follow it.

That the environment cannot execute the contract is exactly why the artifact
guards carry the weight they do. A missing import in the deployed file was live
in this repo and invisible to every behavioral test, because the tests import
the library modules rather than the artifact -- `test_contract_sync.py` now
resolves the artifact's names statically instead.

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

The validator path keeps one sharp edge by design: when the leader's *calldata*
is malformed, the validator returns `False` to force rotation rather than
ratifying the failure -- the leader is untrusted, and nothing it emits may be
folded into consensus without being re-derived. `verdicts_agree` is total so
that a malformed verdict resolves to that `False` instead of raising a
`TypeError` inside the validator, which would be an unclassified fault no node
could compare.

**Addresses are parsed behind a user-error guard.** `Address()` raises on
malformed input; unguarded, that surfaces as a runtime fault rather than a
rejected transaction, which reads to a caller as "the contract is broken"
instead of "that address is wrong".

**Raised messages carry a classification prefix.** Validators have to reach
consensus on failures, not only on successes, so every `gl.vm.UserError` is
prefixed `[EXPECTED]` and `errors_agree` defines the comparison rule per class:
`[EXPECTED]`/`[EXTERNAL]` must match exactly, `[TRANSIENT]` agrees without
matching text, and `[LLM_ERROR]` or anything unclassified disagrees so the
network rotates rather than freezing an unexplained failure into consensus. The
full vocabulary is declared even though this contract only raises `[EXPECTED]`,
because the prefixes are a protocol shared with validator code.

The corollary is that no path may fail *un*classified. That is why `_now()`
restates a parser `ValueError` as `[EXPECTED]` and range-checks the result, and
why `request_spend` rejects an amount too wide for `u256`: calldata decodes
integers at arbitrary precision, and a value that faulted inside `u256()` on the
way to storage would be exactly the unclassified failure validators cannot
compare.

**Every int and bool parameter is coerced before it is read.** A parameter
annotation is documentation, not enforcement. The runner resolves the method and
calls it with the decoded calldata as-is -- `meth2call(instance, *args, **kwargs)`
-- and neither `@gl.public.view` nor `@gl.public.write` wraps the function; both
only set attributes on it. So an `amount: int` parameter arrives as whatever the
caller encoded, and calldata decodes to `None`, `int`, `str`, `bytes`, `bool`,
`Address`, `list` or `dict`. Two distinct failures follow, and the second is the
worse one:

- **Unclassified faults.** `"500" > cap` raises `TypeError` and `None < 0` raises
  `TypeError`, escaping as a VM error with no prefix -- precisely what the
  classification protocol exists to prevent, since validators have no rule for
  comparing a failure that carries no class.
- **Silent authorization.** `BoolDesc.set` stores `1 if val else 0`, reading raw
  truthiness of any object. Unguarded, `set_agent(addr, "false")` would *grant*
  that address spending authority, and `set_denylist(payee, "")` would unblock a
  payee the owner meant to block.

So `_require_int`, `_require_u256` and `_require_bool` sit at the boundary and
restate a type mismatch as `[EXPECTED]`. A bool is refused where an int is wanted
rather than widened, because `True == 1` in Python would otherwise make
`request_spend(payee, True, memo)` read as a one-base-unit spend. The line drawn
is between *representable* and *not representable*: an amount of `0` or `-5` is
representable, so it is recorded as a denial reasoned `amount_not_positive`, while
`"5"` cannot be interpreted at all and is refused. `simulate` applies the same
guard as `request_spend`, since a view that accepted input the write path rejects
would be a misleading preview of the call it exists to predict.

`test_contract_sync.py` asserts this structurally -- every int/bool parameter of
every calldata-reachable method must be rebound through a helper before any other
read of it, and the helpers must raise `[EXPECTED]`. The property is checked on
the AST rather than by execution because the importable `genlayer` here is a stub,
so this file is the only place it can be checked at all.

## Known gaps

- Validator agreement is unverified against a live runtime (see above).
- **History grows without bound.** Reads are window-bounded, so this costs
  storage rather than per-request time, but there is no pruning.

  Measured against the pinned runner's own storage descriptors, one recorded
  spend costs exactly:

  ```
  181 bytes  +  len(memo)  +  len(outcome)  +  len(reason)
  ```

  Every variable-length field is stored verbatim and charged byte for byte, so
  a denial is not cheaper than an approval -- only differently worded. The
  ceiling is **2212 bytes**: a memo at the 2000-character `MAX_MEMO_CHARS` cap
  under `auto_approved_allowlist`, the longest reason code `settle` can write.
  A deploy of the two-clause mandate in the command above writes 383 bytes.

  Against the public Studio's 256 MiB daily storage budget that is ~121,000
  spends per day at the ceiling, or ~809,000 with a typical 120-character memo.
  The quota does not bind here, and the first write each UTC day is allowed
  regardless, so a contract cannot be locked out by it.

  `test_contract_runtime.py` asserts the formula, the 2212-byte ceiling, and the
  383-byte deploy, so these numbers fail loudly if the `Spend` record changes
  rather than going stale in this file.

  The obvious reduction -- storing `sha256(memo)` instead of the text, which
  would cut the ceiling roughly tenfold -- is deliberately not taken. A stored
  hash proves which memo was submitted only to someone who still holds the
  original, and the party most likely to have discarded it is the one the
  mandate exists to constrain.

