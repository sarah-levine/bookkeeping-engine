"""
registry.py
-----------
Single source of truth for "what statement types exist."

Before this module existed, adding a new statement type meant hand-editing
four separate places — reconcile_comprehensive.py's STATEMENT_TYPE_LABELS
dict, its parser_map dict, manual_statement_entry.py's own separate
PARSER_BY_TYPE dict, and clients/_schema.json's statement_types enum — plus
three scattered hardcoded tuples of "which type codes are credit cards".
Those copies had already drifted (manual_statement_entry.py's dict only had
3 of ~24 types; one credit-card tuple had a 'citi_costco' typo for the real
key 'citi_visa_costco').

Each parser module now registers its own classes at the bottom of its file
(see the register() calls in parsers/bofa.py, parsers/amex.py, etc.), and
everything else reads from this registry instead of a hand-maintained copy.

Deliberately NOT included: detect_statement_type()'s actual text-sniffing
logic. That ~230-line if/elif chain in reconcile_comprehensive.py is order-
dependent (several branches must run before others to avoid false positives)
and is left untouched — a new format still needs one new branch added there
by hand. This registry only removes the other, purely mechanical, copies.
"""

from dataclasses import dataclass, field


@dataclass
class StatementFormat:
    key: str
    label: str
    parser_cls: type
    is_credit_card: bool = False
    manual_entry_supported: bool = field(init=False)

    def __post_init__(self):
        self.manual_entry_supported = hasattr(self.parser_cls, "load_from_dict")


_REGISTRY: dict[str, StatementFormat] = {}


def register(key: str, label: str, parser_cls: type, *, is_credit_card: bool = False) -> None:
    """Register a statement type. Called once at import time by each parser
    module (e.g. at the bottom of parsers/bofa.py)."""
    _REGISTRY[key] = StatementFormat(key, label, parser_cls, is_credit_card)


def get(key: str) -> StatementFormat | None:
    return _REGISTRY.get(key)


def all_formats() -> list[StatementFormat]:
    return list(_REGISTRY.values())


def keys() -> list[str]:
    return list(_REGISTRY.keys())


def labels() -> dict[str, str]:
    return {k: f.label for k, f in _REGISTRY.items()}


def parser_by_type() -> dict[str, type]:
    return {k: f.parser_cls for k, f in _REGISTRY.items()}


def credit_card_keys() -> list[str]:
    return [k for k, f in _REGISTRY.items() if f.is_credit_card]


def manual_entry_parser_map() -> dict[str, type]:
    return {k: f.parser_cls for k, f in _REGISTRY.items() if f.manual_entry_supported}
