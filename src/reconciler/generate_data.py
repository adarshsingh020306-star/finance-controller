"""Synthetic bank-statement / invoice dataset generator with full ground truth.

Emits four files into ``data/``:

* ``bank_transactions.csv``  - 120 rows: date, amount, currency, description,
  counterparty_raw, txn_id
* ``invoices.csv``           - 100 rows: invoice_id, issue_date, due_date, amount,
  currency, customer_name, status
* ``ground_truth.csv``       - the true txn -> invoice mapping, including rows that
  are genuinely unmatchable on either side
* ``fx_rates.json``          - the static month-average rate table the reconciler is
  allowed to use (a treasury desk would hand you exactly this)

=============================================================================
PLANTED CASE PLAN  (asserted in _verify(); the generator fails loudly if the
emitted data ever drifts from these numbers)
=============================================================================

                          invoices   transactions   notes
  CLEAN                        48         48        exact amount+currency, tight
                                                    date window, near-literal name
  NAME_NOISE                   12         12        1:1 but the counterparty string
                                                    is mangled by the payment rail
  CONSOLIDATED                  5          2        2 groups (3 invoices -> 1 txn,
                                                    2 invoices -> 1 txn)
  PARTIAL                       4         10        2 invoices split in 2, 2 split
                                                    in 3
  FX_DIFF                       4          4        invoice in EUR/GBP, credit lands
                                                    in USD off the table rate by
                                                    0.4%-1.2%
  FEE_DEDUCTION                 4          4        wire / intermediary bank fee
                                                    subtracted from the credit
  DATE_DRIFT                    5          5        3 very late (38-59d past due),
                                                    2 very early (paid at issue)
  DUPLICATE                     8          8        genuine credits ...
  DUPLICATE_ARTIFACT            -          8        ... each re-posted by the bank;
                                                    only the earlier one is real
  UNPAID                        6          0        invoices nobody ever paid
  AMBIGUOUS                     4          2        2 scenarios; each = same
                                                    customer, 2 open invoices of the
                                                    identical amount, 1 unreferenced
                                                    payment
  NO_INVOICE                    0         17        11 hard negatives that look like
                                                    customer receipts, 6 obvious
                                                    non-AR bank lines
  --------------------------------------------------------------------------
  TOTAL                       100        120

Ground-truth rows = 98 true (txn, invoice) pairs
                  + 25 unmatchable transactions (8 duplicate artifacts + 17 no-invoice)
                  +  8 unmatchable invoices (6 never paid + 2 ambiguous losers)
                  = 131 rows

=============================================================================
DELIBERATE NON-LEAKAGE DECISIONS
=============================================================================
1. ``invoices.status`` is only ``open`` or ``overdue``, derived purely from due_date
   vs the statement cutoff. A real AR ledger's "paid" flag is an *output* of
   reconciliation, so exposing it here would leak the answer.
2. 18 of the 48 CLEAN transactions carry a literal invoice reference in the memo
   line, because real remittance advice often does. Ground truth records this in
   ``has_invoice_ref`` so the report can state honestly how many matches were won
   by a literal reference rather than by inference.
3. DUPLICATE convention: the *earlier* posting is the real one. Same-day coin-flip
   duplicates would make the ground truth arbitrary, so every artifact re-posting
   lands 1-3 days after its original. This is a documented dataset convention, not
   a hint hidden in the text.
4. AMBIGUOUS convention: the payment is applied to the *older* of the two identical
   invoices (FIFO). Ground truth flags these ``ambiguous=yes`` - a human holding
   only these two files cannot do better than the convention either, so the report
   is expected to count them as genuinely hard rather than as free wins.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

STATEMENT_START = date(2025, 1, 10)
STATEMENT_CUTOFF = date(2025, 7, 15)
INVOICE_WINDOW = (date(2025, 1, 5), date(2025, 5, 30))
PAYMENT_TERMS_DAYS = (15, 30, 45, 60)

#: Static month-average rates published to the reconciler (base currency -> USD).
FX_RATES_TO_USD: dict[str, float] = {
    "USD": 1.0,
    "EUR": 1.0850,
    "GBP": 1.2720,
    "INR": 0.01198,
}

#: Wire / intermediary-bank fees a correspondent may deduct in transit.
BANK_FEES = (12.50, 15.00, 25.00, 30.00, 40.00)

CUSTOMERS: tuple[str, ...] = (
    "Acme Corporation", "Northwind Trading", "Bluepeak Solutions",
    "Helvetica Logistics", "Kestrel Analytics", "Orion Manufacturing",
    "Sable & Finch Partners", "Vantage Point Media", "Redwood Instruments",
    "Aurora Biotech", "Quantum Ledger Systems", "Meridian Freight",
    "Silverline Apparel", "Copperfield Energy", "Lumen Digital Works",
    "Harbourview Consulting", "Ironbark Constructions", "Pinnacle Foods",
    "Cobalt Robotics", "Emberline Textiles", "Fairmont Chemicals",
    "Greystone Advisory", "Hollowbrook Farms", "Indigo Wave Studios",
    "Juniper Health Systems", "Kite & Compass Travel", "Larkspur Publishing",
    "Marlowe Aerospace", "Nimbus Cloudworks", "Oakhaven Furniture",
    "Palisade Security", "Quarry Lane Ceramics", "Rosewood Pharmaceuticals",
    "Stonebridge Insurance", "Thornfield Motors", "Umbra Optics",
    "Verdant Agriculture", "Westgate Shipping", "Xenon Power Systems",
    "Yarrow Naturals", "Zephyr Telecom", "Basalt Mining Group",
    "Cinder Peak Outfitters", "Driftwood Hospitality", "Everglade Water Tech",
)

#: Non-AR bank lines that should never be reconciled to an invoice at all.
NON_AR_MEMOS: tuple[tuple[str, str], ...] = (
    ("MONTHLY ACCOUNT MAINTENANCE FEE", "FIRST MERIDIAN BANK"),
    ("PAYROLL RUN 2025-04 BATCH 0417", "PAYROLL CLEARING"),
    ("OFFICE LEASE APR 2025 UNIT 4B", "CASTLEGATE PROPERTIES LLC"),
    ("INTEREST CREDIT - SWEEP ACCOUNT", "FIRST MERIDIAN BANK"),
    ("CORPORATE CARD SETTLEMENT", "APEX CARD SERVICES"),
    ("VAT REFUND Q1 2025", "HM REVENUE AND CUSTOMS"),
)


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #

@dataclass
class Invoice:
    """One row of ``invoices.csv`` plus the bookkeeping the generator needs."""

    key: str
    customer: str
    issue_date: date
    due_date: date
    amount: float
    currency: str
    case_type: str
    invoice_id: str = ""

    def row(self) -> dict[str, Any]:
        """Return the public CSV row - no case labels, no payment state."""
        status = "overdue" if self.due_date < STATEMENT_CUTOFF else "open"
        return {
            "invoice_id": self.invoice_id,
            "issue_date": self.issue_date.isoformat(),
            "due_date": self.due_date.isoformat(),
            "amount": f"{self.amount:.2f}",
            "currency": self.currency,
            "customer_name": self.customer,
            "status": status,
        }


@dataclass
class Txn:
    """One row of ``bank_transactions.csv`` plus generator bookkeeping."""

    key: str
    txn_date: date
    amount: float
    currency: str
    description: str
    counterparty_raw: str
    case_type: str
    #: invoice keys this transaction truly settles (may be 0, 1 or many)
    settles: list[str] = field(default_factory=list)
    note: str = ""
    has_invoice_ref: bool = False
    ambiguous: bool = False
    txn_id: str = ""

    def row(self) -> dict[str, Any]:
        """Return the public CSV row - no case labels, no linkage."""
        return {
            "txn_id": self.txn_id,
            "date": self.txn_date.isoformat(),
            "amount": f"{self.amount:.2f}",
            "currency": self.currency,
            "description": self.description,
            "counterparty_raw": self.counterparty_raw,
        }


# --------------------------------------------------------------------------- #
# Counterparty string mangling
# --------------------------------------------------------------------------- #

_LEGAL_SUFFIXES = ("PVT LTD", "LLC", "INC", "GMBH", "LTD", "CO", "SA")
_ABBREVIATIONS = {
    "International": "INTL", "Technologies": "TECH", "Solutions": "SOLNS",
    "Corporation": "CORP", "Manufacturing": "MFG", "Systems": "SYS",
    "Partners": "PTNRS", "Consulting": "CONSLT", "Logistics": "LOGISTIC",
    "Analytics": "ANALYT", "Pharmaceuticals": "PHARMA", "Instruments": "INSTR",
    "Publishing": "PUBL", "Hospitality": "HOSP", "Agriculture": "AGRI",
    "Insurance": "INSUR", "Telecom": "TELCO", "Shipping": "SHIP",
}


def clean_bank_name(name: str) -> str:
    """Uppercase a customer name the way a bank statement would, with no other noise.

    Args:
        name: The customer name exactly as it appears on the invoice.

    Returns:
        The all-caps bank rendering used for CLEAN and other non-name-noise cases.
    """
    return name.upper().replace("&", "AND")


def mangle_name(name: str, style: int, rng: random.Random) -> str:
    """Distort a customer name the way a specific payment rail would.

    Each ``style`` is a distinct real-world corruption, so the test set exercises
    several different fuzzy-matching weaknesses rather than one repeated trick.

    Args:
        name: The clean customer name from the invoice.
        style: Which corruption to apply, 0-6.
        rng: Seeded RNG, used for reference numbers and account masks.

    Returns:
        The corrupted counterparty string as it would land on the statement.
    """
    upper = clean_bank_name(name)
    if style == 0:  # legal-suffix swap: "Acme Corporation" -> "ACME CORP PVT LTD"
        return f"{upper.split()[0]} CORP {rng.choice(_LEGAL_SUFFIXES)}"
    if style == 1:  # word-by-word abbreviation
        return " ".join(_ABBREVIATIONS.get(w, w).upper() for w in name.split())
    if style == 2:  # trailing reference number
        return f"{upper} /REF {rng.randint(10000, 99999)}/"
    if style == 3:  # hard truncation at the rail's field width
        return upper[:18].strip()
    if style == 4:  # rail prefix plus masked account number
        rail = rng.choice(("NEFT", "ACH CREDIT", "SEPA CT", "FEDWIRE"))
        return f"{rail}-{upper}-XXXXX{rng.randint(1000, 9999)}"
    if style == 5:  # whitespace collapsed, punctuation dropped
        return "".join(ch for ch in upper if ch.isalnum())
    # style 6: leading word rotated to the end
    words = upper.split()
    return " ".join(words[1:] + words[:1]) if len(words) > 1 else upper


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #

class DatasetBuilder:
    """Builds invoices, transactions and ground truth from the planted case plan."""

    def __init__(self, seed: int = 20260903) -> None:
        self.rng = random.Random(seed)
        self.invoices: list[Invoice] = []
        self.txns: list[Txn] = []
        self._n = 0

    # -- small helpers ----------------------------------------------------- #

    def _next_key(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n:04d}"

    def _amount(self, low: float = 500.0, high: float = 95000.0) -> float:
        """Draw a plausible B2B invoice amount, rounded to cents."""
        return round(self.rng.uniform(low, high), 2)

    def _issue_date(self) -> date:
        span = (INVOICE_WINDOW[1] - INVOICE_WINDOW[0]).days
        return INVOICE_WINDOW[0] + timedelta(days=self.rng.randint(0, span))

    def new_invoice(
        self,
        case_type: str,
        customer: str | None = None,
        currency: str = "USD",
        amount: float | None = None,
        issue: date | None = None,
        terms: int | None = None,
    ) -> Invoice:
        """Create and register one invoice, filling any field left unspecified."""
        issue = issue or self._issue_date()
        terms = terms or self.rng.choice(PAYMENT_TERMS_DAYS)
        inv = Invoice(
            key=self._next_key("INVK"),
            customer=customer or self.rng.choice(CUSTOMERS),
            issue_date=issue,
            due_date=issue + timedelta(days=terms),
            amount=amount if amount is not None else self._amount(),
            currency=currency,
            case_type=case_type,
        )
        self.invoices.append(inv)
        return inv

    def new_txn(self, **kwargs: Any) -> Txn:
        """Create and register one bank transaction."""
        txn = Txn(key=self._next_key("TXNK"), **kwargs)
        self.txns.append(txn)
        return txn

    def _pay_date(self, inv: Invoice, lo: int = -6, hi: int = 9) -> date:
        """Pick a settlement date near the due date, clamped to the statement window."""
        d = inv.due_date + timedelta(days=self.rng.randint(lo, hi))
        return min(max(d, max(STATEMENT_START, inv.issue_date)), STATEMENT_CUTOFF)

    def _memo(self, inv: Invoice, with_ref: bool) -> str:
        """Build a remittance memo, optionally quoting the literal invoice id."""
        if with_ref:
            return self.rng.choice((
                f"PAYMENT FOR {inv.invoice_id}",
                f"REMITTANCE REF {inv.invoice_id}",
                f"SETTLEMENT {inv.invoice_id} THANK YOU",
            ))
        return self.rng.choice((
            "INCOMING WIRE - CUSTOMER PAYMENT",
            "ACH CREDIT RECEIVED",
            "CUSTOMER REMITTANCE",
            "INWARD CLEARING CREDIT",
            "ONLINE TRANSFER FROM CUSTOMER",
        ))

    # -- case blocks ------------------------------------------------------- #

    def build_clean(self, n: int = 48, n_with_ref: int = 18) -> None:
        """Straight 1:1 settlements: exact amount, same currency, tight date window."""
        for i in range(n):
            inv = self.new_invoice("CLEAN")
            self.new_txn(
                txn_date=self._pay_date(inv),
                amount=inv.amount,
                currency=inv.currency,
                description="",  # filled in once invoice ids exist
                counterparty_raw=clean_bank_name(inv.customer),
                case_type="CLEAN",
                settles=[inv.key],
                has_invoice_ref=i < n_with_ref,
                note="exact amount, same currency, payment near due date",
            )

    def build_name_noise(self, n: int = 12) -> None:
        """1:1 settlements where only the counterparty string is hostile.

        Some styles are no-ops on some names - abbreviation does nothing to a name
        with no abbreviatable word, truncation does nothing to a short one. Those
        rows would be secretly CLEAN and would inflate the planted count, so the
        customer is drawn until the chosen style actually changes the string.
        """
        for i in range(n):
            style = i % 7
            customer, raw = self._pick_manglable(style)
            inv = self.new_invoice("NAME_NOISE", customer=customer)
            self.new_txn(
                txn_date=self._pay_date(inv),
                amount=inv.amount,
                currency=inv.currency,
                description="INWARD CLEARING CREDIT",
                counterparty_raw=raw,
                case_type="NAME_NOISE",
                settles=[inv.key],
                note=f"counterparty mangled by rail style {style}",
            )

    def _pick_manglable(self, style: int) -> tuple[str, str]:
        """Find a customer whose name the given rail style genuinely corrupts.

        Args:
            style: The corruption style to apply, 0-6.

        Returns:
            ``(customer_name, mangled_counterparty)``.

        Raises:
            RuntimeError: If no customer in the pool is corrupted by this style.
        """
        candidates = list(CUSTOMERS)
        self.rng.shuffle(candidates)
        for customer in candidates:
            raw = mangle_name(customer, style, self.rng)
            if raw != clean_bank_name(customer):
                return customer, raw
        raise RuntimeError(f"no customer is altered by mangle style {style}")

    def build_consolidated(self) -> None:
        """One credit settling several invoices for the same customer."""
        for group_size in (3, 2):
            customer = self.rng.choice(CUSTOMERS)
            issue = self._issue_date()
            group = [
                self.new_invoice(
                    "CONSOLIDATED",
                    customer=customer,
                    issue=issue + timedelta(days=3 * k),
                    terms=30,
                )
                for k in range(group_size)
            ]
            total = round(sum(inv.amount for inv in group), 2)
            self.new_txn(
                txn_date=self._pay_date(group[-1], lo=0, hi=5),
                amount=total,
                currency="USD",
                description=f"BULK SETTLEMENT {group_size} INVOICES",
                counterparty_raw=clean_bank_name(customer),
                case_type="CONSOLIDATED",
                settles=[inv.key for inv in group],
                note=(f"single credit clears {group_size} invoices; "
                      "no individual invoice matches this amount"),
            )

    def build_partial(self) -> None:
        """One invoice settled by two or three separate transfers."""
        for parts in (2, 2, 3, 3):
            inv = self.new_invoice("PARTIAL", terms=45)
            for idx, part in enumerate(self._split_amount(inv.amount, parts)):
                self.new_txn(
                    txn_date=self._pay_date(inv, lo=-10 + 7 * idx, hi=-6 + 7 * idx),
                    amount=part,
                    currency=inv.currency,
                    description=f"PART PAYMENT {idx + 1} OF {parts}",
                    counterparty_raw=clean_bank_name(inv.customer),
                    case_type="PARTIAL",
                    settles=[inv.key],
                    note=f"instalment {idx + 1}/{parts}; only the sum equals the invoice",
                )

    def _split_amount(self, total: float, parts: int) -> list[float]:
        """Split an amount into ``parts`` uneven instalments that sum back exactly."""
        weights = [self.rng.uniform(0.8, 1.4) for _ in range(parts)]
        scale = total / sum(weights)
        out = [round(w * scale, 2) for w in weights[:-1]]
        out.append(round(total - sum(out), 2))
        return out

    def build_fx_diff(self, n: int = 4) -> None:
        """Invoice denominated in EUR/GBP, credit lands in USD off the table rate."""
        for i in range(n):
            ccy = "EUR" if i % 2 == 0 else "GBP"
            inv = self.new_invoice("FX_DIFF", currency=ccy)
            drift = self.rng.choice((-1, 1)) * self.rng.uniform(0.004, 0.012)
            usd = round(inv.amount * FX_RATES_TO_USD[ccy] * (1 + drift), 2)
            self.new_txn(
                txn_date=self._pay_date(inv),
                amount=usd,
                currency="USD",
                description="INWARD FX CREDIT - CONVERTED AT SPOT",
                counterparty_raw=clean_bank_name(inv.customer),
                case_type="FX_DIFF",
                settles=[inv.key],
                note=f"{ccy} invoice credited in USD; {drift * 100:+.2f}% off table rate",
            )

    def build_fee_deduction(self, n: int = 4) -> None:
        """Correspondent bank shaves a fixed fee off the credit in transit."""
        for i in range(n):
            inv = self.new_invoice("FEE_DEDUCTION")
            fee = BANK_FEES[i % len(BANK_FEES)]
            self.new_txn(
                txn_date=self._pay_date(inv),
                amount=round(inv.amount - fee, 2),
                currency=inv.currency,
                description="WIRE CREDIT NET OF CORRESPONDENT CHARGES",
                counterparty_raw=clean_bank_name(inv.customer),
                case_type="FEE_DEDUCTION",
                settles=[inv.key],
                note=f"credit is short by a {fee:.2f} wire fee",
            )

    def build_date_drift(self) -> None:
        """Payments far outside any sane date window: 3 very late, 2 very early."""
        for lag in (38, 47, 59):
            inv = self.new_invoice("DATE_DRIFT", terms=30)
            self.new_txn(
                txn_date=min(inv.due_date + timedelta(days=lag), STATEMENT_CUTOFF),
                amount=inv.amount,
                currency=inv.currency,
                description="LATE SETTLEMENT - CUSTOMER PAYMENT",
                counterparty_raw=clean_bank_name(inv.customer),
                case_type="DATE_DRIFT",
                settles=[inv.key],
                note=f"paid {lag} days past due",
            )
        for terms in (60, 45):
            inv = self.new_invoice("DATE_DRIFT", terms=terms)
            self.new_txn(
                txn_date=max(inv.issue_date + timedelta(days=1), STATEMENT_START),
                amount=inv.amount,
                currency=inv.currency,
                description="ADVANCE SETTLEMENT ON ISSUE",
                counterparty_raw=clean_bank_name(inv.customer),
                case_type="DATE_DRIFT",
                settles=[inv.key],
                note=f"prepaid ~{terms} days before due date",
            )

    def build_duplicates(self, n: int = 8) -> None:
        """A real credit plus a bank re-posting of it; only the earlier one is real."""
        for i in range(n):
            inv = self.new_invoice("DUPLICATE")
            pay = self._pay_date(inv)
            with_ref = i < 3
            real = self.new_txn(
                txn_date=pay,
                amount=inv.amount,
                currency=inv.currency,
                description="",
                counterparty_raw=clean_bank_name(inv.customer),
                case_type="DUPLICATE",
                settles=[inv.key],
                has_invoice_ref=with_ref,
                note="genuine credit; a duplicate re-posting of it also appears",
            )
            self.new_txn(
                txn_date=min(pay + timedelta(days=1 + i % 3), STATEMENT_CUTOFF),
                amount=inv.amount,
                currency=inv.currency,
                description="",
                counterparty_raw=real.counterparty_raw,
                case_type="DUPLICATE_ARTIFACT",
                settles=[],
                has_invoice_ref=with_ref,
                note="bank re-posting of an earlier credit; settles nothing",
            )

    def build_unpaid(self, n: int = 6) -> None:
        """Invoices that were simply never paid inside the statement window."""
        for _ in range(n):
            self.new_invoice("UNPAID")

    def build_ambiguous(self) -> None:
        """Same customer, two identical open invoices, one unreferenced payment.

        Ground truth applies the payment FIFO to the older invoice and flags the
        pair ambiguous - a human holding only these two files cannot do better.
        """
        for _ in range(2):
            customer = self.rng.choice(CUSTOMERS)
            amount = self._amount(8000, 40000)
            issue = self._issue_date()
            older = self.new_invoice(
                "AMBIGUOUS", customer=customer, amount=amount, issue=issue, terms=30
            )
            self.new_invoice(
                "AMBIGUOUS",
                customer=customer,
                amount=amount,
                issue=issue + timedelta(days=11),
                terms=30,
            )
            self.new_txn(
                txn_date=self._pay_date(older, lo=-2, hi=6),
                amount=amount,
                currency="USD",
                description="CUSTOMER REMITTANCE - NO REFERENCE QUOTED",
                counterparty_raw=clean_bank_name(customer),
                case_type="AMBIGUOUS",
                settles=[older.key],
                ambiguous=True,
                note=(f"customer has 2 open invoices of exactly {amount:.2f}; "
                      "no reference quoted - FIFO convention applied"),
            )

    def build_no_invoice(self, n_hard: int = 11, n_obvious: int = 6) -> None:
        """Credits and debits that correspond to no invoice at all."""
        span = (STATEMENT_CUTOFF - STATEMENT_START).days
        for _ in range(n_hard):
            self.new_txn(
                txn_date=STATEMENT_START + timedelta(days=self.rng.randint(0, span)),
                amount=self._amount(700, 60000),
                currency="USD",
                description=self.rng.choice((
                    "INCOMING WIRE - CUSTOMER PAYMENT",
                    "ACH CREDIT RECEIVED",
                    "DEPOSIT - UNIDENTIFIED REMITTER",
                    "CUSTOMER REMITTANCE",
                )),
                counterparty_raw=clean_bank_name(self.rng.choice(CUSTOMERS)),
                case_type="NO_INVOICE",
                settles=[],
                note="looks like a customer receipt but no invoice exists for it",
            )
        for i in range(n_obvious):
            memo, party = NON_AR_MEMOS[i % len(NON_AR_MEMOS)]
            sign = -1 if i % 2 == 0 else 1
            self.new_txn(
                txn_date=STATEMENT_START + timedelta(days=self.rng.randint(0, span)),
                amount=round(sign * self.rng.uniform(120, 18000), 2),
                currency="USD",
                description=memo,
                counterparty_raw=party,
                case_type="NO_INVOICE",
                settles=[],
                note="non-AR bank line (fee / payroll / lease / tax)",
            )

    # -- assembly ---------------------------------------------------------- #

    def build(self) -> None:
        """Run every case block, assign public ids, then verify the planted counts."""
        self.build_clean()
        self.build_name_noise()
        self.build_consolidated()
        self.build_partial()
        self.build_fx_diff()
        self.build_fee_deduction()
        self.build_date_drift()
        self.build_duplicates()
        self.build_unpaid()
        self.build_ambiguous()
        self.build_no_invoice()

        # Public ids are assigned in date order so the files read like real exports.
        self.invoices.sort(key=lambda i: (i.issue_date, i.key))
        for n, inv in enumerate(self.invoices, start=1):
            inv.invoice_id = f"INV-2025-{n:04d}"

        self.txns.sort(key=lambda t: (t.txn_date, t.key))
        for n, txn in enumerate(self.txns, start=1):
            txn.txn_id = f"TXN{n:05d}"

        # Memos that quote an invoice id can only be written once ids exist.
        by_key = {inv.key: inv for inv in self.invoices}
        origin_of = {
            t.key: t for t in self.txns if t.case_type == "DUPLICATE"
        }
        dup_origin = {}
        for artifact in (t for t in self.txns if t.case_type == "DUPLICATE_ARTIFACT"):
            match = next(
                t for t in origin_of.values()
                if t.counterparty_raw == artifact.counterparty_raw
                and t.amount == artifact.amount
            )
            dup_origin[artifact.key] = match
        for txn in self.txns:
            if txn.description:
                continue
            target = (by_key[txn.settles[0]] if txn.settles
                      else by_key[dup_origin[txn.key].settles[0]])
            txn.description = self._memo(target, txn.has_invoice_ref)

        self._verify()

    def _verify(self) -> None:
        """Assert the emitted data matches the documented case plan exactly."""
        expected_inv = {
            "CLEAN": 48, "NAME_NOISE": 12, "CONSOLIDATED": 5, "PARTIAL": 4,
            "FX_DIFF": 4, "FEE_DEDUCTION": 4, "DATE_DRIFT": 5, "DUPLICATE": 8,
            "UNPAID": 6, "AMBIGUOUS": 4,
        }
        expected_txn = {
            "CLEAN": 48, "NAME_NOISE": 12, "CONSOLIDATED": 2, "PARTIAL": 10,
            "FX_DIFF": 4, "FEE_DEDUCTION": 4, "DATE_DRIFT": 5, "DUPLICATE": 8,
            "DUPLICATE_ARTIFACT": 8, "AMBIGUOUS": 2, "NO_INVOICE": 17,
        }
        inv_counts = _tally(i.case_type for i in self.invoices)
        txn_counts = _tally(t.case_type for t in self.txns)
        assert inv_counts == _tally_like(expected_inv), f"invoice plan drift: {inv_counts}"
        assert txn_counts == _tally_like(expected_txn), f"txn plan drift: {txn_counts}"
        assert len(self.invoices) == 100, len(self.invoices)
        assert len(self.txns) == 120, len(self.txns)

        # Every partial group must sum back to its invoice exactly.
        for inv in self.invoices:
            if inv.case_type != "PARTIAL":
                continue
            paid = round(sum(t.amount for t in self.txns if inv.key in t.settles), 2)
            assert paid == inv.amount, f"{inv.invoice_id} instalments sum to {paid}"

        # Ids must be unique - a collision would silently corrupt scoring.
        assert len({i.invoice_id for i in self.invoices}) == 100
        assert len({t.txn_id for t in self.txns}) == 120

        # No NAME_NOISE row may be secretly clean, or the planted count is a lie.
        by_key = {inv.key: inv for inv in self.invoices}
        for txn in self.txns:
            if txn.case_type != "NAME_NOISE":
                continue
            clean = clean_bank_name(by_key[txn.settles[0]].customer)
            assert txn.counterparty_raw != clean, f"{txn.txn_id} name noise is a no-op"

        # The only same-(customer, amount) invoice pairs may be the planted
        # AMBIGUOUS ones - accidental ambiguity would be untracked ambiguity.
        seen: dict[tuple[str, float], Invoice] = {}
        for inv in self.invoices:
            prev = seen.get((inv.customer, inv.amount))
            if prev is not None:
                assert prev.case_type == inv.case_type == "AMBIGUOUS", (
                    f"unplanned ambiguity: {prev.key} / {inv.key}")
            seen[(inv.customer, inv.amount)] = inv

    # -- output ------------------------------------------------------------ #

    def ground_truth_rows(self) -> list[dict[str, Any]]:
        """Build ``ground_truth.csv`` rows: true pairs plus both unmatchable sides."""
        by_key = {inv.key: inv for inv in self.invoices}
        rows: list[dict[str, Any]] = []
        settled: set[str] = set()

        for txn in self.txns:
            if txn.settles:
                for inv_key in txn.settles:
                    settled.add(inv_key)
                    rows.append({
                        "txn_id": txn.txn_id,
                        "invoice_id": by_key[inv_key].invoice_id,
                        "relation": _relation(txn),
                        "case_type": txn.case_type,
                        "unmatchable": "no",
                        "ambiguous": "yes" if txn.ambiguous else "no",
                        "has_invoice_ref": "yes" if txn.has_invoice_ref else "no",
                        "note": txn.note,
                    })
            else:
                rows.append({
                    "txn_id": txn.txn_id,
                    "invoice_id": "",
                    "relation": "unmatchable_transaction",
                    "case_type": txn.case_type,
                    "unmatchable": "yes",
                    "ambiguous": "no",
                    "has_invoice_ref": "yes" if txn.has_invoice_ref else "no",
                    "note": txn.note,
                })

        for inv in self.invoices:
            if inv.key in settled:
                continue
            reason = ("never paid inside the statement window"
                      if inv.case_type == "UNPAID"
                      else "loser of an ambiguous same-amount pair; left open by FIFO")
            rows.append({
                "txn_id": "",
                "invoice_id": inv.invoice_id,
                "relation": "unmatchable_invoice",
                "case_type": inv.case_type,
                "unmatchable": "yes",
                "ambiguous": "yes" if inv.case_type == "AMBIGUOUS" else "no",
                "has_invoice_ref": "no",
                "note": reason,
            })
        return rows

    def write(self, out_dir: Path) -> dict[str, Any]:
        """Write all four data files and return a manifest of what was planted."""
        out_dir.mkdir(parents=True, exist_ok=True)
        _write_csv(
            out_dir / "bank_transactions.csv",
            ["txn_id", "date", "amount", "currency", "description", "counterparty_raw"],
            [t.row() for t in self.txns],
        )
        _write_csv(
            out_dir / "invoices.csv",
            ["invoice_id", "issue_date", "due_date", "amount", "currency",
             "customer_name", "status"],
            [i.row() for i in self.invoices],
        )
        gt = self.ground_truth_rows()
        _write_csv(
            out_dir / "ground_truth.csv",
            ["txn_id", "invoice_id", "relation", "case_type", "unmatchable",
             "ambiguous", "has_invoice_ref", "note"],
            gt,
        )
        (out_dir / "fx_rates.json").write_text(
            json.dumps(
                {"base": "USD", "as_of": "2025-04-01", "rates_to_usd": FX_RATES_TO_USD},
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )

        manifest = {
            "seed_invoices": len(self.invoices),
            "seed_transactions": len(self.txns),
            "ground_truth_rows": len(gt),
            "invoice_cases": _tally(i.case_type for i in self.invoices),
            "transaction_cases": _tally(t.case_type for t in self.txns),
            "true_pairs": sum(1 for r in gt if r["unmatchable"] == "no"),
            "unmatchable_transactions": sum(
                1 for r in gt if r["relation"] == "unmatchable_transaction"),
            "unmatchable_invoices": sum(
                1 for r in gt if r["relation"] == "unmatchable_invoice"),
            "transactions_with_invoice_ref": sum(1 for t in self.txns if t.has_invoice_ref),
            "ambiguous_rows": sum(1 for r in gt if r["ambiguous"] == "yes"),
        }
        (out_dir / "case_manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return manifest


# --------------------------------------------------------------------------- #
# Module helpers
# --------------------------------------------------------------------------- #

def _relation(txn: Txn) -> str:
    """Name the cardinality of a true settlement so the scorer can grade it fairly."""
    if txn.case_type == "CONSOLIDATED":
        return "one_txn_many_invoices"
    if txn.case_type == "PARTIAL":
        return "many_txns_one_invoice"
    return "one_to_one"


def _tally(values: Iterable[str]) -> dict[str, int]:
    """Count occurrences, ordered by descending count then alphabetically."""
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return _tally_like(out)


def _tally_like(counts: dict[str, int]) -> dict[str, int]:
    """Re-sort a count dict into the canonical comparison order."""
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    """Write rows to a UTF-8 CSV with stable column order and Unix line endings."""
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    """CLI entry point: generate the dataset and print the planted-case table."""
    parser = argparse.ArgumentParser(
        description="Generate the synthetic reconciliation dataset with ground truth.")
    parser.add_argument("--seed", type=int, default=20260903,
                        help="RNG seed; the default is the dataset shipped in the repo")
    parser.add_argument("--out", type=Path, default=Path("data"),
                        help="output directory for the CSVs")
    args = parser.parse_args()

    builder = DatasetBuilder(seed=args.seed)
    builder.build()
    manifest = builder.write(args.out)

    print(f"Wrote dataset to {args.out.resolve()}  (seed={args.seed})\n")
    print(f"{'CASE TYPE':<22}{'INVOICES':>10}{'TXNS':>8}")
    print("-" * 40)
    inv_c, txn_c = manifest["invoice_cases"], manifest["transaction_cases"]
    for case in sorted(set(inv_c) | set(txn_c)):
        print(f"{case:<22}{inv_c.get(case, 0):>10}{txn_c.get(case, 0):>8}")
    print("-" * 40)
    print(f"{'TOTAL':<22}{manifest['seed_invoices']:>10}{manifest['seed_transactions']:>8}\n")
    print(f"ground truth rows          : {manifest['ground_truth_rows']}")
    print(f"  true (txn,invoice) pairs : {manifest['true_pairs']}")
    print(f"  unmatchable transactions : {manifest['unmatchable_transactions']}")
    print(f"  unmatchable invoices     : {manifest['unmatchable_invoices']}")
    print(f"  rows flagged ambiguous   : {manifest['ambiguous_rows']}")
    print(f"  memos quoting invoice id : {manifest['transactions_with_invoice_ref']}")


if __name__ == "__main__":
    main()
