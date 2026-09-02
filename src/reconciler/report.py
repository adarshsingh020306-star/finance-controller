"""Run the agent over the full batch and report measured results.

Writes the identical report to stdout and to ``reports/results.md``. Wrong matches
are printed before anything else that could soften them, and are never folded into an
average with missed matches - the two failures cost a finance team completely
different amounts of money.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .engine import Reconciler
from .llm import LLMTier
from .models import load_dataset
from .scoring import Scorecard, load_ground_truth, score


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    """Render a markdown table, right-aligning every column after the first."""
    if not rows:
        return ["_(none)_", ""]
    align = ["---"] + ["---:"] * (len(headers) - 1)
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join(align) + " |"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    out.append("")
    return out


def _pct(value: float) -> str:
    """Format a 0-1 ratio as a percentage with one decimal."""
    return f"{value * 100:.1f}%"


def render(card: Scorecard, result, truth) -> str:
    """Build the full markdown report for one measured run.

    Args:
        card: The graded scorecard.
        result: The raw run result, used for the exception list and rationales.
        truth: Ground truth, used to label each exception with its planted case.

    Returns:
        The complete report as a markdown string.
    """
    L: list[str] = []
    mode = "tiers 1-3 (LLM enabled)" if card.llm_available else "tiers 1-2 only (LLM tier not run)"

    L += ["# Reconciliation results", ""]
    L += [f"Bank statement to invoice reconciliation over the full batch. Mode: **{mode}**.", ""]

    # -- throughput ------------------------------------------------------- #
    L += ["## 1. Throughput", ""]
    L += _table(
        ["Metric", "Value"],
        [
            ["Bank transactions processed", card.n_transactions],
            ["Invoices processed", card.n_invoices],
            ["Total records", card.n_transactions + card.n_invoices],
            ["Wall clock (s)", f"{card.elapsed_seconds:.3f}"],
            ["Records / second", f"{card.records_per_second:,.0f}"],
        ],
    )

    # -- match rate ------------------------------------------------------- #
    L += ["## 2. Match rate", ""]
    L += _table(
        ["Metric", "Value"],
        [
            ["Transactions resolved to an invoice", f"{card.matched_transactions} / {card.n_transactions}"],
            ["Match rate", _pct(card.match_rate)],
            ["Transactions sent to exceptions", card.exception_transactions],
            ["Invoices left open", card.exception_invoices],
        ],
    )
    L += [
        f"Of the {card.n_transactions} statement lines, {card.total_unmatchable_txns} "
        f"genuinely settle nothing (duplicate re-postings and non-AR lines). A perfect "
        f"agent would resolve {card.n_transactions - card.total_unmatchable_txns} of "
        f"them, so a match rate near "
        f"{_pct((card.n_transactions - card.total_unmatchable_txns) / card.n_transactions)} "
        f"is the ceiling, not 100%.",
        "",
    ]

    # -- wrong matches first ---------------------------------------------- #
    L += ["## 3. Wrong matches", ""]
    L += [
        f"**{card.wrong} wrong match(es).** A wrong match silently closes a live "
        f"receivable, so this is reported before any aggregate and is never averaged "
        f"with missed matches.",
        "",
    ]
    L += _table(
        ["Txn", "Posted to", "Truth", "Planted case", "Tier", "Conf", "Why the agent believed it"],
        [
            [w.txn_id, w.predicted_invoice, w.true_invoice, w.case_type, w.tier,
             f"{w.confidence:.2f}", w.rationale[:120]]
            for w in card.wrong_matches
        ],
    )

    # -- accuracy --------------------------------------------------------- #
    L += ["## 4. Accuracy against ground truth", ""]
    L += [
        "Measured on `(transaction, invoice)` pairs, so a consolidated payment that "
        "clears three invoices counts as three, not one.",
        "",
    ]
    L += _table(
        ["Metric", "Value"],
        [
            ["True pairs in ground truth", card.total_true_pairs],
            ["Correct matches", card.correct],
            ["**Wrong matches**", f"**{card.wrong}**"],
            ["Missed matches", card.missed],
            ["Precision", _pct(card.precision)],
            ["Recall", _pct(card.recall)],
            ["F1", f"{card.f1:.3f}"],
        ],
    )
    L += ["Correct rejections - lines the agent was right to leave alone:", ""]
    L += _table(
        ["Metric", "Value"],
        [
            ["Genuinely unmatchable transactions", card.total_unmatchable_txns],
            ["Correctly left unmatched", card.correct_rejections],
            ["False alarms (matched something unmatchable)", card.false_alarms],
            ["Rejection accuracy", _pct(card.rejection_accuracy)],
        ],
    )
    L += [
        "How much of the accuracy came from a literal invoice number in the memo, "
        "rather than from inference:",
        "",
    ]
    L += _table(
        ["Source of correct match", "Count"],
        [
            ["Memo quoted the invoice id", card.correct_with_reference],
            ["Inferred from amount / counterparty / date", card.correct_without_reference],
        ],
    )

    # -- per tier --------------------------------------------------------- #
    L += ["## 5. Per-tier breakdown", ""]
    L += [
        "What each tier bought, in order. Precision is that tier's own hit rate; "
        "recall share is how much of the total true work it recovered alone.",
        "",
    ]
    L += _table(
        ["Tier", "Pairs posted", "Correct", "Wrong", "Precision", "Recall share"],
        [
            [t.tier, t.asserted, t.correct, t.wrong, _pct(t.precision),
             _pct(t.recall_share(card.total_true_pairs))]
            for t in card.tiers
        ],
    )
    if card.llm_available:
        s = card.llm_stats
        L += [
            f"Tier 3 calls: {s.get('calls_attempted', 0)} attempted, "
            f"{s.get('api_calls', 0)} hit the API, {s.get('cache_hits', 0)} served from "
            f"cache. Rejected outputs: {s.get('invalid_json', 0)} unparseable, "
            f"{s.get('hallucinated_ids', 0)} named an invoice outside the candidate set.",
            "",
        ]
    else:
        L += [
            "> **Tier 3 did not run in this report.** `ANTHROPIC_API_KEY` was not set, "
            "so every figure above is what the deterministic tiers achieve alone. "
            "Set the key and re-run to populate the `T3_LLM` row; the leftovers it "
            "would receive are exactly the exceptions listed in section 7.",
            "",
        ]

    # -- case confusion --------------------------------------------------- #
    L += ["## 6. Which planted edge cases failed", ""]
    L += _table(
        ["Planted case", "True pairs", "Recovered", "Missed", "Wrong", "Recall"],
        [
            [c.case_type, c.truth_pairs, c.recovered, c.missed, c.wrong, _pct(c.recall)]
            for c in card.cases if c.truth_pairs or c.wrong
        ],
    )
    L += [
        f"Deliberately ambiguous pairs recovered: {card.ambiguous_recovered} of "
        f"{card.ambiguous_total}. These are the cases where one customer has two open "
        f"invoices of the identical amount and the payment quotes no reference; ground "
        f"truth resolves them by a FIFO convention that a human could not have "
        f"independently derived from the two files either.",
        "",
    ]

    # -- exceptions ------------------------------------------------------- #
    L += ["## 7. Exception list", ""]
    L += [
        f"Every one of the {len(result.exceptions)} unresolved records, with the "
        f"specific reason it could not be resolved.",
        "",
    ]
    txn_exc = [e for e in result.exceptions if e.record_type == "transaction"]
    inv_exc = [e for e in result.exceptions if e.record_type == "invoice"]

    L += [f"### 7a. Unresolved transactions ({len(txn_exc)})", ""]
    L += _table(
        ["Txn", "Planted case", "Stage", "Reason"],
        [
            [e.record_id, truth.txn_case.get(e.record_id, "-"), e.tier_reached, e.reason]
            for e in sorted(txn_exc, key=lambda e: e.record_id)
        ],
    )
    L += [f"### 7b. Invoices left open ({len(inv_exc)})", ""]
    L += _table(
        ["Invoice", "Planted case", "Reason"],
        [
            [e.record_id,
             truth.unmatchable_invoices.get(e.record_id, "should have been matched"),
             e.reason]
            for e in sorted(inv_exc, key=lambda e: e.record_id)
        ],
    )

    # -- missed ----------------------------------------------------------- #
    L += ["## 8. Missed matches", ""]
    L += [
        f"{card.missed} true pair(s) the agent failed to assert. Each is on the "
        f"exception list above, so a human sees it - unlike a wrong match.",
        "",
    ]
    L += _table(
        ["Txn", "Should have matched", "Planted case", "Reason given"],
        [
            [m.txn_id, m.true_invoice, m.case_type, m.exception_reason[:160]]
            for m in card.missed_matches
        ],
    )
    return "\n".join(L) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the agent over the full batch, print the report and write results.md.

    Returns:
        Process exit code: ``0`` on success, ``1`` if inputs are missing.
    """
    parser = argparse.ArgumentParser(description="Reconcile and report measured accuracy.")
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--out", type=Path, default=Path("reports/results.md"))
    parser.add_argument("--cache", type=Path, default=Path(".cache/llm"))
    parser.add_argument("--model", default=None, help="override $RECON_MODEL")
    parser.add_argument("--no-llm", action="store_true",
                        help="run the deterministic tiers only")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv  # optional convenience
        load_dotenv()
    except ImportError:
        pass

    try:
        dataset = load_dataset(args.data)
        truth = load_ground_truth(args.data / "ground_truth.csv")
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    llm = LLMTier(cache_dir=args.cache, model=args.model, enabled=not args.no_llm)
    result = Reconciler(dataset, llm).run()
    card = score(result, truth)

    report = render(card, result, truth)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    print(report)
    print(f"[written to {args.out}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
