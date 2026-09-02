"""The reconciliation engine: tiered matching with a single-claim ledger.

Order of work, deterministic first and the model last:

1. **Screen** - debits cannot settle a receivable, so they leave the pipeline before
   any fuzzy logic sees them.
2. **Tier 1, exact** - a quoted invoice reference, or an exact amount in the same
   currency from a near-certain counterparty inside the normal date window.
3. **Tier 2 structural** - subset sums, still fully deterministic: one credit
   clearing several invoices, and several instalments clearing one invoice.
4. **Tier 2 fuzzy** - rapidfuzz counterparty scoring with fee/FX amount tolerance and
   a wider date window, accepted only above a confidence threshold *and* a margin
   over the runner-up.
5. **Tier 3, LLM** - only the leftovers, with a validated candidate shortlist.

Throughout, an invoice may be claimed once and a transaction may be claimed once.
Transactions are processed in date order, so when the bank posts the same credit
twice the earlier posting wins the invoice and the later one is reported as a
suspected duplicate instead of silently double-clearing the receivable.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence, TypeVar

from . import rules
from .llm import LLMTier
from .models import (
    Candidate,
    Dataset,
    Invoice,
    Match,
    ReconException,
    Tier,
    Transaction,
)
from .normalize import extract_invoice_refs, normalize_party, party_similarity

T = TypeVar("T")

#: Largest group the structural tier will consider, to keep subset search bounded.
MAX_CONSOLIDATED_INVOICES = 4
MAX_PARTIAL_INSTALMENTS = 3
#: Above this many same-customer records the subset search is skipped as unsafe.
SUBSET_SEARCH_CAP = 14
#: Cash sums must reconcile to the cent; two cents of slack absorbs rounding only.
SUBSET_TOL = 0.02
#: How many candidates Tier 3 is shown.
LLM_SHORTLIST = 5


@dataclass
class ReconResult:
    """Everything one full pass over the batch produced."""

    matches: list[Match]
    exceptions: list[ReconException]
    elapsed_seconds: float
    n_transactions: int
    n_invoices: int
    llm_stats: dict[str, Any]
    llm_available: bool
    tier_counts: dict[str, int] = field(default_factory=dict)

    def predicted_pairs(self) -> set[tuple[str, str]]:
        """Flatten every match into the ``(txn_id, invoice_id)`` pairs it asserts."""
        return {
            (m.txn_id, inv_id) for m in self.matches for inv_id in m.invoice_ids
        }

    def tier_of_pair(self) -> dict[tuple[str, str], Tier]:
        """Map each asserted pair back to the tier that produced it."""
        return {
            (m.txn_id, inv_id): m.tier
            for m in self.matches
            for inv_id in m.invoice_ids
        }


class Reconciler:
    """Runs the full tiered match over one dataset."""

    def __init__(self, dataset: Dataset, llm: LLMTier) -> None:
        """Bind a dataset and a Tier 3 client.

        Args:
            dataset: Transactions, invoices and the published FX table.
            llm: The Tier 3 client. It may be unavailable; the run still completes.
        """
        self.ds = dataset
        self.llm = llm
        self.rates = dataset.fx_rates_to_usd
        self.inv_by_id = dataset.invoice_index()

        self.claimed_invoice: dict[str, str] = {}   # invoice_id -> claiming txn_id
        self.claimed_txn: dict[str, str] = {}       # txn_id -> primary invoice_id
        self.matches: list[Match] = []
        self.exceptions: list[ReconException] = []
        self._screened: set[str] = set()
        self._llm_notes: dict[str, str] = {}
        self.txn_by_id = dataset.transaction_index()

    # ------------------------------------------------------------------ #
    # Candidate generation
    # ------------------------------------------------------------------ #

    def _score_pair(self, txn: Transaction, inv: Invoice) -> Candidate | None:
        """Score one (transaction, invoice) pairing, or reject it on the date gate.

        Args:
            txn: The bank credit.
            inv: The candidate invoice.

        Returns:
            A scored :class:`Candidate`, or ``None`` when the settlement date is
            further from the due date than :data:`rules.MAX_DATE_GAP_DAYS`.
        """
        gap = rules.date_gap_days(txn.date, inv)
        if abs(gap) > rules.MAX_DATE_GAP_DAYS:
            return None
        name = party_similarity(txn.counterparty_raw, inv.customer_name)
        amount = rules.explain_amount(txn, inv, self.rates)
        return Candidate(
            invoice_id=inv.invoice_id,
            name_score=name,
            amount_basis=amount.basis,
            amount_delta=amount.delta,
            date_gap_days=gap,
            confidence=rules.confidence(name, amount, gap),
        )

    def _candidates(
        self,
        txn: Transaction,
        name_floor: float,
        only_unclaimed: bool = True,
        require_explained_amount: bool = True,
    ) -> list[Candidate]:
        """Build the scored candidate list for one transaction, best first.

        Args:
            txn: The transaction to find invoices for.
            name_floor: Minimum counterparty similarity to consider at all.
            only_unclaimed: Exclude invoices another transaction already settled.
            require_explained_amount: Drop pairings whose amount gap has no business
                explanation. Turned off when building exception reasons, where the
                near miss is exactly what needs reporting.

        Returns:
            Candidates sorted by confidence descending, then invoice id for stability.
        """
        out: list[Candidate] = []
        for inv in self.ds.invoices:
            if only_unclaimed and inv.invoice_id in self.claimed_invoice:
                continue
            cand = self._score_pair(txn, inv)
            if cand is None or cand.name_score < name_floor:
                continue
            if require_explained_amount and cand.amount_basis in ("unexplained",):
                continue
            if require_explained_amount and cand.amount_basis.startswith("fx gap"):
                continue
            out.append(cand)
        out.sort(key=lambda c: (-c.confidence, c.invoice_id))
        return out

    # ------------------------------------------------------------------ #
    # Claim ledger
    # ------------------------------------------------------------------ #

    def _claim(
        self,
        txn_id: str,
        invoice_ids: Sequence[str],
        tier: Tier,
        confidence: float,
        rationale: str,
    ) -> None:
        """Post a match and mark its transaction and invoices as consumed."""
        for inv_id in invoice_ids:
            self.claimed_invoice[inv_id] = txn_id
        self.claimed_txn[txn_id] = invoice_ids[0]
        self.matches.append(
            Match(
                txn_id=txn_id,
                invoice_ids=list(invoice_ids),
                tier=tier,
                confidence=round(confidence, 4),
                rationale=rationale,
            )
        )

    def _open_txns(self) -> list[Transaction]:
        """Transactions still unresolved, in date order (earliest claims first)."""
        return [
            t for t in self.ds.transactions
            if t.txn_id not in self.claimed_txn and t.txn_id not in self._screened
        ]

    def _open_invoices(self) -> list[Invoice]:
        """Invoices no transaction has settled yet."""
        return [i for i in self.ds.invoices if i.invoice_id not in self.claimed_invoice]

    # ------------------------------------------------------------------ #
    # Stage 0 - screening
    # ------------------------------------------------------------------ #

    def screen(self) -> None:
        """Remove statement lines that cannot settle a receivable at all."""
        for txn in self.ds.transactions:
            if rules.is_receipt(txn):
                continue
            self._screened.add(txn.txn_id)
            self.exceptions.append(
                ReconException(
                    record_id=txn.txn_id,
                    record_type="transaction",
                    reason=(
                        f"debit of {abs(txn.amount):,.2f} {txn.currency} to "
                        f"'{txn.counterparty_raw}' - money leaving the account cannot "
                        f"clear an open receivable; memo '{txn.description}'"
                    ),
                    tier_reached="screen",
                )
            )

    # ------------------------------------------------------------------ #
    # Tier 1 - exact
    # ------------------------------------------------------------------ #

    def tier1_exact(self) -> None:
        """Match on a quoted invoice reference, or on an exact same-currency amount.

        A quoted reference is only honoured when the amount also reconciles. A memo
        can quote the right invoice on a re-posted duplicate, or quote a stale
        reference, so the number alone is treated as a strong hint rather than proof.
        """
        lo, hi = rules.TIGHT_DATE_WINDOW
        for txn in self._open_txns():
            if self._match_by_reference(txn):
                continue
            # Uniqueness is tested across the *whole* date range, not just the tight
            # window. Otherwise a second invoice for the same customer at the same
            # amount, sitting a day outside the window, is invisible here and Tier 1
            # posts a coin-flip as a certainty.
            exact_anywhere = [
                c for c in self._candidates(txn, rules.T1_NAME_FLOOR)
                if c.amount_basis == "exact"
            ]
            if len(exact_anywhere) != 1:
                continue
            c = exact_anywhere[0]
            if not lo <= c.date_gap_days <= hi:
                continue  # right amount, unusual timing - let Tier 2 weigh it
            self._claim(
                txn.txn_id, [c.invoice_id], "T1_EXACT", c.confidence,
                f"exact {txn.currency} {txn.amount:,.2f}, counterparty similarity "
                f"{c.name_score:.0f}, {c.date_gap_days:+d}d from due date, "
                f"sole exact-amount candidate within "
                f"{rules.MAX_DATE_GAP_DAYS}d",
            )

    def _match_by_reference(self, txn: Transaction) -> bool:
        """Try to settle a transaction using an invoice id quoted in its memo.

        Returns:
            ``True`` if the reference produced a match, ``False`` to fall through to
            amount-based matching.
        """
        for ref in extract_invoice_refs(txn.description):
            inv = self.inv_by_id.get(ref)
            if inv is None or inv.invoice_id in self.claimed_invoice:
                continue
            verdict = rules.explain_amount(txn, inv, self.rates)
            if not verdict.explained:
                continue
            gap = rules.date_gap_days(txn.date, inv)
            self._claim(
                txn.txn_id, [inv.invoice_id], "T1_EXACT", 0.99,
                f"memo quotes {inv.invoice_id} and the amount reconciles "
                f"({verdict.basis}), {gap:+d}d from due date",
            )
            return True
        return False

    # ------------------------------------------------------------------ #
    # Tier 2a - structural (still deterministic)
    # ------------------------------------------------------------------ #

    def tier2_structural(self) -> None:
        """Resolve many-to-one and one-to-many settlements by exact subset sums."""
        self._match_consolidated()
        self._match_partial()

    def _same_party_invoices(self, txn: Transaction) -> list[Invoice]:
        """Open invoices whose customer plausibly matches this transaction's payer."""
        return [
            inv for inv in self._open_invoices()
            if inv.currency == txn.currency
            and party_similarity(txn.counterparty_raw, inv.customer_name)
            >= rules.STRUCTURAL_NAME_FLOOR
        ]

    def _match_consolidated(self) -> None:
        """One credit clearing several of the same customer's invoices."""
        for txn in self._open_txns():
            pool = self._same_party_invoices(txn)
            if not 2 <= len(pool) <= SUBSET_SEARCH_CAP:
                continue
            combo = _find_subset(
                pool, txn.amount, range(2, MAX_CONSOLIDATED_INVOICES + 1),
                lambda i: i.amount,
            )
            if combo is None:
                continue
            gaps = [rules.date_gap_days(txn.date, i) for i in combo]
            ids = [i.invoice_id for i in combo]
            self._claim(
                txn.txn_id, ids, "T2_STRUCTURAL", 0.95,
                f"one credit of {txn.amount:,.2f} {txn.currency} equals the exact sum "
                f"of {len(ids)} open invoices for the same customer "
                f"({', '.join(ids)}), {min(gaps):+d}..{max(gaps):+d}d from due",
            )

    def _match_partial(self) -> None:
        """Several instalments clearing one invoice."""
        for inv in self._open_invoices():
            pool = [
                t for t in self._open_txns()
                if t.currency == inv.currency
                and party_similarity(t.counterparty_raw, inv.customer_name)
                >= rules.STRUCTURAL_NAME_FLOOR
                and -rules.MAX_DATE_GAP_DAYS <= rules.date_gap_days(t.date, inv)
                <= rules.MAX_DATE_GAP_DAYS
            ]
            if not 2 <= len(pool) <= SUBSET_SEARCH_CAP:
                continue
            combo = _find_subset(
                pool, inv.amount, range(2, MAX_PARTIAL_INSTALMENTS + 1),
                lambda t: t.amount,
            )
            if combo is None:
                continue
            ids = [t.txn_id for t in combo]
            for idx, t in enumerate(combo, start=1):
                self._claim(
                    t.txn_id, [inv.invoice_id], "T2_STRUCTURAL", 0.93,
                    f"instalment {idx} of {len(combo)} ({', '.join(ids)}) summing "
                    f"exactly to {inv.invoice_id} at {inv.amount:,.2f} {inv.currency}",
                )
            # _claim marks the invoice claimed on the first instalment; the rest
            # attach to the same invoice deliberately.

    # ------------------------------------------------------------------ #
    # Tier 2b - fuzzy
    # ------------------------------------------------------------------ #

    def tier2_fuzzy(self) -> None:
        """Score the remaining transactions and accept only confident, clear winners."""
        for txn in self._open_txns():
            cands = self._candidates(txn, rules.T2_NAME_FLOOR)
            if not cands:
                continue
            best = cands[0]
            runner_up = cands[1].confidence if len(cands) > 1 else 0.0
            margin = best.confidence - runner_up
            if best.confidence < rules.T2_CONFIDENCE_THRESHOLD:
                continue
            if margin < rules.T2_MARGIN_THRESHOLD:
                continue  # genuinely ambiguous - escalate rather than guess
            self._claim(
                txn.txn_id, [best.invoice_id], "T2_FUZZY", best.confidence,
                f"counterparty '{txn.counterparty_raw}' ~ "
                f"'{self.inv_by_id[best.invoice_id].customer_name}' at "
                f"{best.name_score:.0f}, amount {best.amount_basis}, "
                f"{best.date_gap_days:+d}d from due, margin {margin:.2f} over next best",
            )

    # ------------------------------------------------------------------ #
    # Tier 3 - LLM
    # ------------------------------------------------------------------ #

    def tier3_llm(self) -> None:
        """Escalate what is left to the model, with a validated candidate shortlist."""
        if not self.llm.available:
            return
        for txn in self._open_txns():
            shortlist = self._llm_shortlist(txn)
            if not shortlist:
                continue
            decision = self.llm.decide(txn, self.ds.invoices, shortlist)
            if decision is None:
                continue
            self._llm_notes[txn.txn_id] = f"{decision.reason} (model confidence {decision.confidence:.2f})"
            if decision.invoice_id is None:
                continue
            if decision.confidence < rules.T3_CONFIDENCE_THRESHOLD:
                continue
            if decision.invoice_id in self.claimed_invoice:
                continue
            self._claim(
                txn.txn_id, [decision.invoice_id], "T3_LLM", decision.confidence,
                f"LLM: {decision.reason}",
            )

    def _llm_shortlist(self, txn: Transaction) -> list[Candidate]:
        """Pick the candidates Tier 3 gets to see.

        Amount-unexplained pairings are deliberately included: the model needs to see
        the near misses to be able to say "none of these", and refusing is a valid
        and common answer at this stage.
        """
        cands = self._candidates(
            txn, rules.LLM_NAME_FLOOR, require_explained_amount=False
        )
        return cands[:LLM_SHORTLIST]

    # ------------------------------------------------------------------ #
    # Exceptions
    # ------------------------------------------------------------------ #

    def build_exceptions(self) -> None:
        """Write a specific reason for every record the agent could not resolve."""
        for txn in self._open_txns():
            self.exceptions.append(self._transaction_exception(txn))
        for inv in self._open_invoices():
            self.exceptions.append(self._invoice_exception(inv))

    def _transaction_exception(self, txn: Transaction) -> ReconException:
        """Diagnose exactly why one transaction went unmatched."""
        tier = "T3_LLM" if self.llm.available else "T2_FUZZY"
        llm_note = self._llm_notes.get(txn.txn_id)

        # Look at the whole landscape, including invoices other transactions took.
        everything = self._candidates(
            txn, name_floor=0.0, only_unclaimed=False, require_explained_amount=False
        )
        if not everything:
            reason = (
                f"no invoice within {rules.MAX_DATE_GAP_DAYS} days of "
                f"{txn.date.isoformat()} for any customer; counterparty "
                f"'{txn.counterparty_raw}' at {txn.amount:,.2f} {txn.currency} "
                f"with memo '{txn.description}' has no receivable behind it"
            )
            return ReconException(record_id=txn.txn_id, record_type="transaction", reason=reason, tier_reached=tier, best_candidate=None)

        best = everything[0]
        best_inv = self.inv_by_id[best.invoice_id]

        # 1. Already settled by an earlier, identical posting -> duplicate.
        claimant = self.claimed_invoice.get(best.invoice_id)
        if claimant and claimant != txn.txn_id:
            other = self.txn_by_id[claimant]
            same_amount = abs(other.amount - txn.amount) <= rules.AMOUNT_EXACT_TOL
            same_party = (
                party_similarity(other.counterparty_raw, txn.counterparty_raw) >= 95
            )
            if same_amount and same_party and other.date <= txn.date:
                lag = (txn.date - other.date).days
                reason = (
                    f"{best.invoice_id} was already settled by {claimant} on "
                    f"{other.date.isoformat()} for the identical amount "
                    f"{txn.amount:,.2f} {txn.currency} from the same counterparty; "
                    f"this line posts {lag} day(s) later and clears no further "
                    f"receivable (suspected duplicate re-posting)"
                )
                return ReconException(record_id=txn.txn_id, record_type="transaction", reason=reason, tier_reached=tier, best_candidate=best.describe())

        # 2. Two or more open invoices are equally plausible.
        open_cands = [c for c in everything if c.invoice_id not in self.claimed_invoice]
        explained = [
            c for c in open_cands
            if c.amount_basis == "exact" or c.amount_basis.startswith(("wire fee", "fx "))
        ]
        if len(explained) >= 2 and (
            explained[0].confidence - explained[1].confidence < rules.T2_MARGIN_THRESHOLD
        ):
            a, b = explained[0], explained[1]
            inv_a, inv_b = self.inv_by_id[a.invoice_id], self.inv_by_id[b.invoice_id]
            reason = (
                f"{len(explained)} invoices equally plausible: {a.invoice_id} and "
                f"{b.invoice_id}, both '{inv_a.customer_name}' at "
                f"{inv_a.amount:,.2f} vs {inv_b.amount:,.2f} {inv_a.currency}; "
                f"confidence margin only {a.confidence - b.confidence:.3f} and the "
                f"memo '{txn.description}' quotes no reference"
            )
            return ReconException(record_id=txn.txn_id, record_type="transaction", reason=_join(reason, llm_note), tier_reached=tier, best_candidate=a.describe())

        # 3. Best open candidate is right on name and date but wrong on money.
        if open_cands:
            top = open_cands[0]
            top_inv = self.inv_by_id[top.invoice_id]
            if top.amount_basis in ("unexplained",) or top.amount_basis.startswith("fx gap"):
                pct = abs(top.amount_delta) / top_inv.amount * 100 if top_inv.amount else 0.0
                reason = (
                    f"closest invoice {top.invoice_id} matches on counterparty "
                    f"(similarity {top.name_score:.0f}) and timing "
                    f"({top.date_gap_days:+d}d from due) but the amount differs by "
                    f"{top.amount_delta:+,.2f} {txn.currency} ({pct:.1f}%); no wire-fee "
                    f"rule (<= {rules.FEE_MAX_ABS:.2f}) or FX conversion at the "
                    f"published rate explains a gap that size"
                )
                return ReconException(record_id=txn.txn_id, record_type="transaction", reason=_join(reason, llm_note), tier_reached=tier, best_candidate=top.describe())

            reason = (
                f"best candidate {top.invoice_id} reached confidence "
                f"{top.confidence:.2f}, below the {rules.T2_CONFIDENCE_THRESHOLD:.2f} "
                f"acceptance threshold: counterparty similarity {top.name_score:.0f} "
                f"('{txn.counterparty_raw}' vs '{top_inv.customer_name}'), amount "
                f"{top.amount_basis}, {top.date_gap_days:+d}d from due date"
            )
            return ReconException(record_id=txn.txn_id, record_type="transaction", reason=_join(reason, llm_note), tier_reached=tier, best_candidate=top.describe())

        # 4. Every plausible invoice is already spoken for.
        reason = (
            f"every invoice resembling '{txn.counterparty_raw}' within "
            f"{rules.MAX_DATE_GAP_DAYS} days is already settled; nearest is "
            f"{best.invoice_id} ({best_inv.customer_name}, {best_inv.amount:,.2f} "
            f"{best_inv.currency}) claimed by {self.claimed_invoice.get(best.invoice_id)}"
        )
        return ReconException(record_id=txn.txn_id, record_type="transaction", reason=_join(reason, llm_note), tier_reached=tier, best_candidate=best.describe())

    def _invoice_exception(self, inv: Invoice) -> ReconException:
        """Diagnose exactly why one invoice was left open."""
        near: list[tuple[float, Transaction, float]] = []
        for txn in self.ds.transactions:
            if txn.txn_id in self._screened:
                continue
            gap = rules.date_gap_days(txn.date, inv)
            if abs(gap) > rules.MAX_DATE_GAP_DAYS:
                continue
            sim = party_similarity(txn.counterparty_raw, inv.customer_name)
            if sim < rules.T2_NAME_FLOOR:
                continue
            near.append((sim, txn, txn.amount - inv.amount))

        if not near:
            reason = (
                f"no credit from a counterparty resembling '{inv.customer_name}' "
                f"landed within {rules.MAX_DATE_GAP_DAYS} days of the "
                f"{inv.due_date.isoformat()} due date; the receivable appears unpaid"
            )
            return ReconException(record_id=inv.invoice_id, record_type="invoice", reason=reason, tier_reached="unpaid", best_candidate=None)

        near.sort(key=lambda x: (-x[0], abs(x[2])))
        sim, txn, delta = near[0]
        state = ("already applied to " + self.claimed_txn[txn.txn_id]
                 if txn.txn_id in self.claimed_txn else "itself unmatched")
        reason = (
            f"open at {inv.amount:,.2f} {inv.currency}; the closest credit is "
            f"{txn.txn_id} on {txn.date.isoformat()} for {txn.amount:,.2f} "
            f"{txn.currency} (counterparty similarity {sim:.0f}, differs by "
            f"{delta:+,.2f}), and it is {state}"
        )
        return ReconException(record_id=inv.invoice_id, record_type="invoice", reason=reason, tier_reached="unpaid", best_candidate=None)

    # ------------------------------------------------------------------ #

    def run(self) -> ReconResult:
        """Execute every stage over the full batch and return the result.

        Returns:
            A :class:`ReconResult` with the matches, the exception list, per-tier
            counts, wall-clock time and Tier 3 call statistics.
        """
        started = time.perf_counter()
        self.screen()
        self.tier1_exact()
        self.tier2_structural()
        self.tier2_fuzzy()
        self.tier3_llm()
        self.build_exceptions()
        elapsed = time.perf_counter() - started

        tier_counts: dict[str, int] = {}
        for m in self.matches:
            tier_counts[m.tier] = tier_counts.get(m.tier, 0) + 1

        return ReconResult(
            matches=self.matches,
            exceptions=self.exceptions,
            elapsed_seconds=elapsed,
            n_transactions=len(self.ds.transactions),
            n_invoices=len(self.ds.invoices),
            llm_stats=self.llm.stats.as_dict(),
            llm_available=self.llm.available,
            tier_counts=tier_counts,
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _find_subset(
    items: Sequence[T],
    target: float,
    sizes: Iterable[int],
    key: Callable[[T], float],
) -> tuple[T, ...] | None:
    """Find a combination of ``items`` whose ``key`` values sum to ``target``.

    Smaller combinations are tried first, so a two-invoice explanation is preferred
    over a coincidental three-invoice one.

    Args:
        items: Candidate records, already filtered to one customer and currency.
        target: The amount to hit.
        sizes: Combination sizes to try, in preference order.
        key: Extracts the amount from a record.

    Returns:
        The first combination summing to ``target`` within :data:`SUBSET_TOL`, or
        ``None``.
    """
    for size in sizes:
        if size > len(items):
            break
        for combo in itertools.combinations(items, size):
            if abs(sum(key(x) for x in combo) - target) <= SUBSET_TOL:
                return combo
    return None


def _join(reason: str, note: str | None) -> str:
    """Append the model's own explanation to a deterministic reason, if there is one."""
    return f"{reason}. LLM agreed no match: {note}" if note else reason
