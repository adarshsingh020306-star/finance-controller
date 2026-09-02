"""Typed records and CSV loading for the reconciliation pipeline.

Everything downstream of :func:`load_dataset` works on validated pydantic objects
rather than dataframes, so a malformed row fails at the boundary instead of turning
into a silent ``NaN`` three tiers later.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Which stage of the pipeline produced a decision. Reported separately so the
#: value the LLM tier adds over the deterministic tiers is visible, not averaged in.
Tier = Literal["T1_EXACT", "T2_FUZZY", "T2_STRUCTURAL", "T3_LLM"]


def _as_date(value: object) -> date:
    """Coerce a CSV cell to a ``date``, accepting date, datetime or ISO string."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()


class Transaction(BaseModel):
    """One line of the bank statement."""

    model_config = ConfigDict(frozen=True)

    txn_id: str
    date: date
    amount: float
    currency: str
    description: str
    counterparty_raw: str

    @field_validator("date", mode="before")
    @classmethod
    def _parse_date(cls, v: object) -> date:
        return _as_date(v)

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class Invoice(BaseModel):
    """One open receivable from the AR ledger."""

    model_config = ConfigDict(frozen=True)

    invoice_id: str
    issue_date: date
    due_date: date
    amount: float
    currency: str
    customer_name: str
    status: str

    @field_validator("issue_date", "due_date", mode="before")
    @classmethod
    def _parse_date(cls, v: object) -> date:
        return _as_date(v)

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class Candidate(BaseModel):
    """A scored (transaction, invoice) pairing considered by one of the tiers."""

    invoice_id: str
    name_score: float = Field(ge=0, le=100)
    amount_basis: str
    amount_delta: float
    date_gap_days: int
    confidence: float = Field(ge=0, le=1)

    def describe(self) -> str:
        """One-line human summary used inside exception reasons."""
        return (
            f"{self.invoice_id} (name {self.name_score:.0f}, "
            f"{self.amount_basis}, delta {self.amount_delta:+.2f}, "
            f"{self.date_gap_days:+d}d, conf {self.confidence:.2f})"
        )


class Match(BaseModel):
    """A settlement the agent is willing to post.

    ``invoice_ids`` holds more than one id only for a consolidated payment. Several
    matches may share an invoice id only when they are instalments of one partial
    settlement.
    """

    txn_id: str
    invoice_ids: list[str]
    tier: Tier
    confidence: float = Field(ge=0, le=1)
    rationale: str


class ReconException(BaseModel):
    """A record the agent refused to resolve, with a specific reason why."""

    record_id: str
    record_type: Literal["transaction", "invoice"]
    reason: str
    tier_reached: str
    best_candidate: str | None = None


class LLMDecision(BaseModel):
    """Strict schema the Tier 3 model must return. Anything else is rejected."""

    model_config = ConfigDict(extra="forbid")

    invoice_id: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=400)


class Dataset(BaseModel):
    """The three inputs the agent reconciles, plus the published FX table."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    transactions: list[Transaction]
    invoices: list[Invoice]
    fx_rates_to_usd: dict[str, float]

    def invoice_index(self) -> dict[str, Invoice]:
        """Return invoices keyed by id for O(1) lookup during scoring."""
        return {inv.invoice_id: inv for inv in self.invoices}

    def transaction_index(self) -> dict[str, Transaction]:
        """Return transactions keyed by id for O(1) lookup during scoring."""
        return {t.txn_id: t for t in self.transactions}


def load_dataset(data_dir: Path) -> Dataset:
    """Read the bank statement, the AR ledger and the FX table off disk.

    Args:
        data_dir: Directory holding ``bank_transactions.csv``, ``invoices.csv`` and
            ``fx_rates.json`` as written by ``generate_data.py``.

    Returns:
        A validated :class:`Dataset`. Transactions come back in date order, which the
        engine relies on so that the earlier of two identical postings claims the
        invoice first.

    Raises:
        FileNotFoundError: If any required input file is missing.
    """
    txn_path = data_dir / "bank_transactions.csv"
    inv_path = data_dir / "invoices.csv"
    fx_path = data_dir / "fx_rates.json"
    for path in (txn_path, inv_path, fx_path):
        if not path.exists():
            raise FileNotFoundError(
                f"{path} not found - run `python -m reconciler.generate_data` first")

    txn_df = pd.read_csv(txn_path, dtype=str, keep_default_na=False)
    inv_df = pd.read_csv(inv_path, dtype=str, keep_default_na=False)

    transactions = [
        Transaction(
            txn_id=r["txn_id"],
            date=r["date"],
            amount=float(r["amount"]),
            currency=r["currency"],
            description=r["description"],
            counterparty_raw=r["counterparty_raw"],
        )
        for r in txn_df.to_dict("records")
    ]
    invoices = [
        Invoice(
            invoice_id=r["invoice_id"],
            issue_date=r["issue_date"],
            due_date=r["due_date"],
            amount=float(r["amount"]),
            currency=r["currency"],
            customer_name=r["customer_name"],
            status=r["status"],
        )
        for r in inv_df.to_dict("records")
    ]

    rates = json.loads(fx_path.read_text(encoding="utf-8"))["rates_to_usd"]

    transactions.sort(key=lambda t: (t.date, t.txn_id))
    invoices.sort(key=lambda i: (i.issue_date, i.invoice_id))
    return Dataset(transactions=transactions, invoices=invoices, fx_rates_to_usd=rates)
