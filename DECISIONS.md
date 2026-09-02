# DECISIONS

Running log of every non-obvious tradeoff, plus anything that broke and how it was
fixed. Newest phase at the bottom.

---

## Phase 1 - synthetic data with ground truth

### D1. Case counts are asserted, not just documented
The planted-case table lives in the module docstring of `generate_data.py`, and
`DatasetBuilder._verify()` asserts the emitted data matches it exactly. A comment
claiming "48 clean matches" is worthless if a later edit silently changes the mix.
The generator now fails loudly instead of quietly reporting different numbers than
the README.

### D2. `invoices.status` deliberately does not leak the answer
A real AR ledger has a `paid` flag, but that flag is an *output* of the very
reconciliation being measured. Emitting it would let any matcher score ~100% by
reading one column. Status is therefore only `open` / `overdue`, derived purely
from `due_date` vs the statement cutoff (2025-07-15).

### D3. 18 of 48 clean transactions quote a literal invoice id
Real remittance advice often carries the invoice number, so excluding it would make
the dataset unrealistically hard. But including it makes those matches nearly free,
so ground truth carries a `has_invoice_ref` column and the Phase 3 report is
required to state how many matches were won by a literal reference rather than by
inference. 24 transactions carry a reference in total (18 clean + 3 genuine
duplicates + the 3 artifact re-postings that copy their memo).

### D4. Duplicate convention: the earlier posting is the real one
Two identical credits posted the same day would make the ground truth an arbitrary
coin flip, and grading a matcher against a coin flip is not measurement. Every
artifact re-posting therefore lands 1-3 days after its original, and the earlier one
is true. This is a documented dataset convention, not a hint hidden in the text -
an agent is allowed to learn and exploit it, and the report should say so.

Three of the eight duplicate pairs quote the *same* invoice id in both memos, so a
naive "regex the invoice number out of the description" matcher produces a wrong
match rather than a free win.

### D5. Ambiguous convention: FIFO, and flagged as ambiguous
Both ambiguous scenarios are: one customer, two open invoices of the identical
amount, one payment with no reference quoted. The truth applies the payment to the
older invoice (FIFO, a real AR convention). A human holding only these two files
cannot do better than the convention either, so ground truth marks these rows
`ambiguous=yes` and the report is expected to count them as genuinely hard rather
than as free wins.

### D6. FX modelled as cross-currency, not same-currency drift
An invoice and a credit in the *same* currency differing by a rate has no physical
explanation. So FX cases are EUR/GBP invoices credited in USD, off the published
table rate by 0.4%-1.2% (correspondent bank spread). `data/fx_rates.json` ships the
static month-average rate table - a treasury desk hands you exactly this, so the
matcher is allowed to use it. INR appears in the table but has no invoices; an
unused rate row is realistic and harmless.

### D7. 17 no-invoice transactions, split into hard and obvious
11 are hard negatives: real customer names, receipt-shaped memos, plausible amounts,
but no invoice exists. 6 are obvious non-AR bank lines (fees, payroll, lease,
interest, card settlement, VAT refund), 3 of them negative-amount debits. Both kinds
occur on a real statement, and the split lets the report distinguish "rejected an
obvious debit" from "correctly refused a plausible-looking credit".

### D8. Statement is 120 rows against 100 invoices
The extra 20 rows come from partial instalments, duplicate re-postings and the
no-invoice lines. Padding the batch with easy non-AR rows to inflate throughput was
rejected - it would make records/sec look better while measuring nothing.

### Broke / fixed

**B1. Two NAME_NOISE rows were secretly clean.** The abbreviation style is a no-op on
a name with no abbreviatable word ("Kite & Compass Travel"), and the truncation style
is a no-op on a name shorter than the cut ("Palisade Security"). Both emitted a
counterparty string identical to the clean bank rendering, so the planted count of 12
name-noise cases was really 10. Fixed by `_pick_manglable()`, which draws a customer
until the chosen style actually changes the string, plus an assertion in `_verify()`
that no name-noise counterparty equals its invoice's clean name.

**B2. Unplanned ambiguity was possible.** Nothing stopped two randomly generated
invoices from landing on the same customer *and* the same amount, which would create
an ambiguous pair that ground truth records as two independent clean matches - the
scorer would then punish a matcher for a genuinely undecidable case. `_verify()` now
asserts the only same-(customer, amount) invoice pairs are the two planted AMBIGUOUS
ones. It passes on the shipped seed.

**B3. Doubled token in remittance memos.** One memo template rendered
`PAYMENT INV INV-2025-0081`. Cosmetic, but it is exactly the kind of artifact a memo
parser would trip over in an unintended way. Changed to `PAYMENT FOR <id>`.

**B4. Heredoc write failed.** Writing the generator through a shell heredoc hit the
platform's command-length limit (`ENAMETOOLONG`). Switched to direct file writes for
anything large. Noted only because it cost a round trip.

---

## Phase 2 - the agent

### D9. Four stages, presented as three tiers
The brief asked for three tiers. Deterministic set-based matching (one credit
clearing several invoices, several instalments clearing one) is neither a simple
exact match nor fuzzy scoring, so it runs as its own stage inside Tier 2 and is
reported separately as `T2_STRUCTURAL`. Folding it into the fuzzy row would have hidden
which mechanism actually recovered those 15 pairs.

Order is T1 exact -> T2 structural -> T2 fuzzy -> T3 LLM. Structural runs *before*
fuzzy because an exact subset sum is stronger evidence than a fuzzy single match, and
running it second stops fuzzy from stealing an instalment.

### D10. The measurement unit is the pair, not the transaction
A consolidated payment clears three invoices and a partial settlement takes three
transactions. Grading whole transactions would let a matcher that found 2 of 3
consolidated invoices score identically to one that found all 3. Everything is
measured on `(txn_id, invoice_id)` pairs: 98 of them.

### D11. A quoted invoice id is a strong hint, not proof
The memo regex is checked first, but a reference only produces a match if the amount
*also* reconciles. Three planted duplicate pairs quote the same invoice id in both
postings, so trusting the number alone yields a wrong match. Ground truth tracks
`has_invoice_ref` and the report states how many correct matches came from a literal
reference (21) versus inference (75), because a reconciler that only greps invoice
numbers should not be able to hide inside an aggregate.

### D12. Ambiguous pairs are escalated, never resolved by convention
FIFO would recover both planted coin-flips and lift recall from 98.0% to 100%. It was
rejected. The agent cannot distinguish the two invoices - they share customer, amount
and currency - so applying FIFO would post a guess with the same confidence as a real
match. The exception text names both invoices and the 0.000 margin, which is what a
controller needs to decide. Recall 98% with the ambiguity visible beats 100% with a
coin-flip buried in it.

### D13. Wrong matches are the expensive error, and the thresholds say so
A wrong match silently closes a live receivable; a missed match lands on a list a
human reads. So Tier 2 requires confidence >= 0.75 *and* a >= 0.06 margin over the
runner-up, an unexplained amount gap is disqualifying rather than merely
low-scoring, and Tier 3 must clear 0.70 on its own self-reported confidence. The
measured cost of that conservatism is 2 missed pairs; the measured benefit is 0 wrong
matches across seven seeds.

### D14. Fee and FX rules are policies, not fitted constants
The generator uses fees of 12.50-30.00, so a rule reading `delta in {12.50, 15.00,
25.00, 30.00}` would have scored better. That is fitting the answer key. The shipped
rule is a policy a controller could sign: a shortfall counts as a wire fee if it is
<= 60.00 absolute **and** <= 2% of the invoice. It costs one recall point on seed
88888 - a 30.00 fee on a small invoice is 2.7% and gets escalated - and that is the
correct behaviour, not a bug.

Likewise FX is a +/-1.5% band around the published month-average table rate, not the
0.4-1.2% drift the generator actually injects.

### D15. Tier 3 cannot invent an invoice
The model's answer is validated twice: against the pydantic schema, then against the
exact set of candidate ids it was shown. An id outside that set increments a
`hallucinated_ids` counter and is converted into a refusal rather than a match. The
prompt also tells it that refusing is a normal answer, since by construction it only
ever sees cases the deterministic tiers could not resolve.

Prompt-and-validate was chosen over tool-use forcing: the raw text is what gets
cached, so a cache entry stays replayable even if the SDK's tool surface changes.

### D16. The cache key is the whole request payload
SHA-256 over the canonical JSON of transaction, candidates, model id and a
`PROMPT_VERSION` constant. Changing the prompt or the candidate fields therefore
invalidates every entry rather than silently replaying decisions made under different
instructions. Reruns cost nothing and are byte-identical.

### D17. No key means the report says so
With no `ANTHROPIC_API_KEY` the pipeline runs tiers 1-2 and prints a callout that
Tier 3 did not execute. It does not quietly present deterministic-only numbers as the
full system's result.

## Phase 3 - measurement

### D18. Correct, wrong and missed are never blended
Three separate counters, and the wrong-match table is section 3 - before precision,
recall or F1 appear anywhere. An F1 of 0.99 can hide either 1 wrong match or 2 missed
ones, and those cost a finance team completely different amounts.

### D19. The match-rate ceiling is stated next to the match rate
25 of 120 statement lines genuinely settle nothing, so the achievable match rate is
79.2%, not 100%. The report prints that ceiling beside the measured 77.5% so the
number cannot be read as a 22-point failure.

### D20. Correct rejections are scored too
Precision and recall over true pairs say nothing about the 25 lines the agent was
right to leave alone. Refusing all 25 is reported separately as rejection accuracy,
because a matcher that hits 98% recall by matching everything would look identical on
recall alone.

## Phase 4 - repo

### D21. Tests use stdlib unittest
The brief capped dependencies at pandas, rapidfuzz, anthropic and pydantic, and asked
before adding more. pytest would have been the reflex; `unittest` costs nothing and
needed no permission.

### D22. Tier 3 is tested without spending a token
The LLM tests pre-seed the on-disk cache with the exact response under test, so
`decide()` takes its ordinary cache-hit branch and the real validation path runs -
including the hallucination guard. The fake API key means a test that accidentally
reached the network would fail rather than pass quietly.

### Broke / fixed

**B5. Tier 1 posted a coin-flip as a certainty.** Tier 1 accepted a pairing when
exactly one exact-amount candidate sat inside the -10/+15 day window. On one of the
planted ambiguous scenarios the rival invoice was 11 days out, so Tier 1 saw a
"unique" match and posted it. It happened to be the right answer, which is worse than
being wrong - the agent got credit for reasoning it never did, and the same rule would
post a genuine coin-flip on data where the rival sits just inside the window instead.
Fixed by testing uniqueness across the full +/-90-day range and only then applying the
tight window. Measured effect: ambiguous recall 1/2 -> 0/2, total recall 99.0% ->
98.0%, wrong matches unchanged at 0. Lower headline number, honest mechanism.

**B6. `make` is not installed on the development machine (Windows).** The Makefile is
untested here; `run.sh` is the verified path and is what the README leads with. The
Makefile targets are thin wrappers over the same module invocations.

**B7. Pydantic models reject positional arguments.** Nine `ReconException(...)` call
sites were written positionally and failed at construction. Converted to keyword
arguments.

**B8. PEP 695 generics broke the stated floor.** `def _find_subset[T](...)` requires
Python 3.12, while the project claims 3.11. Replaced with an explicit `TypeVar`.

**B9. `/tmp` means different things to Git Bash and to Python on Windows**, so the
multi-seed sweep wrote reports the reader could not find. Switched to explicit
absolute paths. No effect on shipped code - the sweep is a development check.
