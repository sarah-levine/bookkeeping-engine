"""
classify.py
-----------
Shared Classify stage for the Extract/Classify/Report pipeline (see
REFACTORING_ROADMAP.md's "Architecture Proposal"). Consumes the standard
TransactionRow shape (parsers/row_schema.py) produced by a parser's Extract
stage.

classify_checking_rows() is scoped narrowly to what NorthernTrustCheckingParser
currently does — nothing more — since it's this module's only caller so far:
split rows into credit/CC-payment/debit buckets using the client-agnostic
card-network fallback (_is_known_cc_network_payment, parsers/base.py) plus a
client's own cc_keywords/cc_payment_vendors config, and apply a client-
config-driven Square line-position vendor remap. The name is deliberately
specific (not just "classify") — a checking-account statement's bucket set
(credits/debits/CC payments) differs from a credit-card statement's
(payments/credits/charges, see parsers/base.py's _classify_cc_transaction),
and generalizing across both shapes with only one real caller would mean
guessing at a shared interface from a sample size of one.

Deliberately NOT included: aggregation (_aggregate_by_vendor() in
parsers/base.py). Northern Trust doesn't aggregate transactions today — no
real client config it uses sets an aggregation-relevant knob
(no_aggregate_vendors/never_aggregate_vendors), so there's no real fixture to
verify aggregation behavior against here. REFACTORING_ROADMAP.md recommends
proving that stage on the next parser migrated (Citi Savings, which does
aggregate) before generalizing it into this module.
"""

from typing import Iterable

from parsers.base import _is_known_cc_network_payment
from parsers.row_schema import TransactionRow


def classify_checking_rows(rows: Iterable[TransactionRow], config: dict) -> dict:
    """Classify a checking account's TransactionRows into credit/debit/
    credit_card_payment buckets, applying a config-driven Square line-
    position vendor remap along the way.

    Returns {'credits': [...], 'debits': [...], 'credit_card_payments': [...]}
    — each a list of {'date', 'vendor', 'amount', 'memo'} dicts, matching
    the shape these attributes have always been stored in.
    """
    square_order = {entry['position']: entry for entry in config.get('square_line_order', [])}
    square_counter = 0  # tracks which Square transaction we're on
    cc_kws = config.get('cc_keywords', []) or config.get('cc_payment_vendors', [])

    credits, debits, credit_card_payments = [], [], []

    for row in rows:
        vendor = row.vendor
        # Apply position-based Square QB account mapping
        memo = ''
        if 'Square' in vendor and square_order:
            square_counter += 1
            mapping = square_order.get(square_counter)
            if mapping:
                vendor = mapping['account']
                memo = mapping.get('memo', '')
        entry = {'date': row.date, 'vendor': vendor, 'amount': row.amount, 'memo': memo}
        if row.type == 'credit':
            credits.append(entry)
        elif (_is_known_cc_network_payment(vendor.upper())
              or any(kw.upper() in vendor.upper() for kw in cc_kws)):
            credit_card_payments.append(entry)
        else:
            debits.append(entry)

    return {'credits': credits, 'debits': debits, 'credit_card_payments': credit_card_payments}
