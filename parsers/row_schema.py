"""
row_schema.py
-------------
The standard transaction row shape for the Extract -> Classify+Aggregate ->
Report pipeline described in REFACTORING_ROADMAP.md's "Architecture Proposal"
section. This module defines the shape only — nothing in the codebase
constructs or consumes a TransactionRow yet. It's being introduced as its
own reviewable step ahead of parsers/northern_trust.py's migration to it
(see the roadmap's phased rollout plan), so the schema itself can be
evaluated independent of any parser change.

Sign convention: debits/fees are NEGATIVE, credits/payments-received are
POSITIVE. This matches Northern Trust's own existing convention (see
parsers/northern_trust.py's parse(), which already stores debits and CC
payments as negative Decimals) — chosen so its migration requires zero sign
inversion, isolating that phase's risk to shape translation, not sign logic.
This is the schema's canonical convention going forward, not an accident of
which parser migrates first: other parsers (e.g. Citi, which stores
checking-account debits as positive Decimals today) will need explicit sign
inversion in their own Extract step when their turn comes.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

RowType = Literal["credit", "debit", "check", "payment", "fee"]


@dataclass
class TransactionRow:
    date: str                # MM/DD/YY, matching report.py's _safe_date_key formats
    vendor: str               # post-normalize_vendor() display name
    raw_description: str      # pre-normalization text, kept for debugging/audit
    amount: Decimal            # debits/fees negative, credits/payments positive
    type: RowType
