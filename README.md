# AI Finance Controller — bank statement to invoice reconciliation

Closes one finance ops loop end to end: it reads a 120-line bank statement and a
100-invoice AR ledger, matches every credit to the receivable it settles, and reports
its accuracy against planted ground truth plus a specific reason for every record it
refused to resolve.

Deterministic rules do the work; an LLM is called only for the leftovers, and it is
never allowed to invent an invoice.

---

## Results (measured, full batch, no sampling)

Deterministic tiers only — the run below had no `ANTHROPIC_API_KEY` set, so Tier 3
did not execute. Reproduce with `./run.sh --no-llm`.

| | |
|---|---:|
| Records processed | 220 (120 transactions + 100 invoices) |
| Wall clock | 0.106 s |
| Throughput | **2,080 records/sec** |
| Match rate | 93 / 120 transactions (77.5%) |
| **Correct matches** | **96** of 98 true pairs |
| **Wrong matches** | **0** |
| Missed matches | 2 |
| Precision | **100.0%** |
| Recall | **98.0%** |
| F1 | 0.990 |
| Unmatchable transactions correctly left alone | 25 / 25 (100%) |
| False alarms | 0 |

Accuracy is measured on `(transaction, invoice)` **pairs**, not transactions, so a
consolidated payment clearing three invoices counts as three. The match rate ceiling
is 79.2%, not 100%: 25 of the 120 statement lines genuinely settle nothing.

### Per tier — what each stage actually bought

| Tier | Pairs posted | Correct | Wrong | Precision | Recall share |
|---|---:|---:|---:|---:|---:|
| T1_EXACT — reference / exact amount | 66 | 66 | 0 | 100.0% | 67.3% |
| T2_STRUCTURAL — subset sums | 15 | 15 | 0 | 100.0% | 15.3% |
| T2_FUZZY — rapidfuzz + tolerances | 15 | 15 | 0 | 100.0% | 15.3% |
| T3_LLM | 0 | 0 | 0 | — | — |

**The honest headline: on this dataset the LLM tier bought nothing, because the
deterministic tiers already cleared everything that is deterministically clearable.**
What reaches Tier 3 is 27 transactions: 25 that genuinely match nothing, and the 2
deliberately ambiguous ones. Its job there is to *keep refusing* — a Tier 3 that
starts matching those would drive precision below 100%, not above it. That is a
result about the problem, not a gap in the build.

### Where the correct matches came from

| Source | Count |
|---|---:|
| Memo quoted the invoice id literally | 21 |
| Inferred from amount / counterparty / date | 75 |

### Per planted edge case

| Planted case | True pairs | Recovered | Missed | Wrong | Recall |
|---|---:|---:|---:|---:|---:|
| CLEAN | 48 | 48 | 0 | 0 | 100% |
| NAME_NOISE | 12 | 12 | 0 | 0 | 100% |
| PARTIAL | 10 | 10 | 0 | 0 | 100% |
| DUPLICATE | 8 | 8 | 0 | 0 | 100% |
| CONSOLIDATED | 5 | 5 | 0 | 0 | 100% |
| DATE_DRIFT | 5 | 5 | 0 | 0 | 100% |
| FX_DIFF | 4 | 4 | 0 | 0 | 100% |
| FEE_DEDUCTION | 4 | 4 | 0 | 0 | 100% |
| **AMBIGUOUS** | **2** | **0** | **2** | **0** | **0%** |

Both misses are the planted coin-flips: one customer, two open invoices of the
identical amount, a payment quoting no reference. Ground truth resolves them by a
FIFO convention a human could not have derived from the two files either. The agent
escalates them instead of guessing, which is the behaviour I want — see
[DECISIONS.md](DECISIONS.md) D12.

### Not overfit to one seed

Seven seeds, full batch each, deterministic tiers only:

| seed | 1 | 7 | 42 | 101 | 555 | 2024 | 88888 |
|---|---:|---:|---:|---:|---:|---:|---:|
| correct / 98 | 96 | 96 | 96 | 96 | 96 | 96 | 95 |
| **wrong** | **0** | **0** | **0** | **0** | **0** | **0** | **0** |
| recall | 98.0% | 98.0% | 98.0% | 98.0% | 98.0% | 98.0% | 96.9% |

Seed 88888's extra miss is a 30.00 wire fee on a small invoice — 2.7%, over the 2%
materiality policy — so the rule refused to absorb it and sent it to a human. Working
as designed.

---

## Setup and run

Needs Python 3.11+. One command, from a fresh clone:

```bash
./run.sh
```

That creates a virtualenv, installs dependencies, generates the dataset, runs the
tests, reconciles the full batch and writes `reports/results.md`.

Tier 3 is optional. Without a key the pipeline runs tiers 1–2 and says so in the
report rather than passing deterministic-only numbers off as its full result:

```bash
cp .env.example .env    # then put your key in it
```

Other entry points:

```bash
./run.sh --no-llm
```

```bash
make data && make reconcile && make test
```

---

## How it works

```
bank_transactions.csv ─┐
invoices.csv ──────────┼─→ screen → T1 exact → T2 structural → T2 fuzzy → T3 LLM
fx_rates.json ─────────┘        │        │            │            │        │
                                └────────┴────────────┴────────────┴────────┴─→ exceptions
                                                                                    │
                                          ground_truth.csv ──→ scoring ──→ results.md
```

**Screen** — a debit cannot clear a receivable, so it leaves before any fuzzy logic
sees it and gets an honest reason.

**Tier 1, exact.** An invoice id quoted in the memo *whose amount also reconciles*, or
an exact same-currency amount from a near-certain counterparty inside a −10/+15 day
window. Uniqueness is tested across the full ±90-day range, not just the window —
otherwise a rival invoice sitting one day outside is invisible and Tier 1 posts a
coin-flip as a certainty. That bug was real; see DECISIONS B5.

**Tier 2 structural** — still fully deterministic. Exact subset sums resolve one
credit clearing several invoices, and several instalments clearing one invoice.

**Tier 2 fuzzy** — `rapidfuzz` counterparty similarity over normalised names, with
amount tolerances (wire fee, FX at the published rate) and a wider date window.
Accepted only above a 0.75 confidence **and** a 0.06 margin over the runner-up; below
the margin the pairing is genuinely ambiguous and is escalated, not guessed.

**Tier 3, LLM** — the leftovers only, with the top 5 candidates and a strict JSON
schema. The answer is validated against the exact candidate set it was shown; an
invoice id it was never offered is counted as a hallucination and converted into a
refusal. Every call is cached on disk under a SHA-256 of the request payload, so
reruns are free and byte-identical.

**Claim ledger** — an invoice may be settled once and a transaction may settle once.
Transactions are processed in date order, so when the bank posts the same credit
twice the earlier posting wins and the later one is reported as a suspected
duplicate rather than silently double-clearing the receivable.

### Every threshold in one place

All policy lives in [`src/reconciler/rules.py`](src/reconciler/rules.py), so a
reviewer can change one and re-measure:

| Rule | Value |
|---|---|
| Wire fee ceiling | ≤ 60.00 absolute **and** ≤ 2% of invoice |
| FX tolerance vs published rate | ± 1.5% |
| Normal date window | −10 / +15 days from due date |
| Maximum date gap considered | ± 90 days |
| Tier 2 acceptance | confidence ≥ 0.75 **and** margin ≥ 0.06 |
| Tier 3 acceptance | model confidence ≥ 0.70 |
| Confidence blend | 0.45 name + 0.35 amount + 0.20 date |

---

## Exception list

Every unresolved record carries a specific, quantified reason — never "could not
match". A test asserts that each reason cites concrete evidence. Real output:

> `INV-2025-0003` was already settled by `TXN00001` on 2025-01-26 for the identical
> amount 33,630.93 USD from the same counterparty; this line posts 1 day(s) later and
> clears no further receivable (suspected duplicate re-posting)

> 2 invoices equally plausible: `INV-2025-0087` and `INV-2025-0098`, both 'Pinnacle
> Foods' at 13,116.47 vs 13,116.47 USD; confidence margin only 0.000 and the memo
> 'CUSTOMER REMITTANCE - NO REFERENCE QUOTED' quotes no reference

> closest invoice `INV-2025-0009` matches on counterparty (similarity 50) and timing
> (−8d from due) but the amount differs by +1,574.08 USD (9.7%); no wire-fee rule
> (≤ 60.00) or FX conversion at the published rate explains a gap that size

---

## The dataset

100 invoices, 120 transactions, 131 ground-truth rows (98 true pairs, 25 unmatchable
transactions, 8 unmatchable invoices). The planted case plan is asserted in
`DatasetBuilder._verify()`, so the generator fails loudly rather than quietly
emitting a different mix than this README claims.

Two anti-leakage measures matter for judging:

1. `invoices.status` is only `open` / `overdue`, derived from due date vs the
   statement cutoff. A real ledger's `paid` flag is an *output* of reconciliation;
   shipping it would let a one-line matcher score 100%.
2. 24 memos quote a literal invoice id, because real remittance advice does — and
   ground truth flags them, so the report must state how many matches were won by a
   regex rather than by inference (21 of 96). Three duplicate pairs quote the *same*
   id in both memos, so naive id-extraction yields a wrong match, not a free win.

Full case table and conventions: [DECISIONS.md](DECISIONS.md).

---

## Layout

```
src/reconciler/
  generate_data.py   Phase 1 — synthetic data + ground truth, case plan asserted
  models.py          pydantic records, CSV loading
  normalize.py       counterparty normalisation + rapidfuzz scoring
  rules.py           every business threshold, in one file
  engine.py          tiered matching, claim ledger, exception reasoning
  llm.py             Tier 3: cached, schema-validated, hallucination-guarded
  scoring.py         grading against ground truth
  report.py          stdout + reports/results.md
tests/               34 tests, stdlib unittest
data/                generated CSVs + ground truth
reports/results.md   full report, regenerated each run
```

## Tests

```bash
make test
```

34 tests, no extra dependency. The Tier 3 tests exercise the real `LLMTier` code
path — including the hallucination guard — without spending a token, by pre-seeding
the on-disk cache so `decide()` takes its normal cache-hit branch. If it ever reached
for the network instead, the fake key would fail and the test would fail with it.

## Known limitations

- The 2% fee-materiality rule refuses small-invoice fees that a human would wave
  through. Deliberate: absorbing unexplained shortfalls is how reconciliation tools
  lose money.
- Subset search is capped at 4 invoices per consolidated payment and 3 instalments
  per invoice, and skipped entirely above 14 same-customer records. Beyond that the
  combinatorics stop being safe.
- Ambiguous pairs are escalated, never resolved by convention. FIFO would recover
  both and lift recall to 100%, but it would post a coin-flip as a certainty.
- FX uses a static published rate table, not a daily rate. That is what a treasury
  desk actually hands you, but a real deployment would want dated rates.
