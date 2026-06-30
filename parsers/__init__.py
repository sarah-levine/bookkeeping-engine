"""
Parser package for reconcile_comprehensive.py.
Each module contains parsers for a specific bank/institution.
"""
from parsers.base import StatementParser, ClientRegistry, _registry
from parsers.report import (
    _safe_date_key, _report_header, _summary_block, _balance_check,
    _payments_section, _credits_section, _individual_section,
    _deposits_section, _checks_section, _adp_section,
    _cc_payments_section, _add_missing_row, _charges_section,
)
from parsers.chase import ChaseParser, ChaseInkParser, ChaseUnitedParser, ChaseSapphireParser
from parsers.amex import AmexStatementParser, AmexCheckingParser
from parsers.wells_fargo import WellsFargoCreditCardParser, WellsFargoCheckingParser
from parsers.bofa import BankOfAmericaCreditCardParser, BankOfAmericaCheckingParser, BankOfAmericaSavingsParser
from parsers.citi import CitiCheckingParser, CitiVisaCostcoParser, CitiSavingsParser
from parsers.bmo import BMOCheckingParser, BMOCreditCardParser
from parsers.usbank import USBankCheckingParser
from parsers.northern_trust import NorthernTrustCheckingParser
from parsers.capital_one import CapitalOneParser
