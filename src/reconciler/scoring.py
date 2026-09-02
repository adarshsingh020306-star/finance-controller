"""Grade a reconciliation run against ground truth.

The unit of measurement is the ``(txn_id, invoice_id)`` pair, not the transaction,
because a consolidated payment asserts several pairs and a partial settlement asserts
several too. Grading whole transactions would let a matcher that found 2 of a
customer's 3 consolidated invoices score the same as one that found all 3.

Three numbers are kept strictly separate and never averaged together:

* **correct** - a pair the agent asserted that ground truth agrees with
* **wrong** - a pair the agent asserted that is false. In finance this is the
  expensive error: it silently closes a live receivable.
* **missed** - a true pair the agent failed to assert. Cheap by comparison: the
  record lands on the exception list for a human.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .engine import ReconResult

Pair = tuple[str, str]


@dataclass
class GroundTruth:
    """The planted truth for one dataset."""

    pairs: set[Pair]
    pair_case: dict[Pair, str]
    pair_ambiguous: set[Pair]
    pair_has_ref: set[Pair]
    unmatchable_txns: dict[str, str]      # txn_id -> case_type
    unmatchable_invoices: dict[str, str]  # invoice_id -> case_type
    txn_case: dict[str, str]

    @property
    def matchable_txns(self) -> set[str]:
        """Transactions that genuinely settle at least one invoice."""
        return {t for t, _ in self.pairs}


def load_ground_truth(path: Path) -> GroundTruth:
    """Read ``ground_truth.csv`` into the structures the scorer needs.

    Args:
        path: Path to the ground-truth CSV emitted by the generator.

    Returns:
        A :class:`GroundTruth` holding true pairs, per-pair case labels, and the two
        unmatchable sides.

    Raises:
        FileNotFoundError: If the ground-truth file is missing.
    """
    if not path.exists():
        raise FileNotFoundError(f"{path} not found - run the generator first")
    df = pd.read_csv(path, dtype=str, keep_default_na=False)

    pairs: set[Pair] = set()
    pair_case: dict[Pair, str] = {}
    ambiguous: set[Pair] = set()
    has_ref: set[Pair] = set()
    bad_txns: dict[str, str] = {}
    bad_invs: dict[str, str] = {}
    txn_case: dict[str, str] = {}

    for row in df.to_dict("records"):
        txn_id, inv_id = row["txn_id"], row["invoice_id"]
        case = row["case_type"]
        if txn_id:
            txn_case[txn_id] = case
        if row["unmatchable"] == "no":
            pair = (txn_id, inv_id)
            pairs.add(pair)
            pair_case[pair] = case
            if row["ambiguous"] == "yes":
                ambiguous.add(pair)
            if row["has_invoice_ref"] == "yes":
                has_ref.add(pair)
        elif row["relation"] == "unmatchable_transaction":
            bad_txns[txn_id] = case
        elif row["relation"] == "unmatchable_invoice":
            bad_invs[inv_id] = case

    return GroundTruth(
        pairs=pairs,
        pair_case=pair_case,
        pair_ambiguous=ambiguous,
        pair_has_ref=has_ref,
        unmatchable_txns=bad_txns,
        unmatchable_invoices=bad_invs,
        txn_case=txn_case,
    )


@dataclass
class WrongMatch:
    """One pair the agent asserted that ground truth contradicts."""

    txn_id: str
    predicted_invoice: str
    true_invoice: str
    case_type: str
    tier: str
    confidence: float
    rationale: str


@dataclass
class MissedMatch:
    """One true pair the agent failed to assert."""

    txn_id: str
    true_invoice: str
    case_type: str
    exception_reason: str


@dataclass
class TierMetrics:
    """Precision and volume for a single tier."""

    tier: str
    asserted: int
    correct: int
    wrong: int

    @property
    def precision(self) -> float:
        """Share of this tier's asserted pairs that are true."""
        return self.correct / self.asserted if self.asserted else 0.0

    def recall_share(self, total_true: int) -> float:
        """Share of *all* true pairs this tier recovered on its own."""
        return self.correct / total_true if total_true else 0.0


@dataclass
class CaseMetrics:
    """How the agent handled one planted edge-case type."""

    case_type: str
    truth_pairs: int
    recovered: int
    missed: int
    wrong: int

    @property
    def recall(self) -> float:
        """Share of this case type's true pairs that were recovered."""
        return self.recovered / self.truth_pairs if self.truth_pairs else 0.0


@dataclass
class Scorecard:
    """The full measured result of one run."""

    n_transactions: int
    n_invoices: int
    elapsed_seconds: float

    correct: int
    wrong: int
    missed: int
    total_true_pairs: int

    matched_transactions: int
    exception_transactions: int
    exception_invoices: int

    correct_rejections: int
    false_alarms: int
    total_unmatchable_txns: int

    tiers: list[TierMetrics]
    cases: list[CaseMetrics]
    wrong_matches: list[WrongMatch]
    missed_matches: list[MissedMatch]

    correct_with_reference: int
    correct_without_reference: int
    ambiguous_recovered: int
    ambiguous_total: int

    llm_available: bool
    llm_stats: dict = field(default_factory=dict)

    @property
    def precision(self) -> float:
        """Correct / asserted. How much of what the agent posted is trustworthy."""
        asserted = self.correct + self.wrong
        return self.correct / asserted if asserted else 0.0

    @property
    def recall(self) -> float:
        """Correct / true. How much of the real work the agent got through."""
        return self.correct / self.total_true_pairs if self.total_true_pairs else 0.0

    @property
    def f1(self) -> float:
        """Harmonic mean of precision and recall."""
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def match_rate(self) -> float:
        """Share of statement lines the agent resolved to at least one invoice."""
        return self.matched_transactions / self.n_transactions if self.n_transactions else 0.0

    @property
    def records_per_second(self) -> float:
        """Throughput over the combined batch of transactions and invoices."""
        total = self.n_transactions + self.n_invoices
        return total / self.elapsed_seconds if self.elapsed_seconds > 0 else 0.0

    @property
    def rejection_accuracy(self) -> float:
        """Share of genuinely unmatchable transactions correctly left alone."""
        return (self.correct_rejections / self.total_unmatchable_txns
                if self.total_unmatchable_txns else 0.0)


def score(result: ReconResult, truth: GroundTruth) -> Scorecard:
    """Grade a run against ground truth.

    Args:
        result: The output of :meth:`~reconciler.engine.Reconciler.run`.
        truth: The planted truth loaded by :func:`load_ground_truth`.

    Returns:
        A fully populated :class:`Scorecard`. Nothing is rounded or blended here -
        the report layer decides how to present it.
    """
    predicted = result.predicted_pairs()
    tier_of = result.tier_of_pair()
    match_by_txn = {m.txn_id: m for m in result.matches}
    exc_by_id = {e.record_id: e for e in result.exceptions}

    correct_pairs = predicted & truth.pairs
    wrong_pairs = predicted - truth.pairs
    missed_pairs = truth.pairs - predicted

    # Which invoice the transaction *should* have settled, for the wrong-match table.
    true_inv_for_txn: dict[str, list[str]] = {}
    for t, i in truth.pairs:
        true_inv_for_txn.setdefault(t, []).append(i)

    wrong_matches = [
        WrongMatch(
            txn_id=t,
            predicted_invoice=i,
            true_invoice=", ".join(sorted(true_inv_for_txn.get(t, []))) or "(none - unmatchable)",
            case_type=truth.txn_case.get(t, "UNKNOWN"),
            tier=tier_of.get((t, i), "?"),
            confidence=match_by_txn[t].confidence if t in match_by_txn else 0.0,
            rationale=match_by_txn[t].rationale if t in match_by_txn else "",
        )
        for t, i in sorted(wrong_pairs)
    ]
    missed_matches = [
        MissedMatch(
            txn_id=t,
            true_invoice=i,
            case_type=truth.pair_case.get((t, i), "UNKNOWN"),
            exception_reason=(exc_by_id[t].reason if t in exc_by_id
                              else "matched to a different invoice"),
        )
        for t, i in sorted(missed_pairs)
    ]

    # Per-tier precision.
    tiers: list[TierMetrics] = []
    for tier in ("T1_EXACT", "T2_STRUCTURAL", "T2_FUZZY", "T3_LLM"):
        asserted = {p for p, tr in tier_of.items() if tr == tier}
        tiers.append(
            TierMetrics(
                tier=tier,
                asserted=len(asserted),
                correct=len(asserted & truth.pairs),
                wrong=len(asserted - truth.pairs),
            )
        )

    # Per planted case type.
    case_names = sorted(
        set(truth.pair_case.values())
        | set(truth.unmatchable_txns.values())
        | set(truth.unmatchable_invoices.values())
    )
    cases: list[CaseMetrics] = []
    for name in case_names:
        truth_here = {p for p, c in truth.pair_case.items() if c == name}
        wrong_here = sum(1 for w in wrong_matches if w.case_type == name)
        cases.append(
            CaseMetrics(
                case_type=name,
                truth_pairs=len(truth_here),
                recovered=len(truth_here & predicted),
                missed=len(truth_here - predicted),
                wrong=wrong_here,
            )
        )

    matched_txns = {m.txn_id for m in result.matches}
    unmatchable = set(truth.unmatchable_txns)
    false_alarms = len(matched_txns & unmatchable)

    return Scorecard(
        n_transactions=result.n_transactions,
        n_invoices=result.n_invoices,
        elapsed_seconds=result.elapsed_seconds,
        correct=len(correct_pairs),
        wrong=len(wrong_pairs),
        missed=len(missed_pairs),
        total_true_pairs=len(truth.pairs),
        matched_transactions=len(matched_txns),
        exception_transactions=sum(
            1 for e in result.exceptions if e.record_type == "transaction"),
        exception_invoices=sum(
            1 for e in result.exceptions if e.record_type == "invoice"),
        correct_rejections=len(unmatchable - matched_txns),
        false_alarms=false_alarms,
        total_unmatchable_txns=len(unmatchable),
        tiers=tiers,
        cases=cases,
        wrong_matches=wrong_matches,
        missed_matches=missed_matches,
        correct_with_reference=len(correct_pairs & truth.pair_has_ref),
        correct_without_reference=len(correct_pairs - truth.pair_has_ref),
        ambiguous_recovered=len(correct_pairs & truth.pair_ambiguous),
        ambiguous_total=len(truth.pair_ambiguous),
        llm_available=result.llm_available,
        llm_stats=result.llm_stats,
    )
