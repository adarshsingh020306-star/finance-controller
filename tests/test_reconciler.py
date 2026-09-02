"""Tests for the reconciliation agent.

Run with ``python -m unittest discover -s tests`` (stdlib only - no extra dependency).

The Tier 3 tests deserve a note. They exercise the real :class:`LLMTier` code path
end to end, including validation and the hallucination guard, without spending a
token: the on-disk cache is pre-seeded with the exact response under test, so
``decide()`` takes its normal cache-hit branch. If it ever tried to reach the network
instead, the fake key would fail and the test would fail with it.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from reconciler import rules
from reconciler.engine import Reconciler
from reconciler.generate_data import DatasetBuilder
from reconciler.llm import LLMTier
from reconciler.models import Candidate, Invoice, Transaction, load_dataset
from reconciler.normalize import extract_invoice_refs, normalize_party, party_similarity
from reconciler.scoring import load_ground_truth, score

RATES = {"USD": 1.0, "EUR": 1.0850, "GBP": 1.2720}


def make_invoice(**kw) -> Invoice:
    """Build an invoice with sensible defaults for the field under test."""
    base = dict(
        invoice_id="INV-2025-0001",
        issue_date=date(2025, 3, 1),
        due_date=date(2025, 3, 31),
        amount=1000.00,
        currency="USD",
        customer_name="Acme Corporation",
        status="open",
    )
    return Invoice(**{**base, **kw})


def make_txn(**kw) -> Transaction:
    """Build a transaction with sensible defaults for the field under test."""
    base = dict(
        txn_id="TXN00001",
        date=date(2025, 4, 2),
        amount=1000.00,
        currency="USD",
        description="ACH CREDIT RECEIVED",
        counterparty_raw="ACME CORPORATION",
    )
    return Transaction(**{**base, **kw})


class TestNormalisation(unittest.TestCase):
    """Counterparty strings must survive every rail artefact the banks add."""

    def test_rail_prefix_and_masked_account_are_stripped(self) -> None:
        self.assertEqual(
            normalize_party("NEFT-ACME CORPORATION-XXXXX1234"), "ACME")

    def test_legal_suffixes_do_not_count_as_identity(self) -> None:
        self.assertEqual(normalize_party("ACME CORP PVT LTD"), "ACME")

    def test_trailing_reference_is_stripped(self) -> None:
        self.assertEqual(
            normalize_party("NORTHWIND TRADING /REF 88213/"), "NORTHWIND TRADING")

    def test_ampersand_becomes_a_word(self) -> None:
        self.assertEqual(
            normalize_party("Sable & Finch Partners"), "SABLE AND FINCH PARTNER")

    def test_known_mangles_score_high(self) -> None:
        for bank, ledger in [
            ("ACME CORP PVT LTD", "Acme Corporation"),
            ("EVERGLADEWATERTECH", "Everglade Water Tech"),
            ("CONSTRUCTIONS IRONBARK", "Ironbark Constructions"),
            ("QUANTUM LEDGER SYS", "Quantum Ledger Systems"),
            ("REDWOOD INSTRUMENT", "Redwood Instruments"),
            ("VERDANT AGRI", "Verdant Agriculture"),
        ]:
            with self.subTest(bank=bank):
                self.assertGreaterEqual(party_similarity(bank, ledger), 80.0)

    def test_different_companies_score_low(self) -> None:
        self.assertLess(party_similarity("ACME CORPORATION", "Zephyr Telecom"), 60.0)

    def test_short_fragment_does_not_score_as_a_full_match(self) -> None:
        """A single shared token must not look as good as a real match."""
        self.assertLess(
            party_similarity("CINDER", "Cinder Peak Outfitters Incorporated"), 95.0)

    def test_invoice_reference_extraction(self) -> None:
        self.assertEqual(
            extract_invoice_refs("SETTLEMENT INV-2025-0069 THANK YOU"),
            ["INV-2025-0069"])
        self.assertEqual(extract_invoice_refs("ACH CREDIT RECEIVED"), [])


class TestAmountRules(unittest.TestCase):
    """Only a wire fee or an FX conversion may explain an amount gap."""

    def test_exact_same_currency(self) -> None:
        v = rules.explain_amount(make_txn(), make_invoice(), RATES)
        self.assertTrue(v.explained)
        self.assertEqual(v.basis, "exact")

    def test_small_shortfall_is_a_wire_fee(self) -> None:
        v = rules.explain_amount(make_txn(amount=985.00), make_invoice(), RATES)
        self.assertTrue(v.explained)
        self.assertIn("wire fee", v.basis)

    def test_shortfall_over_materiality_is_not_a_fee(self) -> None:
        """30.00 off a 1,000 invoice is 3% - over policy, so a human decides."""
        v = rules.explain_amount(make_txn(amount=970.00), make_invoice(), RATES)
        self.assertFalse(v.explained)
        self.assertEqual(v.basis, "unexplained")

    def test_overpayment_is_never_explained(self) -> None:
        v = rules.explain_amount(make_txn(amount=1015.00), make_invoice(), RATES)
        self.assertFalse(v.explained)

    def test_fx_within_tolerance(self) -> None:
        inv = make_invoice(amount=1000.00, currency="EUR")
        v = rules.explain_amount(make_txn(amount=1090.00), inv, RATES)
        self.assertTrue(v.explained)
        self.assertIn("fx", v.basis)

    def test_fx_outside_tolerance(self) -> None:
        inv = make_invoice(amount=1000.00, currency="EUR")
        v = rules.explain_amount(make_txn(amount=1250.00), inv, RATES)
        self.assertFalse(v.explained)

    def test_debits_cannot_settle_receivables(self) -> None:
        self.assertFalse(rules.is_receipt(make_txn(amount=-500.0)))
        self.assertTrue(rules.is_receipt(make_txn(amount=500.0)))

    def test_date_score_decays_outside_the_window(self) -> None:
        self.assertEqual(rules.date_score(0), 1.0)
        self.assertEqual(rules.date_score(15), 1.0)
        self.assertLess(rules.date_score(60), 1.0)
        self.assertEqual(rules.date_score(200), 0.0)


class TestLLMTier(unittest.TestCase):
    """Tier 3 must never invent an invoice, and must cache what it is told."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.tier = LLMTier(cache_dir=self.tmp, model="test-model", api_key="fake-key")
        self.txn = make_txn()
        self.invoices = [
            make_invoice(invoice_id="INV-2025-0001"),
            make_invoice(invoice_id="INV-2025-0002", amount=1000.00),
        ]
        self.cands = [
            Candidate(invoice_id="INV-2025-0001", name_score=100.0,
                      amount_basis="exact", amount_delta=0.0, date_gap_days=2,
                      confidence=0.99),
            Candidate(invoice_id="INV-2025-0002", name_score=100.0,
                      amount_basis="exact", amount_delta=0.0, date_gap_days=2,
                      confidence=0.98),
        ]

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_cache(self, raw: str) -> None:
        """Pre-write the cache entry for this exact request, so no API call happens."""
        payload = self.tier._payload(self.txn, self.invoices, self.cands)
        key = self.tier._cache_key(payload)
        (self.tmp / f"{key}.json").write_text(
            json.dumps({"request": payload, "raw_response": raw}), encoding="utf-8")

    def test_valid_choice_is_accepted(self) -> None:
        self._seed_cache('{"invoice_id":"INV-2025-0001","confidence":0.9,'
                         '"reason":"memo and amount agree"}')
        d = self.tier.decide(self.txn, self.invoices, self.cands)
        self.assertIsNotNone(d)
        self.assertEqual(d.invoice_id, "INV-2025-0001")
        self.assertEqual(self.tier.stats.cache_hits, 1)
        self.assertEqual(self.tier.stats.api_calls, 0)

    def test_null_choice_is_accepted_as_a_refusal(self) -> None:
        self._seed_cache('{"invoice_id":null,"confidence":0.2,"reason":"both equal"}')
        d = self.tier.decide(self.txn, self.invoices, self.cands)
        self.assertIsNotNone(d)
        self.assertIsNone(d.invoice_id)

    def test_code_fence_is_tolerated(self) -> None:
        self._seed_cache('```json\n{"invoice_id":"INV-2025-0002","confidence":0.8,'
                         '"reason":"closer date"}\n```')
        d = self.tier.decide(self.txn, self.invoices, self.cands)
        self.assertEqual(d.invoice_id, "INV-2025-0002")

    def test_hallucinated_invoice_is_rejected(self) -> None:
        """An id that was never in the candidate set must become a refusal."""
        self._seed_cache('{"invoice_id":"INV-2025-9999","confidence":0.99,'
                         '"reason":"looks right"}')
        d = self.tier.decide(self.txn, self.invoices, self.cands)
        self.assertIsNone(d.invoice_id)
        self.assertEqual(d.confidence, 0.0)
        self.assertEqual(self.tier.stats.hallucinated_ids, 1)
        self.assertIn("INV-2025-9999", d.reason)

    def test_unparseable_output_is_counted_not_trusted(self) -> None:
        self._seed_cache("I think it is probably the first one.")
        self.assertIsNone(self.tier.decide(self.txn, self.invoices, self.cands))
        self.assertEqual(self.tier.stats.invalid_json, 1)

    def test_out_of_range_confidence_is_rejected(self) -> None:
        self._seed_cache('{"invoice_id":"INV-2025-0001","confidence":7,"reason":"x"}')
        self.assertIsNone(self.tier.decide(self.txn, self.invoices, self.cands))
        self.assertEqual(self.tier.stats.invalid_json, 1)

    def test_cache_key_is_stable_and_input_sensitive(self) -> None:
        a = self.tier._cache_key(self.tier._payload(self.txn, self.invoices, self.cands))
        b = self.tier._cache_key(self.tier._payload(self.txn, self.invoices, self.cands))
        c = self.tier._cache_key(
            self.tier._payload(make_txn(amount=999.0), self.invoices, self.cands))
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_missing_key_means_unavailable_not_silently_skipped(self) -> None:
        tier = LLMTier(cache_dir=self.tmp, api_key="")
        self.assertFalse(tier.available)
        self.assertIn("ANTHROPIC_API_KEY", tier.unavailable_reason())


class TestEndToEnd(unittest.TestCase):
    """A full generate -> reconcile -> score pass must hold its measured numbers."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp())
        builder = DatasetBuilder(seed=20260903)
        builder.build()
        builder.write(cls.tmp)
        ds = load_dataset(cls.tmp)
        llm = LLMTier(cache_dir=cls.tmp / "cache", enabled=False)
        cls.result = Reconciler(ds, llm).run()
        cls.card = score(cls.result, load_ground_truth(cls.tmp / "ground_truth.csv"))

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_processes_the_whole_batch(self) -> None:
        self.assertEqual(self.card.n_transactions, 120)
        self.assertEqual(self.card.n_invoices, 100)

    def test_no_wrong_matches_on_the_shipped_seed(self) -> None:
        """The number that matters most. A regression here must fail the suite."""
        self.assertEqual(self.card.wrong, 0, self.card.wrong_matches)

    def test_recall_does_not_regress(self) -> None:
        self.assertGreaterEqual(self.card.correct, 96)

    def test_every_unmatchable_transaction_is_left_alone(self) -> None:
        self.assertEqual(self.card.false_alarms, 0)

    def test_no_invoice_is_settled_twice(self) -> None:
        """Double-clearing a receivable is the failure duplicates are meant to cause."""
        claimed: list[str] = []
        for m in self.result.matches:
            claimed.extend(m.invoice_ids)
        partials = {
            inv for inv in claimed
            if sum(1 for m in self.result.matches if inv in m.invoice_ids) > 1
        }
        for inv in partials:
            owners = [m for m in self.result.matches if inv in m.invoice_ids]
            self.assertTrue(
                all(m.tier == "T2_STRUCTURAL" for m in owners),
                f"{inv} claimed by several transactions outside a partial group")

    def test_every_unresolved_record_has_a_specific_reason(self) -> None:
        for exc in self.result.exceptions:
            with self.subTest(record=exc.record_id):
                self.assertGreater(len(exc.reason), 40)
                self.assertNotIn("could not match", exc.reason.lower())
                # A reason must cite evidence: an id, an amount or a date.
                self.assertTrue(
                    any(ch.isdigit() for ch in exc.reason),
                    f"reason carries no concrete evidence: {exc.reason}")

    def test_ambiguous_cases_are_escalated_not_guessed(self) -> None:
        """The planted coin-flips must reach the exception list, not be posted."""
        self.assertEqual(self.card.ambiguous_recovered, 0)
        reasons = " ".join(e.reason for e in self.result.exceptions)
        self.assertIn("equally plausible", reasons)

    def test_duplicates_are_named_as_duplicates(self) -> None:
        reasons = " ".join(e.reason for e in self.result.exceptions)
        self.assertIn("suspected duplicate re-posting", reasons)


class TestDatasetIntegrity(unittest.TestCase):
    """The generator's own guarantees, checked independently of the report."""

    def test_planted_plan_is_enforced(self) -> None:
        builder = DatasetBuilder(seed=7)
        builder.build()  # _verify() raises if the case plan drifts
        self.assertEqual(len(builder.invoices), 100)
        self.assertEqual(len(builder.txns), 120)

    def test_status_column_never_leaks_payment_state(self) -> None:
        builder = DatasetBuilder(seed=7)
        builder.build()
        for inv in builder.invoices:
            self.assertIn(inv.row()["status"], {"open", "overdue"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
