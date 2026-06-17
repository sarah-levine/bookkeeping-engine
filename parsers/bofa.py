import sys
import re
import os
import json
from pathlib import Path
from decimal import Decimal
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

def _now_pst():
    """Return current datetime in US/Pacific (PST/PDT)."""
    return datetime.now(ZoneInfo('America/Los_Angeles'))

try:
    import fitz
    import pytesseract
    from PIL import Image
    import io as _io
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

from parsers.base import StatementParser, _registry, KNOWN_CLIENTS, _classify_cc_transaction
from parsers.report import *
from parsers.report import (
    _safe_date_key, _report_header, _summary_block, _balance_check,
    _payments_section, _credits_section, _individual_section,
    _deposits_section, _checks_section, _adp_section,
    _cc_payments_section, _add_missing_row, _charges_section
)

class BankOfAmericaCreditCardParser(StatementParser):
    """
    Bank of America Business Credit Card.
    """
    statement_type = "Bank of America Business Credit Card"

    def __init__(self, pdf_path, client_name=None):
        super().__init__(pdf_path, client_name)
        self.previous_balance = None
        self.new_balance = None
        self.closing_date = None
        self.payments = []
        self.credits = []
        self.charges = []
        self.finance_charge = None
        self.total_payments = Decimal('0')

    def parse(self):
        lines = self.text.split('\n')

        # Extract statement closing year from period line e.g. "December 07, 2025 - January 06, 2026"
        self.statement_year = 2026  # default
        for line in lines:
            m = re.search(r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d+,\s+(\d{4})\s*-\s*(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d+,\s+(\d{4})', line)
            if m:
                self.statement_year = int(m.group(3))
                break

        # Extract closing date from "Statement Closing Date ........ MM/DD/YY"
        for line in lines:
            m = re.search(r'Statement Closing Date[\s.]+(\d{2}/\d{2}/\d{2,4})', line, re.IGNORECASE)
            if m:
                self.closing_date = m.group(1)
                break

        for line in lines:
            if 'Previous Balance' in line and self.previous_balance is None:
                # Search for amount after "Previous Balance" keyword specifically.
                # Must capture optional leading minus sign so credit balances (e.g. -$91.13) are negative.
                m = re.search(r'Previous Balance\s*[.$]*\s*(-?)\$?([\d,]+\.\d{2})', line)
                if not m:
                    m = re.search(r'Previous Balance.*?(-?)\$([\d,]+\.\d{2})', line)
                if m:
                    sign = -1 if m.group(1) == '-' else 1
                    self.previous_balance = sign * Decimal(m.group(2).replace(',', ''))
            if 'New Balance Total' in line and self.new_balance is None:
                # Search for amount after "New Balance Total" keyword specifically.
                # Capture optional leading minus sign for credit balances.
                m = re.search(r'New Balance Total\s*[.$]*\s*(-?)\$?([\d,]+\.\d{2})', line)
                if not m:
                    m = re.search(r'New Balance Total.*?(-?)\$([\d,]+\.\d{2})', line)
                if m:
                    sign = -1 if m.group(1) == '-' else 1
                    self.new_balance = sign * Decimal(m.group(2).replace(',', ''))

        cfg = _registry.get_config(self.client_name) or {}
        # Some BofA statements present a separate credits section keyed by a
        # specific account ending (config: bofa_credits_account); generic
        # statements have no such section.
        credits_acct = str(cfg.get('bofa_credits_account') or '')
        in_payments = False
        in_charges = False
        in_credits_section = False

        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue

            if 'Payments and Other Credits' in stripped and 'PERIOD' not in stripped:
                if credits_acct and credits_acct in ''.join(lines[max(0, i - 2):i + 1]):
                    in_credits_section = True
                    in_payments = False
                    in_charges = False
                else:
                    in_payments = True
                    in_credits_section = False
                    in_charges = False
                continue

            if 'Purchases and Other Charges' in stripped and 'PERIOD' not in stripped:
                in_charges = True
                in_payments = False
                in_credits_section = False
                continue

            if 'TOTAL PAYMENTS' in stripped or 'TOTAL PURCHASES' in stripped or 'TOTAL FINANCE' in stripped or 'Finance Charge Calculation' in stripped:
                in_payments = False
                in_charges = False
                in_credits_section = False
                continue

            if in_payments or in_charges or in_credits_section:
                m = re.match(
                    r'\s*(\d{2}/\d{2})\s+(\d{2}/\d{2})\s+(.+?)\s{2,}(\d{20,})\s+([-]?\s*[\d,]+\.\d{2})\s*$',
                    line)
                if m:
                    post_date = m.group(1)
                    description = m.group(3).strip()
                    amount_str = m.group(5).replace(',', '').replace(' ', '')
                    # Derive year from statement period: Dec dates -> statement year - 1, Jan dates -> statement year
                    month = int(post_date.split('/')[0])
                    if hasattr(self, 'statement_year'):
                        yr = (self.statement_year - 1) if month >= 11 else self.statement_year
                    else:
                        yr = 2026  # fallback
                    year_2d = str(yr)[-2:]
                    full_date = f"{post_date}/{year_2d}"
                    try:
                        amount = Decimal(amount_str)
                        txn_type = _classify_cc_transaction(description, amount)
                        if in_payments or in_credits_section:
                            if txn_type == 'credit':
                                self.credits.append({'date': full_date,
                                                     'description': description,
                                                     'amount': abs(amount)})
                            else:
                                self.payments.append({'date': full_date,
                                                      'description': 'PAYMENT - THANK YOU',
                                                      'amount': abs(amount)})
                        elif 'FINANCE CHARGE' in description.upper():
                            pass  # captured separately as self.finance_charge
                        else:
                            self.charges.append({'date': full_date,
                                                  'vendor': description,
                                                  'amount': amount})
                    except Exception as _txn_err:
                        print(f"  ⚠ CC parser skipped line (parse error): {_txn_err!r} — {line[:80]!r}")
        for line in lines:
            if 'PURCHASE *FINANCE CHARGE*' in line:
                m = re.search(r'([\d,]+\.\d{2})$', line)
                if m:
                    self.finance_charge = Decimal(m.group(1).replace(',', ''))
            if 'LATE PAYMENT FEE' in line or 'RETURNED PAYMENT FEE' in line or 'ANNUAL FEE' in line:
                m = re.search(r'([\d,]+\.\d{2})$', line)
                if m:
                    self.fees += Decimal(m.group(1).replace(',', ''))

        # Set total_payments now so _tied_out() works correctly before generate_report()
        self.total_payments = sum(Decimal(str(p['amount'])) for p in self.payments)

    def generate_report(self):
        aggregated = self._aggregate_by_vendor(self.charges, date_fmt='%m/%d/%y')
        total_charges = sum(r['amount'] for r in aggregated)
        total_payments = sum(p["amount"] for p in self.payments)
        self.total_payments = total_payments
        # Normalize and aggregate credits the same way charges are handled.
        credits_for_agg = [{'date': c['date'], 'vendor': c['description'],
                            'amount': c['amount']} for c in self.credits]
        aggregated_credits = self._aggregate_by_vendor(credits_for_agg, date_fmt='%m/%d/%y')
        total_credits = sum(c['amount'] for c in aggregated_credits)
        statement_charges = None
        if self.new_balance is not None and self.previous_balance is not None:
            statement_charges = (self.new_balance - self.previous_balance
                                 + total_payments + total_credits
                                 - (self.finance_charge or Decimal('0')))
        aggregated, total_charges = _add_missing_row(aggregated, total_charges, statement_charges)

        report = _report_header(self.statement_type, self.client_name,
                                     statement_date=self.closing_date)
        summary_rows = [
            ('Previous Balance',  self.previous_balance),
            ('Payments',          total_payments),
            ('Credits / Returns', total_credits if total_credits else None),
            ('Purchases',         total_charges),
            ('Finance Charges',   self.finance_charge if self.finance_charge else None),
            ('New Balance',       self.new_balance),
        ]
        report += _summary_block(summary_rows)

        if self.previous_balance is not None and self.new_balance is not None:
            calc = (self.previous_balance - total_payments - total_credits
                    + total_charges + (self.finance_charge or Decimal('0')))
            ok = abs(calc - self.new_balance) < Decimal('0.01')
            report += _balance_check(ok, calc)

        if self.payments:
            report += _payments_section(self.payments, total_payments)
        if aggregated_credits:
            report += _deposits_section(aggregated_credits, total_credits,
                                        title='CREDITS / RETURNS')
        report += _charges_section(aggregated, total_charges)
        return report

    def _extract_period(self):
        m = re.search(r'(\w+ \d+, \d{4})\s*-\s*(\w+ \d+, \d{4})', self.text)
        if m:
            return f"{m.group(1)} - {m.group(2)}"
        return "Unknown Period"


# ═══════════════════════════════════════════════════════════════════════════════
# CHECKING ACCOUNT PARSERS (BofA)
# ═══════════════════════════════════════════════════════════════════════════════


class BankOfAmericaCheckingParser(StatementParser):
    """
    Bank of America Business Checking.
    Includes OCR-based check payee extraction when PyMuPDF + pytesseract are available.
    """
    statement_type = "Bank of America Business Checking"

    def __init__(self, pdf_path, client_name=None):
        super().__init__(pdf_path, client_name)
        self.beginning_balance = None
        self.ending_balance = None
        self.credits = []
        self.debits = []
        self.checks = []
        self.service_fees = Decimal('0')

    def _detect_client(self):
        lines = self.text.split('\n')
        for i, line in enumerate(lines[:30]):
            if re.search(r'^[A-Z\s,\.]+(?:INC|LLC|CORP)[\s\.]*$', line.strip()) and len(line.strip()) > 10:
                potential = line.strip().rstrip('.,')
                if 'BANK OF AMERICA' not in potential and 'PREFERRED REWARDS' not in potential:
                    if potential in KNOWN_CLIENTS:
                        return potential
        return super()._detect_client()

    def parse(self):
        lines = self.text.split('\n')

        for line in lines:
            if 'Beginning balance on' in line and self.beginning_balance is None:
                m = re.search(r'\$?([\d,]+\.\d{2})', line)
                if m:
                    self.beginning_balance = Decimal(m.group(1).replace(',', ''))
            if 'Ending balance on' in line and self.ending_balance is None:
                m = re.search(r'\$?([\d,]+\.\d{2})', line)
                if m:
                    self.ending_balance = Decimal(m.group(1).replace(',', ''))

        in_deposits = False
        in_withdrawals = False
        in_checks = False
        current_date = current_desc = current_amount = None

        for i, line in enumerate(lines):
            if not line.strip():
                continue

            if line.strip().startswith('Deposits and other credits') and 'Total' not in line and not re.search(r'\$?[\d,]+\.\d{2}\s*$', line):
                if current_date and current_desc and current_amount is not None:
                    self._save_transaction(current_date, current_desc, current_amount,
                                           in_deposits, in_withdrawals)
                current_date = current_desc = current_amount = None
                in_deposits, in_withdrawals, in_checks = True, False, False
                continue

            if line.strip().startswith('Withdrawals and other debits') and 'Total' not in line:
                if current_date and current_desc and current_amount is not None:
                    self._save_transaction(current_date, current_desc, current_amount,
                                           in_deposits, in_withdrawals)
                current_date = current_desc = current_amount = None
                in_deposits, in_withdrawals, in_checks = False, True, False
                continue

            if line.strip() == 'Checks' or (line.strip().startswith('Date') and 'Check #' in line):
                if current_date and current_desc and current_amount is not None:
                    self._save_transaction(current_date, current_desc, current_amount,
                                           in_deposits, in_withdrawals)
                current_date = current_desc = current_amount = None
                in_deposits, in_withdrawals, in_checks = False, False, True
                continue

            if any(x in line for x in ['Total deposits', 'Total withdrawals', 'Total checks',
                                        'Daily ledger balances']):
                if current_date and current_desc and current_amount is not None:
                    self._save_transaction(current_date, current_desc, current_amount,
                                           in_deposits, in_withdrawals)
                current_date = current_desc = current_amount = None
                in_deposits = in_withdrawals = in_checks = False
                continue

            # Capture total service fees line e.g. 'Total service fees -$8.00'
            if 'Total service fees' in line:
                m = re.search(r'-\$?([\d,]+\.\d{2})', line)
                if m:
                    self.service_fees = Decimal(m.group(1).replace(',', ''))
                in_deposits = in_withdrawals = in_checks = False
                continue

            if 'Service fees' in line and 'Total' not in line:
                if current_date and current_desc and current_amount is not None:
                    self._save_transaction(current_date, current_desc, current_amount,
                                           in_deposits, in_withdrawals)
                current_date = current_desc = current_amount = None
                in_deposits = in_withdrawals = in_checks = False
                continue

            if line.strip().startswith('Date') and 'Description' in line:
                continue
            if 'Subtotal for card account' in line or 'Card account #' in line:
                continue

            if in_deposits or in_withdrawals:
                dm = re.match(r'^(\d{2}/\d{2}/\d{2})\s+(.+)', line)
                if dm:
                    if current_date and current_desc and current_amount is not None:
                        self._save_transaction(current_date, current_desc, current_amount,
                                               in_deposits, in_withdrawals)
                    current_date = dm.group(1)
                    rest = dm.group(2).strip()
                    am = re.search(r'([-]?[\d,]+\.\d{2})$', rest)
                    if am:
                        current_amount = Decimal(am.group(1).replace(',', ''))
                        current_desc = rest[:am.start()].strip()
                        for pat in [r'\s+DES:.*$', r'\s+ID:.*$', r'\s+INDN:.*$',
                                    r'\s+Confirmation#.*$', r'\s+Page\s+\d+\s+of\s+\d+.*$']:
                            current_desc = re.sub(pat, '', current_desc, flags=re.IGNORECASE)
                        current_desc = current_desc.strip()
                    else:
                        current_desc = rest
                        current_amount = None
                else:
                    skip = ['continued on the next page', 'Your checking account', 'Account #',
                            'Available in English', 'Make bank transfers', 'Use our app',
                            'international wires', 'Scan the code', 'When you use the QRC',
                            'Mobile Banking requires', 'Message and data rates',
                            'Fees or other costs', 'bofa.com', 'bankofamerica.com']
                    if any(s in line for s in skip):
                        continue
                    if re.search(r'^Page\s+\d+\s+of\s+\d+', line.strip()):
                        continue
                    if re.search(r'Account\s*#\s*\d', line):
                        continue
                    if self.client_name and self.client_name in line:
                        continue
                    if current_amount is None:
                        am = re.search(r'([-]?[\d,]+\.\d{2})$', line)
                        if am:
                            current_amount = Decimal(am.group(1).replace(',', ''))
                            extra = line[:am.start()].strip()
                            if extra and not extra.startswith('ID:'):
                                current_desc = (current_desc or '') + ' ' + extra
                    else:
                        if not line.strip().startswith('ID:'):
                            current_desc = (current_desc or '') + ' ' + line.strip()

            if in_checks:
                # Two-column layout: "03/18/26 -2,087.00 03/23/26 1242* -380.00"
                # or single: "03/05/26 1239 -380.00"
                check_pat = r'(\d{2}/\d{2}/\d{2})\s+(\d+\*?)?\s*([-]?[\d,]+\.\d{2})'
                for match in re.findall(check_pat, line):
                    date_c, chk_num, amt = match
                    self.checks.append({
                        'date': date_c,
                        'check_number': chk_num.rstrip('*') if chk_num else '',
                        'amount': Decimal(amt.replace(',', '')),
                        'payee': ''
                    })

    def _save_transaction(self, date, desc, amount, in_deposits, in_withdrawals):
        if in_deposits:
            self.credits.append({'date': date, 'vendor': desc, 'amount': amount})
        elif in_withdrawals:
            self.debits.append({'date': date, 'vendor': desc, 'amount': amount})

    def extract_check_payees(self, check_payee_map=None, check_date_map=None):
        """Apply check payee/date maps and optionally use OCR to extract payee names."""
        if check_payee_map is None:
            check_payee_map = {}
        if check_date_map is None:
            check_date_map = {}

        # Apply payee and date maps — match by check number, or fall back to post date as key
        for check in self.checks:
            check_num = check.get('check_number', '')
            post_date = check.get('date', '')
            key = check_num if check_num else post_date
            if key and key in check_payee_map:
                check['payee'] = check_payee_map[key]
            elif post_date in check_payee_map:
                check['payee'] = check_payee_map[post_date]
            # Override date with check-written date if provided
            if check_num and check_num in check_date_map:
                check['check_date'] = check_date_map[check_num]
            elif post_date in check_date_map:
                check['check_date'] = check_date_map[post_date]

        if not OCR_AVAILABLE:
            return
        try:
            pdf = fitz.open(self.pdf_path)
            for page_num in range(len(pdf)):
                page = pdf[page_num]
                if 'Check images' in page.get_text():
                    images = page.get_images()
                    if images:
                        for check in self.checks:
                            if check.get('payee'):  # already set by manual map
                                continue
                            try:
                                xref = images[-1][0]
                                base_image = pdf.extract_image(xref)
                                img = Image.open(_io.BytesIO(base_image['image']))
                                ocr_text = pytesseract.image_to_string(img)
                                check['payee'] = self._extract_payee_from_ocr(ocr_text)
                            except Exception:
                                check['payee'] = ''
                    break
            pdf.close()
        except Exception:
            pass

    def _extract_payee_from_ocr(self, ocr_text):
        lines = ocr_text.split('\n')
        for i, line in enumerate(lines):
            lu = line.upper().strip()
            # BofA bill payment checks: 'To. PAYEE NAME' format
            if re.match(r'^TO[\.\ ]', lu):
                p = re.sub(r'^TO[\.\ ]+', '', line, flags=re.IGNORECASE).strip()
                p = self._clean_ocr_payee(p)
                if p and len(p) > 2:
                    return p
            if 'PAY' in lu and 'ORDER' in lu:
                if i + 1 < len(lines):
                    p = self._clean_ocr_payee(lines[i + 1])
                    if p:
                        return p
                if 'ORDER OF' in lu:
                    parts = line.split('ORDER OF', 1)
                    if len(parts) > 1:
                        p = self._clean_ocr_payee(parts[1])
                        if p:
                            return p
        return ''

    def _clean_ocr_payee(self, text):
        cleaned = re.sub(r'^[^A-Za-z]+', '', text.strip())
        cleaned = re.sub(r'[^A-Za-z\s\-\.]+$', '', cleaned)
        cleaned = re.sub(r'[^A-Za-z\s\-\.]', ' ', cleaned)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        words = cleaned.split()
        if len(words) > 2:
            cleaned = ' '.join(words[:2])
        if len(cleaned) >= 3 and any(c.isalpha() for c in cleaned):
            return cleaned.title()
        return ''

    def aggregate_transactions(self):
        """Separate and aggregate credits/debits with client-specific rules."""
        aggs = self.transaction_aggregations()
        online_banking_credits, other_credits = [], []
        agg_credits = {a['label']: [] for a in aggs}
        for t in self.credits:
            vu = t['vendor'].upper()
            if 'ONLINE BANKING TRANSFER' in vu:
                online_banking_credits.append(
                    {'date': t['date'], 'vendor': t['vendor'],
                     'amount': t['amount'], 'count': 1})
                continue
            rule = next((a for a in aggs if a['match'] in vu), None)
            if rule:
                agg_credits[rule['label']].append(t)
            else:
                other_credits.append(t)

        credit_totals = defaultdict(lambda: {'total': Decimal('0'), 'count': 0, 'latest_date': None})
        for t in other_credits:
            v = self.normalize_vendor(t['vendor'])
            credit_totals[v]['total'] += t['amount']
            credit_totals[v]['count'] += 1
            d = datetime.strptime(t['date'], '%m/%d/%y')
            if credit_totals[v]['latest_date'] is None or d > credit_totals[v]['latest_date']:
                credit_totals[v]['latest_date'] = d

        aggregated_credits = [
            {'date': data['latest_date'].strftime('%m/%d/%y'), 'vendor': v,
             'amount': data['total'], 'count': data['count']}
            for v, data in credit_totals.items()
        ]
        aggregated_credits.extend(online_banking_credits)

        for label, txns in agg_credits.items():
            if txns:
                aggregated_credits.append(self._rollup_line(txns, label))

        aggregated_credits.sort(key=lambda x: datetime.strptime(x['date'], '%m/%d/%y'))

        adp_debits, online_banking_debits, credit_card_payments, other_debits = [], [], [], []
        agg_debits = {a['label']: [] for a in aggs}
        for t in self.debits:
            d_upper = t['vendor'].upper()
            if 'ADP' in d_upper:
                adp_debits.append(
                    {'date': t['date'], 'vendor': t['vendor'],
                     'amount': t['amount'], 'count': 1})
            elif 'ONLINE BANKING TRANSFER' in d_upper:
                online_banking_debits.append(
                    {'date': t['date'], 'vendor': t['vendor'],
                     'amount': t['amount'], 'count': 1})
            elif ('CITI CARD' in d_upper or 'CREDIT CARD' in d_upper or 'CITICTP' in d_upper or
                  any(kw.upper() in d_upper for kw in
                      (_registry.get_config(self.client_name) or {}).get('cc_keywords', []))):
                credit_card_payments.append(
                    {'date': t['date'], 'vendor': t['vendor'],
                     'amount': t['amount'], 'count': 1})
            else:
                rule = next((a for a in aggs if a['match'] in d_upper), None)
                if rule:
                    agg_debits[rule['label']].append(t)
                else:
                    other_debits.append(t)

        debit_totals = defaultdict(lambda: {'total': Decimal('0'), 'count': 0, 'latest_date': None})
        for t in other_debits:
            v = self.normalize_vendor(t['vendor'])
            debit_totals[v]['total'] += t['amount']
            debit_totals[v]['count'] += 1
            d = datetime.strptime(t['date'], '%m/%d/%y')
            if debit_totals[v]['latest_date'] is None or d > debit_totals[v]['latest_date']:
                debit_totals[v]['latest_date'] = d

        aggregated_other_debits = [
            {'date': data['latest_date'].strftime('%m/%d/%y'), 'vendor': v,
             'amount': data['total'], 'count': data['count']}
            for v, data in debit_totals.items()
        ]

        all_debits = aggregated_other_debits
        all_debits.sort(key=lambda x: datetime.strptime(x['date'], '%m/%d/%y'))
        online_banking_debits.sort(key=lambda x: datetime.strptime(x['date'], '%m/%d/%y'))
        credit_card_payments.sort(key=lambda x: datetime.strptime(x['date'], '%m/%d/%y'))
        adp_debits.sort(key=lambda x: datetime.strptime(x['date'], '%m/%d/%y'))

        aggregated_special_debits = []
        for label, txns in agg_debits.items():
            if txns:
                aggregated_special_debits.append(self._rollup_line(txns, label))

        return aggregated_credits, all_debits, credit_card_payments, adp_debits, aggregated_special_debits, online_banking_debits

    def generate_report(self, check_payee_map=None, check_date_map=None):
        self.extract_check_payees(check_payee_map, check_date_map)
        credits, debits, cc_payments, adp, special_debits, transfers = self.aggregate_transactions()

        total_credits        = sum(c['amount'] for c in credits)
        total_debits         = sum(d['amount'] for d in debits)
        total_cc             = sum(p['amount'] for p in cc_payments)
        total_adp            = sum(a['amount'] for a in adp)
        total_checks         = sum(c['amount'] for c in self.checks)
        total_special_debits = sum(p['amount'] for p in special_debits)
        total_transfers      = sum(t['amount'] for t in transfers)
        service_fees      = self.service_fees

        # Combine cc_payments and online banking transfers into one transfers list
        all_transfers = sorted(
            [{'date': p['date'], 'vendor': p['vendor'], 'amount': p['amount']} for p in cc_payments] +
            [{'date': t['date'], 'vendor': t['vendor'], 'amount': t['amount']} for t in transfers],
            key=lambda x: _safe_date_key(x['date'])
        )
        total_all_transfers = total_cc + total_transfers
        total_all_deb = abs(total_debits) + abs(total_adp) + abs(total_all_transfers) + abs(total_checks) + service_fees

        calc = (self.beginning_balance + total_credits + total_debits
                + total_adp + total_special_debits + total_all_transfers + total_checks - service_fees)
        ok = abs(calc - self.ending_balance) < Decimal('0.01')

        report = _report_header(self.statement_type, self.client_name,
                                statement_date=self._extract_period())

        summary_rows = [
            ('Beginning Balance',         self.beginning_balance),
            ('Deposits and Credits',       total_credits),
            ('Withdrawals and Debits',     -total_all_deb),
            ('  Checks',                   abs(total_checks) if total_checks else None, 'indent'),
            ('  Payroll',                  abs(total_adp) if total_adp else None, 'indent'),
            ('  Credit Card Payments',     abs(total_all_transfers) if total_all_transfers else None, 'indent'),
            ('  Bank Fees',                service_fees if service_fees else None, 'indent'),
            ('Ending Balance',             self.ending_balance),
        ]
        report += _summary_block(summary_rows)
        report += _balance_check(ok, calc)

        report += _deposits_section(credits, total_credits)
        if debits:
            deb_rows = sorted(debits, key=lambda x: _safe_date_key(x['date']))
            report += _individual_section(deb_rows, total_debits, 'WITHDRAWALS AND DEBITS')
        if self.checks:
            report += _checks_section(self.checks, total_checks)
        if adp:
            report += _adp_section(adp, total_adp)
        if all_transfers:
            cfg = _registry.get_config(self.client_name) or {} if self.client_name else {}
            cc_group_keywords = cfg.get('cc_payment_group_keywords', None)

            # For each CC vendor mapping, determine the last reconciled statement date
            # from reconciliation_log.csv. A payment is "pending" if its date is
            # AFTER the last reconciled statement date for that card.
            cc_reconciled_dates = {}  # account_type -> last reconciled date (datetime)
            try:
                import csv as _csv
                from pathlib import Path as _Path
                from datetime import datetime as _dt
                from log_utils import get_logs_dir as _get_logs_dir
                # reconciliation_log.csv lives in the private logs dir.
                recon_log = _get_logs_dir() / 'reconciliation_log.csv'
                if recon_log.exists():
                    with open(recon_log, newline='') as _f:
                        for row in _csv.DictReader(_f):
                            if row.get('client', '').upper() == (self.client_name or '').upper():
                                at = row.get('account_type', '').lower()
                                sd = row.get('statement_date', '').strip()
                                if sd:
                                    for fmt in ('%m/%d/%y', '%m/%d/%Y', '%Y-%m-%d'):
                                        try:
                                            d = _dt.strptime(sd, fmt).date()
                                            if at not in cc_reconciled_dates or d > cc_reconciled_dates[at]:
                                                cc_reconciled_dates[at] = d
                                            break
                                        except ValueError:
                                            continue
            except Exception:
                pass

            # Tag each transfer as pending if its date is after the last reconciled CC date
            cc_vendor_map = cfg.get('cc_vendor_account_map', {})
            # cc_vendor_account_map: {keyword: account_type}, e.g. {"CITI": "citi_costco"}
            annotated_transfers = []
            for t in all_transfers:
                vendor_upper = t['vendor'].upper()
                pending = False
                for kw, at in cc_vendor_map.items():
                    if kw.upper() in vendor_upper:
                        last_recon = cc_reconciled_dates.get(at)
                        if last_recon:
                            try:
                                from datetime import datetime as _dt2
                                for fmt in ('%m/%d/%y', '%m/%d/%Y'):
                                    try:
                                        pay_date = _dt2.strptime(t['date'], fmt).date()
                                        break
                                    except ValueError:
                                        pay_date = None
                                if pay_date and pay_date > last_recon:
                                    pending = True
                            except Exception:
                                pass
                        else:
                            pending = True  # no reconciled date at all → pending
                        break
                annotated_transfers.append({**t, 'pending': pending})

            report += _cc_payments_section(annotated_transfers, total_all_transfers,
                                           title='CREDIT CARD PAYMENTS',
                                           group_vendor_keywords=cc_group_keywords)
        # Bank Fees shown in summary only
        return report

    def _extract_period(self):
        m = re.search(r'for\s+(\w+ \d+, \d{4})\s+to\s+(\w+ \d+, \d{4})', self.text)
        if m:
            return f"{m.group(1)} - {m.group(2)}"
        return 'Unknown Period'


# ═══════════════════════════════════════════════════════════════════════════════
# SAVINGS ACCOUNT PARSERS
# ═══════════════════════════════════════════════════════════════════════════════

class BankOfAmericaSavingsParser(BankOfAmericaCheckingParser):
    """
    Bank of America Business Savings.
    Same transaction structure as BofA Checking.
    """
    statement_type = "Bank of America Business Savings"

    def generate_report(self, check_payee_map=None):
        # Savings accounts don't have checks or CC payments sections
        self.extract_check_payees(check_payee_map)
        credits, debits, _, _, _, online_banking = self.aggregate_transactions()

        # Online banking transfers (e.g. transfers to checking) are debits
        # on savings accounts — include them in the withdrawals section
        all_debits = debits + [
            {**t, 'amount': abs(t['amount'])}
            for t in online_banking
            if t.get('amount', 0) < 0
        ]

        total_credits = sum(c['amount'] for c in credits)
        total_debits  = sum(d['amount'] for d in all_debits)

        report = _report_header(self.statement_type, self.client_name,
                                statement_date=self._extract_period())
        report += _summary_block([
            ('Beginning Balance',      self.beginning_balance),
            ('Deposits and Credits',   total_credits),
            ('Withdrawals and Debits', total_debits),
            ('Ending Balance',         self.ending_balance),
        ])

        calc = self.beginning_balance + total_credits - total_debits
        ok = abs(calc - self.ending_balance) < Decimal('0.01')
        report += _balance_check(ok, calc)

        if not ok:
            raise ValueError(
                f"BankOfAmericaSavingsParser balance check FAILED: "
                f"computed {calc} != statement {self.ending_balance} "
                f"(diff {abs(calc - self.ending_balance)}). "
                f"Credits={total_credits}, Debits={total_debits}. "
                f"Check for missing transactions."
            )

        report += _deposits_section(credits, total_credits)
        report += _charges_section(all_debits, total_debits, title='WITHDRAWALS AND DEBITS')
        return report


# ═══════════════════════════════════════════════════════════════════════════════
# NORTHERN TRUST CHECKING PARSER
# ═══════════════════════════════════════════════════════════════════════════════


