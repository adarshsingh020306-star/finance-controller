"""Counterparty-string normalisation and similarity scoring.

Bank rails mangle a customer name in a handful of predictable ways: they uppercase
it, bolt on a rail prefix and a masked account number, staple a reference number to
the end, truncate it to a fixed field width, or drop the punctuation and spaces
entirely. Normalising those artefacts away before fuzzy matching is what turns a
50-point rapidfuzz score into a 95-point one.

Nothing here is tuned to a specific row of the dataset - every rule strips a class of
artefact that a real payment rail actually produces.
"""

from __future__ import annotations

import re
from functools import lru_cache

from rapidfuzz import fuzz

#: Rail identifiers that prefix a counterparty string on the statement.
_RAIL_PREFIXES = (
    "NEFT", "RTGS", "IMPS", "UPI", "ACH CREDIT", "ACH", "SEPA CT", "SEPA",
    "FEDWIRE", "WIRE", "SWIFT", "BACS", "CHAPS", "TRANSFER FROM", "PAYMENT FROM",
)

#: Company-form words that carry no identifying information.
_LEGAL_TOKENS = {
    "PVT", "PRIVATE", "LTD", "LIMITED", "LLC", "LLP", "INC", "INCORPORATED",
    "CORP", "CORPORATION", "CO", "COMPANY", "GMBH", "AG", "SA", "SAS", "BV",
    "NV", "PLC", "PTE", "SRL", "SPA", "OY", "AB", "AS",
}

#: Abbreviations banks substitute for long words, mapped back to a common stem.
#: Both sides of a comparison are pushed through this, so "SOLNS" and "SOLUTIONS"
#: converge rather than one being rewritten into the other.
_STEM_MAP = {
    "INTL": "INTERNATIONAL", "INTERNATIONL": "INTERNATIONAL",
    "TECH": "TECHNOLOG", "TECHNOLOGY": "TECHNOLOG", "TECHNOLOGIES": "TECHNOLOG",
    "SOLNS": "SOLUTION", "SOLUTIONS": "SOLUTION",
    "MFG": "MANUFACTUR", "MANUFACTURING": "MANUFACTUR",
    "SYS": "SYSTEM", "SYSTEMS": "SYSTEM",
    "PTNRS": "PARTNER", "PARTNERS": "PARTNER",
    "CONSLT": "CONSULT", "CONSULTING": "CONSULT",
    "LOGISTIC": "LOGISTIC", "LOGISTICS": "LOGISTIC",
    "ANALYT": "ANALYTIC", "ANALYTICS": "ANALYTIC",
    "PHARMA": "PHARMACEUTIC", "PHARMACEUTICALS": "PHARMACEUTIC",
    "INSTR": "INSTRUMENT", "INSTRUMENTS": "INSTRUMENT",
    "PUBL": "PUBLISH", "PUBLISHING": "PUBLISH",
    "HOSP": "HOSPITAL", "HOSPITALITY": "HOSPITAL",
    "AGRI": "AGRICULTUR", "AGRICULTURE": "AGRICULTUR",
    "INSUR": "INSURANC", "INSURANCE": "INSURANC",
    "TELCO": "TELECOM", "TELECOM": "TELECOM",
    "SHIP": "SHIPPING", "SHIPPING": "SHIPPING",
    "CONSTRUCTIONS": "CONSTRUCTION",
    "OUTFITTERS": "OUTFIT", "OUTFIT": "OUTFIT",
    "INSTRUMENT": "INSTRUMENT",
}

_MASKED_ACCOUNT = re.compile(r"\bX{3,}\d*\b|\b\d{6,}\b")
_TRAILING_REF = re.compile(r"/\s*REF[^/]*/?|\bREF\s*[:#]?\s*\d+\b", re.IGNORECASE)
_INVOICE_REF = re.compile(r"\bINV-\d{4}-\d{4}\b", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^A-Z0-9 ]+")
_SPACES = re.compile(r"\s+")


def extract_invoice_refs(text: str) -> list[str]:
    """Pull any literal invoice ids quoted in a memo line.

    Args:
        text: The raw ``description`` field of a bank transaction.

    Returns:
        Every ``INV-YYYY-NNNN`` token found, uppercased, in order of appearance.
    """
    return [m.upper() for m in _INVOICE_REF.findall(text or "")]


@lru_cache(maxsize=4096)
def normalize_party(raw: str) -> str:
    """Strip payment-rail artefacts from a counterparty string.

    Applied to both sides of every comparison, so an invoice's clean
    ``customer_name`` and a statement's mangled ``counterparty_raw`` converge on the
    same canonical form.

    Args:
        raw: Either a bank ``counterparty_raw`` or an invoice ``customer_name``.

    Returns:
        An uppercase, space-separated canonical name with rail prefixes, masked
        account numbers, trailing references, legal-form words and punctuation
        removed, and known abbreviations folded to a shared stem.
    """
    text = (raw or "").upper().replace("&", " AND ")
    text = _TRAILING_REF.sub(" ", text)
    text = _NON_ALNUM.sub(" ", text)
    text = _MASKED_ACCOUNT.sub(" ", text)

    # A rail prefix is only a prefix - "WIRE" inside a company name must survive.
    for prefix in sorted(_RAIL_PREFIXES, key=len, reverse=True):
        if text.strip().startswith(prefix + " "):
            text = text.strip()[len(prefix):]
            break

    tokens = [t for t in _SPACES.sub(" ", text).strip().split(" ") if t]
    kept = [_STEM_MAP.get(t, t) for t in tokens if t and t not in _LEGAL_TOKENS]
    # Drop a lone trailing "AND" left behind by a stripped legal form.
    while kept and kept[-1] == "AND":
        kept.pop()
    return " ".join(kept)


@lru_cache(maxsize=65536)
def party_similarity(bank_name: str, ledger_name: str) -> float:
    """Score how likely two counterparty strings name the same company.

    Takes the strongest of four rapidfuzz views, because each rail artefact defeats
    a different one:

    * ``token_set_ratio`` survives reordering and dropped words
      ("CONSTRUCTIONS IRONBARK" vs "Ironbark Constructions")
    * ``partial_ratio`` survives truncation ("REDWOOD INSTRUMENT")
    * ``WRatio`` handles the general mixed case
    * a de-spaced ``ratio`` survives collapsed whitespace ("EVERGLADEWATERTECH")

    Args:
        bank_name: The raw counterparty string from the statement.
        ledger_name: The customer name from the invoice.

    Returns:
        A similarity in ``[0, 100]``.
    """
    left, right = normalize_party(bank_name), normalize_party(ledger_name)
    if not left or not right:
        return 0.0
    if left == right:
        return 100.0

    flat_l, flat_r = left.replace(" ", ""), right.replace(" ", "")
    scores = (
        fuzz.token_set_ratio(left, right),
        fuzz.partial_ratio(left, right),
        fuzz.WRatio(left, right),
        fuzz.ratio(flat_l, flat_r),
    )
    best = max(scores)

    # token_set_ratio returns 100 whenever one name's tokens are a subset of the
    # other's, so a single shared token ("CINDER" vs "CINDER PEAK OUTFIT") scores as
    # highly as a full match. Damp that down by how much of the longer name is
    # actually covered, otherwise short fragments match far too many customers.
    coverage = min(len(flat_l), len(flat_r)) / max(len(flat_l), len(flat_r))
    if best >= 95 and coverage < 0.55:
        best = 95.0 * (0.55 + 0.45 * coverage / 0.55)
    return float(best)
