"""Tier 3: ask a model only about the pairings the deterministic tiers gave up on.

Three properties matter more than raw accuracy here:

1. **The model cannot invent an invoice.** Its answer is validated against the exact
   candidate set it was shown, and an id outside that set is discarded as a refusal,
   not silently trusted.
2. **Runs are cheap and repeatable.** Every call is cached on disk under a hash of
   the exact request payload, so a second run of the report costs nothing and
   produces byte-identical decisions.
3. **Absence degrades honestly.** With no API key the tier reports itself as
   unavailable and every leftover becomes an exception saying so, rather than the
   pipeline quietly claiming deterministic-only numbers as its full result.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import Candidate, LLMDecision, Invoice, Transaction

#: Bumping this invalidates every cached decision. Change it whenever the prompt,
#: the candidate payload or the parsing rules change.
PROMPT_VERSION = "v1"

DEFAULT_MODEL = "claude-sonnet-5"

_SYSTEM_PROMPT = """\
You are a finance operations controller reconciling a bank statement against open \
sales invoices. You are only shown transactions that deterministic rules could not \
resolve, so "no match" is a common and fully acceptable answer.

Rules you must follow:
- Choose an invoice ONLY from the candidate list you are given. Never output an \
invoice_id that is not in that list.
- If no candidate is convincing, return null. A wrong match is far more costly than \
an unresolved one, because it silently closes a real receivable.
- If two or more candidates are equally plausible, return null and say so in the \
reason.
- The amount is the strongest signal. A gap is only acceptable if it is explained by \
a small wire fee (the credit is short by a token amount) or by an FX conversion at \
roughly the stated rate. An unexplained gap of any size means no match.
- Reply with a single JSON object and nothing else. No prose, no code fences.

JSON schema:
{"invoice_id": <string from the candidate list, or null>,
 "confidence": <number between 0 and 1>,
 "reason": "<one line, under 200 characters, citing the specific evidence>"}
"""


@dataclass
class LLMStats:
    """Counters describing what Tier 3 actually did during a run."""

    calls_attempted: int = 0
    cache_hits: int = 0
    api_calls: int = 0
    invalid_json: int = 0
    hallucinated_ids: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Return the counters as a plain dict for the report."""
        return {
            "calls_attempted": self.calls_attempted,
            "cache_hits": self.cache_hits,
            "api_calls": self.api_calls,
            "invalid_json": self.invalid_json,
            "hallucinated_ids": self.hallucinated_ids,
            "errors": self.errors[:5],
        }


class LLMTier:
    """Cached, schema-validated access to the Tier 3 model."""

    def __init__(
        self,
        cache_dir: Path,
        model: str | None = None,
        api_key: str | None = None,
        enabled: bool = True,
    ) -> None:
        """Prepare the tier, resolving the model and key from the environment.

        Args:
            cache_dir: Directory for the on-disk decision cache.
            model: Model id; defaults to ``$RECON_MODEL`` then :data:`DEFAULT_MODEL`.
            api_key: API key; defaults to ``$ANTHROPIC_API_KEY``.
            enabled: Set ``False`` to force the tier off for a deterministic-only run.
        """
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.model = model or os.environ.get("RECON_MODEL") or DEFAULT_MODEL
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY") or ""
        self.enabled = enabled
        self.stats = LLMStats()
        self._client: Any = None

    @property
    def available(self) -> bool:
        """Whether Tier 3 can actually run, i.e. it is enabled and has a key."""
        return self.enabled and bool(self.api_key)

    def unavailable_reason(self) -> str:
        """Explain in one line why Tier 3 is not running, for the exception list."""
        if not self.enabled:
            return "Tier 3 disabled for this run (--no-llm)"
        return "Tier 3 unavailable: ANTHROPIC_API_KEY is not set"

    # -- prompt / cache ---------------------------------------------------- #

    def _payload(
        self, txn: Transaction, invoices: list[Invoice], cands: list[Candidate]
    ) -> dict[str, Any]:
        """Build the exact, order-stable request payload that is also the cache key."""
        by_id = {inv.invoice_id: inv for inv in invoices}
        return {
            "prompt_version": PROMPT_VERSION,
            "model": self.model,
            "transaction": {
                "txn_id": txn.txn_id,
                "date": txn.date.isoformat(),
                "amount": round(txn.amount, 2),
                "currency": txn.currency,
                "description": txn.description,
                "counterparty_raw": txn.counterparty_raw,
            },
            "candidates": [
                {
                    "invoice_id": c.invoice_id,
                    "customer_name": by_id[c.invoice_id].customer_name,
                    "amount": round(by_id[c.invoice_id].amount, 2),
                    "currency": by_id[c.invoice_id].currency,
                    "issue_date": by_id[c.invoice_id].issue_date.isoformat(),
                    "due_date": by_id[c.invoice_id].due_date.isoformat(),
                    "counterparty_similarity": round(c.name_score, 1),
                    "amount_gap_vs_transaction": round(c.amount_delta, 2),
                    "amount_gap_explanation": c.amount_basis,
                    "days_from_due_date": c.date_gap_days,
                }
                for c in cands
            ],
        }

    @staticmethod
    def _cache_key(payload: dict[str, Any]) -> str:
        """Hash a request payload into a stable cache filename."""
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _read_cache(self, key: str) -> dict[str, Any] | None:
        path = self.cache_dir / f"{key}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def _write_cache(self, key: str, payload: dict[str, Any], raw: str) -> None:
        (self.cache_dir / f"{key}.json").write_text(
            json.dumps({"request": payload, "raw_response": raw}, indent=2),
            encoding="utf-8",
        )

    # -- the call ---------------------------------------------------------- #

    def _call_api(self, payload: dict[str, Any]) -> str:
        """Send one request to the Messages API and return the raw text reply."""
        if self._client is None:
            from anthropic import Anthropic  # imported lazily so no key => no import

            self._client = Anthropic(api_key=self.api_key)
        user_msg = (
            "Reconcile this bank transaction against the candidate invoices.\n\n"
            + json.dumps(
                {"transaction": payload["transaction"], "candidates": payload["candidates"]},
                indent=2,
            )
        )
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=300,
            temperature=0,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

    @staticmethod
    def _parse(raw: str) -> LLMDecision | None:
        """Parse a raw model reply into a validated decision, or ``None``.

        Tolerates a code fence or a stray sentence around the object, because that is
        a formatting slip rather than a reasoning failure - but the object itself must
        satisfy the schema exactly.
        """
        text = (raw or "").strip()
        if "```" in text:
            parts = text.split("```")
            text = max(parts, key=len).removeprefix("json").strip()
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            return LLMDecision.model_validate_json(text[start : end + 1])
        except (ValidationError, ValueError):
            return None

    def decide(
        self, txn: Transaction, invoices: list[Invoice], cands: list[Candidate]
    ) -> LLMDecision | None:
        """Ask the model to pick one of ``cands`` for ``txn``, or to decline.

        The returned decision is guaranteed to name an invoice from ``cands`` or
        ``None``; a hallucinated id is counted and converted into a refusal.

        Args:
            txn: The unresolved bank transaction.
            invoices: Full invoice list, used to expand candidate details.
            cands: The shortlist the deterministic tiers produced, best first.

        Returns:
            A validated :class:`LLMDecision`, or ``None`` if the tier is unavailable,
            the reply was unparseable, or the reply named an invoice not on offer.
        """
        if not self.available or not cands:
            return None

        self.stats.calls_attempted += 1
        payload = self._payload(txn, invoices, cands)
        key = self._cache_key(payload)

        cached = self._read_cache(key)
        if cached is not None:
            self.stats.cache_hits += 1
            raw = cached.get("raw_response", "")
        else:
            try:
                raw = self._call_api(payload)
                self.stats.api_calls += 1
                self._write_cache(key, payload, raw)
            except Exception as exc:  # noqa: BLE001 - one bad call must not kill a batch
                self.stats.errors.append(f"{txn.txn_id}: {type(exc).__name__}: {exc}")
                return None

        decision = self._parse(raw)
        if decision is None:
            self.stats.invalid_json += 1
            return None

        allowed = {c.invoice_id for c in cands}
        if decision.invoice_id is not None and decision.invoice_id not in allowed:
            # The model named an invoice it was never shown. Refuse it outright.
            self.stats.hallucinated_ids += 1
            return LLMDecision(
                invoice_id=None,
                confidence=0.0,
                reason=(f"model proposed {decision.invoice_id}, which was not among "
                        f"the {len(allowed)} candidates supplied; rejected"),
            )
        return decision
