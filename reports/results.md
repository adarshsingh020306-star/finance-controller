# Reconciliation results

Bank statement to invoice reconciliation over the full batch. Mode: **tiers 1-2 only (LLM tier not run)**.

## 1. Throughput

| Metric | Value |
| --- | ---: |
| Bank transactions processed | 120 |
| Invoices processed | 100 |
| Total records | 220 |
| Wall clock (s) | 0.129 |
| Records / second | 1,701 |

## 2. Match rate

| Metric | Value |
| --- | ---: |
| Transactions resolved to an invoice | 93 / 120 |
| Match rate | 77.5% |
| Transactions sent to exceptions | 27 |
| Invoices left open | 10 |

Of the 120 statement lines, 25 genuinely settle nothing (duplicate re-postings and non-AR lines). A perfect agent would resolve 95 of them, so a match rate near 79.2% is the ceiling, not 100%.

## 3. Wrong matches

**0 wrong match(es).** A wrong match silently closes a live receivable, so this is reported before any aggregate and is never averaged with missed matches.

_(none)_

## 4. Accuracy against ground truth

Measured on `(transaction, invoice)` pairs, so a consolidated payment that clears three invoices counts as three, not one.

| Metric | Value |
| --- | ---: |
| True pairs in ground truth | 98 |
| Correct matches | 96 |
| **Wrong matches** | **0** |
| Missed matches | 2 |
| Precision | 100.0% |
| Recall | 98.0% |
| F1 | 0.990 |

Correct rejections - lines the agent was right to leave alone:

| Metric | Value |
| --- | ---: |
| Genuinely unmatchable transactions | 25 |
| Correctly left unmatched | 25 |
| False alarms (matched something unmatchable) | 0 |
| Rejection accuracy | 100.0% |

How much of the accuracy came from a literal invoice number in the memo, rather than from inference:

| Source of correct match | Count |
| --- | ---: |
| Memo quoted the invoice id | 21 |
| Inferred from amount / counterparty / date | 75 |

## 5. Per-tier breakdown

What each tier bought, in order. Precision is that tier's own hit rate; recall share is how much of the total true work it recovered alone.

| Tier | Pairs posted | Correct | Wrong | Precision | Recall share |
| --- | ---: | ---: | ---: | ---: | ---: |
| T1_EXACT | 66 | 66 | 0 | 100.0% | 67.3% |
| T2_STRUCTURAL | 15 | 15 | 0 | 100.0% | 15.3% |
| T2_FUZZY | 15 | 15 | 0 | 100.0% | 15.3% |
| T3_LLM | 0 | 0 | 0 | 0.0% | 0.0% |

> **Tier 3 did not run in this report.** `ANTHROPIC_API_KEY` was not set, so every figure above is what the deterministic tiers achieve alone. Set the key and re-run to populate the `T3_LLM` row; the leftovers it would receive are exactly the exceptions listed in section 7.

## 6. Which planted edge cases failed

| Planted case | True pairs | Recovered | Missed | Wrong | Recall |
| --- | ---: | ---: | ---: | ---: | ---: |
| AMBIGUOUS | 2 | 0 | 2 | 0 | 0.0% |
| CLEAN | 48 | 48 | 0 | 0 | 100.0% |
| CONSOLIDATED | 5 | 5 | 0 | 0 | 100.0% |
| DATE_DRIFT | 5 | 5 | 0 | 0 | 100.0% |
| DUPLICATE | 8 | 8 | 0 | 0 | 100.0% |
| FEE_DEDUCTION | 4 | 4 | 0 | 0 | 100.0% |
| FX_DIFF | 4 | 4 | 0 | 0 | 100.0% |
| NAME_NOISE | 12 | 12 | 0 | 0 | 100.0% |
| PARTIAL | 10 | 10 | 0 | 0 | 100.0% |

Deliberately ambiguous pairs recovered: 0 of 2. These are the cases where one customer has two open invoices of the identical amount and the payment quotes no reference; ground truth resolves them by a FIFO convention that a human could not have independently derived from the two files either.

## 7. Exception list

Every one of the 37 unresolved records, with the specific reason it could not be resolved.

### 7a. Unresolved transactions (27)

| Txn | Planted case | Stage | Reason |
| --- | ---: | ---: | ---: |
| TXN00002 | DUPLICATE_ARTIFACT | T2_FUZZY | INV-2025-0003 was already settled by TXN00001 on 2025-01-26 for the identical amount 33,630.93 USD from the same counterparty; this line posts 1 day(s) later and clears no further receivable (suspected duplicate re-posting) |
| TXN00007 | NO_INVOICE | screen | debit of 1,820.34 USD to 'CASTLEGATE PROPERTIES LLC' - money leaving the account cannot clear an open receivable; memo 'OFFICE LEASE APR 2025 UNIT 4B' |
| TXN00008 | NO_INVOICE | T2_FUZZY | closest invoice INV-2025-0024 matches on counterparty (similarity 50) and timing (-39d from due) but the amount differs by -24,641.48 USD (77.8%); no wire-fee rule (<= 60.00) or FX conversion at the published rate explains a gap that size |
| TXN00018 | NO_INVOICE | T2_FUZZY | closest invoice INV-2025-0024 matches on counterparty (similarity 57) and timing (-14d from due) but the amount differs by -14,118.71 USD (44.6%); no wire-fee rule (<= 60.00) or FX conversion at the published rate explains a gap that size |
| TXN00019 | NO_INVOICE | T2_FUZZY | closest invoice INV-2025-0009 matches on counterparty (similarity 50) and timing (-8d from due) but the amount differs by +1,574.08 USD (9.7%); no wire-fee rule (<= 60.00) or FX conversion at the published rate explains a gap that size |
| TXN00021 | NO_INVOICE | T2_FUZZY | closest invoice INV-2025-0009 matches on counterparty (similarity 69) and timing (-7d from due) but the amount differs by -4,887.12 USD (30.3%); no wire-fee rule (<= 60.00) or FX conversion at the published rate explains a gap that size |
| TXN00025 | NO_INVOICE | T2_FUZZY | closest invoice INV-2025-0024 matches on counterparty (similarity 50) and timing (-10d from due) but the amount differs by +12,727.43 USD (40.2%); no wire-fee rule (<= 60.00) or FX conversion at the published rate explains a gap that size |
| TXN00027 | NO_INVOICE | T2_FUZZY | closest invoice INV-2025-0007 matches on counterparty (similarity 100) and timing (+33d from due) but the amount differs by -59,008.88 USD (75.3%); no wire-fee rule (<= 60.00) or FX conversion at the published rate explains a gap that size |
| TXN00030 | NO_INVOICE | T2_FUZZY | closest invoice INV-2025-0024 matches on counterparty (similarity 75) and timing (-2d from due) but the amount differs by -788.05 USD (2.5%); no wire-fee rule (<= 60.00) or FX conversion at the published rate explains a gap that size |
| TXN00036 | NO_INVOICE | T2_FUZZY | closest invoice INV-2025-0052 matches on counterparty (similarity 100) and timing (-46d from due) but the amount differs by -26,464.45 USD (45.8%); no wire-fee rule (<= 60.00) or FX conversion at the published rate explains a gap that size |
| TXN00037 | NO_INVOICE | T2_FUZZY | closest invoice INV-2025-0024 matches on counterparty (similarity 40) and timing (+7d from due) but the amount differs by -4,788.16 USD (15.1%); no wire-fee rule (<= 60.00) or FX conversion at the published rate explains a gap that size |
| TXN00040 | DUPLICATE_ARTIFACT | T2_FUZZY | INV-2025-0018 was already settled by TXN00039 on 2025-03-31 for the identical amount 37,315.77 USD from the same counterparty; this line posts 1 day(s) later and clears no further receivable (suspected duplicate re-posting) |
| TXN00061 | NO_INVOICE | T2_FUZZY | closest invoice INV-2025-0052 matches on counterparty (similarity 43) and timing (-12d from due) but the amount differs by -33,907.24 USD (58.6%); no wire-fee rule (<= 60.00) or FX conversion at the published rate explains a gap that size |
| TXN00062 | NO_INVOICE | screen | debit of 313.19 USD to 'APEX CARD SERVICES' - money leaving the account cannot clear an open receivable; memo 'CORPORATE CARD SETTLEMENT' |
| TXN00063 | NO_INVOICE | T2_FUZZY | closest invoice INV-2025-0052 matches on counterparty (similarity 46) and timing (-8d from due) but the amount differs by -55,026.59 USD (95.1%); no wire-fee rule (<= 60.00) or FX conversion at the published rate explains a gap that size |
| TXN00072 | DUPLICATE_ARTIFACT | T2_FUZZY | INV-2025-0073 was already settled by TXN00069 on 2025-05-12 for the identical amount 61,122.40 USD from the same counterparty; this line posts 2 day(s) later and clears no further receivable (suspected duplicate re-posting) |
| TXN00073 | DUPLICATE_ARTIFACT | T2_FUZZY | INV-2025-0070 was already settled by TXN00068 on 2025-05-11 for the identical amount 1,118.80 USD from the same counterparty; this line posts 3 day(s) later and clears no further receivable (suspected duplicate re-posting) |
| TXN00078 | NO_INVOICE | T2_FUZZY | closest invoice INV-2025-0052 matches on counterparty (similarity 36) and timing (+4d from due) but the amount differs by -10,418.40 USD (18.0%); no wire-fee rule (<= 60.00) or FX conversion at the published rate explains a gap that size |
| TXN00087 | NO_INVOICE | T2_FUZZY | closest invoice INV-2025-0087 matches on counterparty (similarity 42) and timing (-16d from due) but the amount differs by +44,008.43 USD (335.5%); no wire-fee rule (<= 60.00) or FX conversion at the published rate explains a gap that size |
| TXN00095 | DUPLICATE_ARTIFACT | T2_FUZZY | INV-2025-0069 was already settled by TXN00092 on 2025-06-06 for the identical amount 7,027.35 USD from the same counterparty; this line posts 1 day(s) later and clears no further receivable (suspected duplicate re-posting) |
| TXN00100 | DUPLICATE_ARTIFACT | T2_FUZZY | INV-2025-0081 was already settled by TXN00098 on 2025-06-12 for the identical amount 20,576.75 USD from the same counterparty; this line posts 2 day(s) later and clears no further receivable (suspected duplicate re-posting) |
| TXN00105 | AMBIGUOUS | T2_FUZZY | 2 invoices equally plausible: INV-2025-0087 and INV-2025-0098, both 'Pinnacle Foods' at 13,116.47 vs 13,116.47 USD; confidence margin only 0.000 and the memo 'CUSTOMER REMITTANCE - NO REFERENCE QUOTED' quotes no reference |
| TXN00111 | AMBIGUOUS | T2_FUZZY | 2 invoices equally plausible: INV-2025-0094 and INV-2025-0100, both 'Palisade Security' at 23,211.55 vs 23,211.55 USD; confidence margin only 0.002 and the memo 'CUSTOMER REMITTANCE - NO REFERENCE QUOTED' quotes no reference |
| TXN00112 | NO_INVOICE | T2_FUZZY | closest invoice INV-2025-0094 matches on counterparty (similarity 45) and timing (+1d from due) but the amount differs by -13,031.17 USD (56.1%); no wire-fee rule (<= 60.00) or FX conversion at the published rate explains a gap that size |
| TXN00113 | DUPLICATE_ARTIFACT | T2_FUZZY | INV-2025-0090 was already settled by TXN00110 on 2025-06-26 for the identical amount 54,536.94 USD from the same counterparty; this line posts 3 day(s) later and clears no further receivable (suspected duplicate re-posting) |
| TXN00116 | DUPLICATE_ARTIFACT | T2_FUZZY | INV-2025-0076 was already settled by TXN00114 on 2025-07-01 for the identical amount 68,308.28 USD from the same counterparty; this line posts 2 day(s) later and clears no further receivable (suspected duplicate re-posting) |
| TXN00117 | NO_INVOICE | screen | debit of 2,135.39 USD to 'FIRST MERIDIAN BANK' - money leaving the account cannot clear an open receivable; memo 'MONTHLY ACCOUNT MAINTENANCE FEE' |

### 7b. Invoices left open (10)

| Invoice | Planted case | Reason |
| --- | ---: | ---: |
| INV-2025-0007 | UNPAID | open at 78,383.47 USD; the closest credit is TXN00027 on 2025-03-14 for 19,374.59 USD (counterparty similarity 100, differs by -59,008.88), and it is itself unmatched |
| INV-2025-0009 | UNPAID | open at 16,152.33 USD; the closest credit is TXN00070 on 2025-05-13 for 46,835.71 USD (counterparty similarity 100, differs by +30,683.38), and it is already applied to INV-2025-0039 |
| INV-2025-0015 | UNPAID | open at 85,380.88 USD; the closest credit is TXN00006 on 2025-02-09 for 42,488.16 USD (counterparty similarity 100, differs by -42,892.72), and it is already applied to INV-2025-0011 |
| INV-2025-0024 | UNPAID | open at 31,679.00 USD; the closest credit is TXN00013 on 2025-02-22 for 32,654.14 USD (counterparty similarity 100, differs by +975.14), and it is already applied to INV-2025-0006 |
| INV-2025-0052 | UNPAID | open at 57,844.23 USD; the closest credit is TXN00036 on 2025-03-29 for 31,379.78 USD (counterparty similarity 100, differs by -26,464.45), and it is itself unmatched |
| INV-2025-0074 | UNPAID | open at 89,902.18 USD; the closest credit is TXN00120 on 2025-07-13 for 54,234.46 USD (counterparty similarity 100, differs by -35,667.72), and it is already applied to INV-2025-0089 |
| INV-2025-0087 | should have been matched | open at 13,116.47 USD; the closest credit is TXN00105 on 2025-06-21 for 13,116.47 USD (counterparty similarity 100, differs by +0.00), and it is itself unmatched |
| INV-2025-0094 | should have been matched | open at 23,211.55 USD; the closest credit is TXN00111 on 2025-06-26 for 23,211.55 USD (counterparty similarity 100, differs by +0.00), and it is itself unmatched |
| INV-2025-0098 | AMBIGUOUS | open at 13,116.47 USD; the closest credit is TXN00105 on 2025-06-21 for 13,116.47 USD (counterparty similarity 100, differs by +0.00), and it is itself unmatched |
| INV-2025-0100 | AMBIGUOUS | open at 23,211.55 USD; the closest credit is TXN00111 on 2025-06-26 for 23,211.55 USD (counterparty similarity 100, differs by +0.00), and it is itself unmatched |

## 8. Missed matches

2 true pair(s) the agent failed to assert. Each is on the exception list above, so a human sees it - unlike a wrong match.

| Txn | Should have matched | Planted case | Reason given |
| --- | ---: | ---: | ---: |
| TXN00105 | INV-2025-0087 | AMBIGUOUS | 2 invoices equally plausible: INV-2025-0087 and INV-2025-0098, both 'Pinnacle Foods' at 13,116.47 vs 13,116.47 USD; confidence margin only 0.000 and the memo 'C |
| TXN00111 | INV-2025-0094 | AMBIGUOUS | 2 invoices equally plausible: INV-2025-0094 and INV-2025-0100, both 'Palisade Security' at 23,211.55 vs 23,211.55 USD; confidence margin only 0.002 and the memo |

