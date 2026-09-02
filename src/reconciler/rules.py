"""Business rules that decide whether a discrepancy is explainable.

Every threshold in this module is a policy a controller could defend in writing, not
a number tuned to make one row pass. They are collected here so the README and
`DECISIONS.md` can quote them and so a reviewer can change one and re-measure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .models import Invoice, Transaction

# --------------------------------------------------------------------------- #
# Policy thresholds
# --------------------------------------------------------------------------- #

#: Cash matches to the cent; anything looser is a discrepancy needing an explanation.
AMOUNT_EXACT_TOL = 0.01

#: A correspondent-bank wire fee is a small fixed charge. A shortfall is treated as a
#: fee only if it is small in absolute terms *and* immaterial against the invoice.
FEE_MAX_ABS = 60.00
FEE_MAX_PCT = 0.02

#: Spread between the published month-average rate and the rate actually applied.
FX_TOLERANCE_PCT = 0.015

#: Days either side of the due date that count as "paid on time" - full date score.
TIGHT_DATE_WINDOW = (-10, 15)

#: Beyond this many days from the due date a pairing is not considered at all.
MAX_DATE_GAP_DAYS = 90

#: Counterparty similarity floors for each tier.
T1_NAME_FLOOR = 88.0
T2_NAME_FLOOR = 62.0
STRUCTURAL_NAME_FLOOR = 85.0
LLM_NAME_FLOOR = 45.0

#: Tier 2 accepts a pairing only above this confidence...
T2_CONFIDENCE_THRESHOLD = 0.75
#: ...and only if it beats the runner-up by this margin. Below the margin the pairing
#: is genuinely ambiguous and is escalated rather than guessed.
T2_MARGIN_THRESHOLD = 0.06

#: Tier 3 accepts the model's answer only above this self-reported confidence.
T3_CONFIDENCE_THRESHOLD = 0.70

#: Confidence blend weights. Counterparty carries the most signal because amount and
#: date collide often across a 100-invoice ledger; a name does not.
W_NAME, W_AMOUNT, W_DATE = 0.45, 0.35, 0.20


@dataclass(frozen=True)
class AmountVerdict:
    """Whether a transaction/invoice amount gap has a business explanation."""

    basis: str
    delta: float
    score: float
    explained: bool


def convert(amount: float, from_ccy: str, to_ccy: str, rates_to_usd: dict[str, float]) -> float:
    """Convert an amount between currencies using the published rate table.

    Args:
        amount: The amount to convert.
        from_ccy: Currency the amount is denominated in.
        to_ccy: Target currency.
        rates_to_usd: Month-average rates, each currency expressed in USD.

    Returns:
        The converted amount, or the input unchanged when either currency is absent
        from the table (the caller then sees an unexplained gap rather than a
        fabricated conversion).
    """
    if from_ccy == to_ccy:
        return amount
    if from_ccy not in rates_to_usd or to_ccy not in rates_to_usd:
        return amount
    return amount * rates_to_usd[from_ccy] / rates_to_usd[to_ccy]


def explain_amount(
    txn: Transaction, inv: Invoice, rates_to_usd: dict[str, float]
) -> AmountVerdict:
    """Decide whether the gap between a credit and an invoice is explainable.

    Recognised explanations, in order of strength:

    * **exact** - same currency, equal to the cent.
    * **fx** - different currencies, and the credit lands within
      :data:`FX_TOLERANCE_PCT` of the invoice converted at the published rate.
    * **wire fee** - same currency, the credit is *short* by no more than
      :data:`FEE_MAX_ABS` and no more than :data:`FEE_MAX_PCT` of the invoice.

    An overpayment, or a shortfall too large to be a fee, is deliberately left
    unexplained: silently absorbing it is how reconciliation tools lose money.

    Args:
        txn: The bank credit under consideration.
        inv: The candidate invoice.
        rates_to_usd: The published FX table.

    Returns:
        An :class:`AmountVerdict` carrying a human-readable basis, the signed delta
        in the transaction's currency, a 0-1 score and whether it is acceptable.
    """
    expected = convert(inv.amount, inv.currency, txn.currency, rates_to_usd)
    delta = round(txn.amount - expected, 2)

    if txn.currency == inv.currency and abs(delta) <= AMOUNT_EXACT_TOL:
        return AmountVerdict("exact", delta, 1.0, True)

    if txn.currency != inv.currency:
        if expected > 0 and abs(delta) <= expected * FX_TOLERANCE_PCT:
            pct = delta / expected * 100
            return AmountVerdict(
                f"fx {inv.currency}->{txn.currency} {pct:+.2f}% vs table rate",
                delta, 0.85, True)
        return AmountVerdict(
            f"fx gap {delta:+.2f} exceeds {FX_TOLERANCE_PCT:.1%} tolerance",
            delta, 0.0, False)

    shortfall = -delta
    if 0 < shortfall <= min(FEE_MAX_ABS, inv.amount * FEE_MAX_PCT):
        return AmountVerdict(f"wire fee {shortfall:.2f}", delta, 0.90, True)

    return AmountVerdict("unexplained", delta, 0.0, False)


def date_gap_days(txn_date: date, inv: Invoice) -> int:
    """Signed days from an invoice's due date to the settlement date."""
    return (txn_date - inv.due_date).days


def date_score(gap: int) -> float:
    """Score settlement timing, full marks inside the normal window then decaying.

    Args:
        gap: Signed days from due date to payment, as returned by
            :func:`date_gap_days`.

    Returns:
        ``1.0`` inside :data:`TIGHT_DATE_WINDOW`, decaying linearly to ``0.0`` at
        :data:`MAX_DATE_GAP_DAYS`, and ``0.0`` beyond it.
    """
    lo, hi = TIGHT_DATE_WINDOW
    if lo <= gap <= hi:
        return 1.0
    overshoot = (gap - hi) if gap > hi else (lo - gap)
    edge = MAX_DATE_GAP_DAYS - (hi if gap > hi else -lo)
    if overshoot >= edge:
        return 0.0
    return max(0.0, 1.0 - overshoot / edge)


def confidence(name_score: float, amount: AmountVerdict, gap: int) -> float:
    """Blend the three signals into a single 0-1 confidence.

    Args:
        name_score: Counterparty similarity in ``[0, 100]``.
        amount: The verdict from :func:`explain_amount`.
        gap: Signed days from due date to payment.

    Returns:
        A confidence in ``[0, 1]``. An unexplained amount drives the amount term to
        zero, which alone keeps a pairing under the Tier 2 threshold.
    """
    return round(
        W_NAME * (name_score / 100.0)
        + W_AMOUNT * amount.score
        + W_DATE * date_score(gap),
        4,
    )


def is_receipt(txn: Transaction) -> bool:
    """Return whether a statement line could settle a receivable at all.

    A debit moves money out; it can never clear an open invoice. Screening these
    out first keeps them out of the fuzzy tiers and gives them an honest exception
    reason rather than a low-confidence near miss.
    """
    return txn.amount > 0
