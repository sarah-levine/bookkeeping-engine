import sys
import re
import os
import json
import subprocess
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

from parsers.base import StatementParser, _registry, KNOWN_CLIENTS
from parsers.report import *
from parsers.report import _safe_date_key, _report_header, _summary_block, _balance_check, \
    _payments_section, _credits_section, _charges_section

class BMOCheckingParser(StatementParser):
    """
    BMO Premium Business Checking.
    Statements are typically scanned images — uses PyMuPDF + pytesseract for OCR.
    BMO statement format:
      - Columns: Date | Transaction description | Withdrawal | Deposit | Balance
      - Beginning/Ending balance labeled "BEGINNING BALANCE" / "ENDING BALANCE"
      - Dates formatted as "Feb 02", "Feb 03", etc.
    """
    statement_type = "BMO Premium Business Checking"

    MONTH_MAP = {
        'JAN': '01', 'FEB': '02', 'MAR': '03', 'APR': '04',
        'MAY': '05', 'JUN': '06', 'JUL': '07', 'AUG': '08',
        'SEP': '09', 'OCT': '10', 'NOV': '11', 'DEC': '12',
    }

    def __init__(self, pdf_path, client_name=None):
        self.pdf_path = pdf_path
        self.client_name = client_name
        self.beginning_balance = None
        self.ending_balance = None
        self.credits  = []   # deposits
        self.debits   = []   # withdrawals (stored as negative Decimal)
        self.checks   = []   # check items (stored as negative Decimal)
        self.service_fees = Decimal('0')
        self._ocr_text = None
        self.text = self._extract_text()
        if not self.client_name:
            self.client_name = self._detect_client()

    # ── text extraction ──────────────────────────────────────────────────────

    def _extract_text(self):
        """Try pdftotext first; fall back to OCR."""
        # pdftotext (for digital PDFs)
        try:
            result = subprocess.run(
                ['pdftotext', '-layout', str(self.pdf_path), '-'],
                capture_output=True, text=True, check=True
            )
            if result.stdout.strip():
                self._ocr_text = result.stdout
                return self._ocr_text
        except Exception:
            pass

        # OCR fallback (scanned images)
        try:
            import fitz
            from PIL import Image
            import pytesseract
            doc = fitz.open(self.pdf_path)
            pages = []
            for page in doc:
                mat = fitz.Matrix(1.0, 1.0)  # balance speed vs accuracy
                pix = page.get_pixmap(matrix=mat)
                img = Image.frombytes('RGB', [pix.width, pix.height], pix.samples)
                # Resize to max 2000px wide to cap memory and OCR time
                if img.width > 2000:
                    ratio = 2000 / img.width
                    img = img.resize((2000, int(img.height * ratio)), Image.LANCZOS)
                pages.append(pytesseract.image_to_string(img))
            doc.close()
            self._ocr_text = '\n'.join(pages)
            return self._ocr_text
        except Exception:
            return ''

    def _detect_client(self):
        return super()._detect_client()

    def normalize_vendor(self, description):
        result = _registry.normalize_vendor(self.client_name or '', description)
        if result != description:
            return result
        return description.strip()

    # ── parsing ───────────────────────────────────────────────────────────────

    def _parse_amount(self, s):
        """Convert '$1,234.56' or '($1,234.56)' or '1234.56' to Decimal."""
        s = s.strip().replace('$', '').replace(',', '')
        negative = s.startswith('(') and s.endswith(')')
        s = s.strip('()')
        try:
            val = Decimal(s)
            return -val if negative else val
        except Exception:
            return None

    def _parse_date(self, month_str, day_str, year):
        """Convert 'Feb', '02', 2026 -> '02/02/26'."""
        mo = self.MONTH_MAP.get(month_str.upper()[:3], '01')
        yy = str(year)[2:]
        return f"{mo}/{day_str.zfill(2)}/{yy}"

    def _get_statement_year(self):
        for y in re.findall(r'\b(20\d{2})\b', self.text):
            return int(y)
        return 2026

    def parse(self):
        lines = self.text.split('\n')
        year = self._get_statement_year()

        # ── balance lines ────────────────────────────────────────────────────
        for line in lines:
            upper = line.upper()
            if 'BEGINNING BALANCE' in upper and self.beginning_balance is None:
                amounts = re.findall(r'[\$]?([\d,]+\.\d{2})', line)
                if amounts:
                    self.beginning_balance = Decimal(amounts[-1].replace(',', ''))
            if 'ENDING BALANCE' in upper and self.ending_balance is None:
                amounts = re.findall(r'[\$]?([\d,]+\.\d{2})', line)
                if amounts:
                    self.ending_balance = Decimal(amounts[-1].replace(',', ''))

        # ── transaction lines ─────────────────────────────────────────────────
        # BMO layout (pdftotext -layout):
        #   "Feb 02   Check 4867                           ($338.48)               $40,214.28"
        #   "Feb 03   ACH DEPOSIT                                       $2,907.62   $43,122.40"
        #   "         CCD iWallet cards  iWallet ca"    <- continuation (no date)
        # Withdrawal column is left of Deposit column; both may be absent on a given row.

        current_date = None
        pending_desc_parts = []
        pending_amount = None
        pending_is_credit = None

        def flush_pending():
            if current_date and pending_amount is not None:
                desc = ' '.join(pending_desc_parts).strip()
                # Strip trailing balance figures from description
                desc = re.sub(r'\s+[\$]?[\d,]+\.\d{2}\s*$', '', desc).strip()
                vendor = self.normalize_vendor(desc)
                if pending_is_credit:
                    self.credits.append({'date': current_date, 'vendor': vendor,
                                         'amount': pending_amount})
                else:
                    # Check lines
                    check_m = re.match(r'(?:Check|Chk)\s+(\d+)', desc, re.IGNORECASE)
                    if check_m:
                        self.checks.append({'date': current_date,
                                            'number': check_m.group(1),
                                            'amount': pending_amount,
                                            'vendor': vendor})
                    elif re.search(r'non.?std|straps|service.?fee|maintenance', desc, re.IGNORECASE):
                        self.service_fees += abs(pending_amount)
                    else:
                        self.debits.append({'date': current_date, 'vendor': vendor,
                                            'amount': pending_amount})

        # Regex for a leading date: "Feb 02" or "Feb  2"
        date_re = re.compile(
            r'^\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})\s+(.+)',
            re.IGNORECASE
        )
        # Amount pattern (withdrawal OR deposit) at end of a transaction line
        # Withdrawal: ($1,234.56)  or  -1,234.56
        # Deposit:    $1,234.56   (no parens)
        # Balance trail: last bare number is the running balance — skip it
        amount_re = re.compile(
            r'(\([\d,]+\.\d{2}\)|(?<![\d,])[\d,]+\.\d{2})(?:\s+[\$]?[\d,]+\.\d{2})?\s*$'
        )

        for line in lines:
            upper = line.upper()
            if 'CONTINUED ON NEXT PAGE' in upper:
                continue
            if 'MONTHLY ACTIVITY DETAILS' in upper:
                continue
            if re.match(r'^\s*(?:Date|Transaction description|Withdrawal|Deposit|Balance)\s*$',
                        line, re.IGNORECASE):
                continue

            dm = date_re.match(line)
            if dm:
                flush_pending()
                month_str = dm.group(1)
                day_str   = dm.group(2)
                rest      = dm.group(3).strip()
                current_date = self._parse_date(month_str, day_str, year)

                am = amount_re.search(rest)
                if am:
                    raw = am.group(1)
                    is_credit = not (raw.startswith('(') or raw.startswith('-'))
                    val = self._parse_amount(raw)
                    if val is not None:
                        pending_amount    = val if is_credit else -abs(val)
                        pending_is_credit = is_credit
                        desc_part = rest[:am.start()].strip()
                        pending_desc_parts = [desc_part] if desc_part else []
                    else:
                        pending_amount = None
                        pending_is_credit = None
                        pending_desc_parts = [rest]
                else:
                    pending_amount = None
                    pending_is_credit = None
                    pending_desc_parts = [rest]
            else:
                # Continuation line (no date)
                stripped = line.strip()
                if not stripped:
                    continue
                # If continuation carries the amount (sometimes layout shifts it)
                if pending_amount is None and current_date:
                    am = amount_re.search(stripped)
                    if am:
                        raw = am.group(1)
                        is_credit = not (raw.startswith('(') or raw.startswith('-'))
                        val = self._parse_amount(raw)
                        if val is not None:
                            pending_amount    = val if is_credit else -abs(val)
                            pending_is_credit = is_credit
                            desc_part = stripped[:am.start()].strip()
                            if desc_part:
                                pending_desc_parts.append(desc_part)
                            continue
                if current_date and stripped and not re.match(
                        r'^[\d,]+\.\d{2}$', stripped):
                    pending_desc_parts.append(stripped)

        flush_pending()

    # ── report ────────────────────────────────────────────────────────────────

    def load_from_dict(self, data):
        """
        Populate parser state from a pre-extracted data dict.
        Used when OCR output is too noisy (e.g. photographed statements).
        credits: [{'date': 'MM/DD/YY', 'vendor': str, 'amount': Decimal}, ...]
        checks:  [{'date': 'MM/DD/YY', 'number': str, 'amount': Decimal, 'vendor': str}, ...]
        debits:  [{'date': 'MM/DD/YY', 'vendor': str, 'amount': Decimal}, ...]  (positive values)
        """
        self.beginning_balance = Decimal(str(data.get('beginning_balance', 0)))
        self.ending_balance    = Decimal(str(data.get('ending_balance', 0)))
        self.credits           = data.get('credits', [])
        self.checks            = data.get('checks', [])
        self.debits            = data.get('debits', [])
        self.service_fees      = Decimal(str(data.get('service_fees', 0)))
        self.statement_period  = data.get('statement_period', '')
        self.client_name       = data.get('client_name', self.client_name)

    def generate_report(self, check_payee_map=None, check_date_map=None):
        check_payee_map = check_payee_map or {}
        check_date_map  = check_date_map  or {}

        def norm(desc):
            return _registry.normalize_vendor(self.client_name or '', desc)

        def agg(txns):
            """Aggregate transactions by vendor — same pattern as BofA parser."""
            from collections import defaultdict
            totals = defaultdict(lambda: {'amount': Decimal('0'), 'count': 0, 'date': ''})
            for t in txns:
                v = norm(t['vendor'])
                totals[v]['amount'] += Decimal(str(t['amount']))
                totals[v]['count']  += 1
                totals[v]['date']    = t['date']
            return sorted(
                [{'date': d['date'], 'vendor': v, 'amount': d['amount'], 'count': d['count']}
                 for v, d in totals.items()],
                key=lambda x: _safe_date_key(x['date'])
            )

        # Split debits into payroll vs other vs bank fees — mirrors BofA pattern
        cfg = _registry.get_config(self.client_name) or {}
        payroll_kws = [kw.upper() for kw in cfg.get('payroll_vendors', [])]

        payroll_txns  = [t for t in self.debits if any(kw in t['vendor'].upper() for kw in payroll_kws)]
        fee_txns      = [t for t in self.debits if re.search(r'straps|maintenance|service.?fee', t['vendor'], re.IGNORECASE)]
        other_txns    = [t for t in self.debits if t not in payroll_txns and t not in fee_txns]

        # Also fold service_fees scalar into fees display
        agg_credits = agg(self.credits)
        agg_other   = agg(other_txns)
        pay_rows    = sorted([{'date': t['date'], 'vendor': norm(t['vendor']),
                                'amount': Decimal(str(t['amount'])), 'count': 1}
                               for t in payroll_txns],
                              key=lambda x: _safe_date_key(x['date']))
        fee_rows    = sorted([{'date': t['date'], 'vendor': norm(t['vendor']),
                                'amount': Decimal(str(t['amount'])), 'count': 1}
                               for t in fee_txns],
                              key=lambda x: _safe_date_key(x['date']))

        # Split checks: 5-digit checks starting with "10" are payroll checks
        def is_payroll_check(num):
            return bool(re.match(r'^10\d{3}$', str(num)))

        check_rows   = []
        payroll_checks = []
        for ck in self.checks:
            num   = ck.get('number', '')
            payee = check_payee_map.get(num, ck.get('vendor', ''))
            date  = check_date_map.get(num, ck['date'])
            row = {'check_num': num, 'check_number': num,
                   'date': date, 'payee': payee,
                   'amount': Decimal(str(ck['amount']))}
            if is_payroll_check(num):
                payroll_checks.append({'date': date, 'vendor': f'Payroll Check #{num}',
                                        'amount': Decimal(str(ck['amount'])), 'count': 1})
            else:
                check_rows.append(row)

        total_dep   = sum(r['amount'] for r in agg_credits)
        total_other = sum(r['amount'] for r in agg_other)
        total_pay   = sum(r['amount'] for r in pay_rows) + sum(r['amount'] for r in payroll_checks)
        total_fees  = sum(r['amount'] for r in fee_rows) or Decimal(str(self.service_fees))
        total_chk   = sum(Decimal(str(c['amount'])) for c in self.checks if not is_payroll_check(c.get('number','')))
        total_all_deb = total_other + total_pay + total_fees + total_chk

        period = getattr(self, 'statement_period', '') or ''
        if not period and self.text:
            m = re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2},?\s+\d{4}',
                           self.text, re.IGNORECASE)
            if m:
                period = m.group(0)

        report  = _report_header(self.statement_type, self.client_name,
                                  statement_date=period)
        report += _summary_block([
            ('Beginning Balance',     self.beginning_balance),
            ('Deposits and Credits',  total_dep),
            ('Withdrawals and Debits', total_all_deb),
            ('  Checks',              total_chk   if total_chk   else None, 'indent'),
            ('  Payroll',             total_pay   if total_pay   else None, 'indent'),
            ('  Bank Fees',           total_fees  if total_fees  else None, 'indent'),
            ('Ending Balance',        self.ending_balance),
        ])

        if self.beginning_balance and self.ending_balance:
            calc = self.beginning_balance + total_dep - total_all_deb
            ok   = abs(calc - self.ending_balance) < Decimal('0.05')
            report += _balance_check(ok, calc)

        report += _deposits_section(agg_credits, total_dep)

        if other_txns:
            # ServicePower consolidates into one line; all others shown individually
            sp_total = Decimal('0')
            sp_date  = ''
            wd_rows  = []
            for t in sorted(other_txns, key=lambda x: _safe_date_key(x['date'])):
                if 'servicepower' in t['vendor'].lower():
                    sp_total += Decimal(str(t['amount']))
                    sp_date   = t['date']
                else:
                    wd_rows.append((t['date'], norm(t['vendor']), Decimal(str(t['amount']))))
            if sp_total:
                wd_rows.append((sp_date, 'ServicePower', sp_total))
                wd_rows.sort(key=lambda x: _safe_date_key(x[0]))

            W = 80
            lines_wd = ['=' * W, 'WITHDRAWALS AND DEBITS', '',
                        f"{'Date':<12} {'Description':<50} {'Amount':>16}",
                        '-' * W]
            for date, vendor, amount in wd_rows:
                lines_wd.append(f"{date:<12} {vendor:<50} ${amount:>15,.2f}")
            lines_wd.append(f"{'':63} {'-' * 17}")
            lines_wd.append(f"{'TOTAL WITHDRAWALS AND DEBITS:':<63} ${total_other:>15,.2f}")
            lines_wd.append('')
            report += '\n'.join(lines_wd) + '\n'

        if check_rows:
            report += _checks_section(check_rows, total_chk)
        # Note: payroll_checks (10xxxxx) are rendered in PAYROLL section above
        if pay_rows or payroll_checks:
            all_pay = sorted(pay_rows + payroll_checks, key=lambda x: _safe_date_key(x['date']))
            report += _adp_section(all_pay, total_pay)
        if fee_rows:
            report += _individual_section(fee_rows, total_fees, 'BANK FEES')

        return report


class BMOCreditCardParser(StatementParser):
    """
    BMO Business Platinum Rewards Credit Card parser.

    Supports pdftotext-based extraction (when poppler-utils is available)
    and manual load_from_dict() for pre-extracted data (scanned PDFs).
    """
    statement_type = "BMO Business Platinum Rewards Credit Card"

    def __init__(self, pdf_path=None, client_name=None):
        self.pdf_path = pdf_path
        self.client_name = client_name
        self.previous_balance = None
        self.new_balance = None
        self.total_payments = Decimal('0')
        self.payments = []
        self.credits = []
        self.charges = []
        self.statement_period = ''

        if pdf_path:
            self.text = self._extract_text()
            if not self.client_name:
                self.client_name = self._detect_client()
        else:
            self.text = ''

    def _extract_text(self):
        """Try pdftotext first; fall back to PyMuPDF + pytesseract for scanned PDFs."""
        try:
            result = subprocess.run(
                ['pdftotext', '-layout', str(self.pdf_path), '-'],
                capture_output=True, text=True, check=True
            )
            if result.stdout.strip():
                self._ocr_text = result.stdout
                return self._ocr_text
        except Exception:
            pass

        try:
            import fitz
            from PIL import Image
            import pytesseract
            doc = fitz.open(self.pdf_path)
            pages = []
            for page in doc:
                mat = fitz.Matrix(1.0, 1.0)
                pix = page.get_pixmap(matrix=mat)
                img = Image.frombytes('RGB', [pix.width, pix.height], pix.samples)
                if img.width > 2000:
                    ratio = 2000 / img.width
                    img = img.resize((2000, int(img.height * ratio)), Image.LANCZOS)
                pages.append(pytesseract.image_to_string(img))
            doc.close()
            self._ocr_text = '\n'.join(pages)
            return self._ocr_text
        except Exception:
            return ''

    def _detect_client(self):
        return super()._detect_client()

    def _expand_date(self, date_str):
        """Normalize MM/DD, MM/DD/YY, or MM/DD/YYYY to MM/DD/YYYY."""
        from datetime import datetime as _dt
        if re.match(r'^\d{2}/\d{2}/\d{4}$', date_str):
            return date_str
        year = _dt.now().year
        if self.statement_period:
            m = re.search(r'\d{4}', self.statement_period)
            if m:
                year = int(m.group())
        if re.match(r'^\d{2}/\d{2}/\d{2}$', date_str):
            yy = int(date_str[-2:])
            yyyy = 2000 + yy if yy < 70 else 1900 + yy
            return date_str[:5] + '/' + str(yyyy)
        if re.match(r'^\d{2}/\d{2}$', date_str):
            return date_str + '/' + str(year)
        return date_str

    def _normalize_dates(self):
        for lst in (self.charges, self.payments, self.credits):
            for t in lst:
                if 'date' in t:
                    t['date'] = self._expand_date(t['date'])

    def load_from_dict(self, data):
        """
        Populate parser state from a pre-extracted data dict.

        charges:  [{'date': 'MM/DD/YYYY', 'vendor': str, 'amount': Decimal}, ...]
        payments: [{'date': 'MM/DD/YYYY', 'description': str, 'amount': Decimal}, ...]
        credits:  [{'date': 'MM/DD/YYYY', 'description': str, 'amount': Decimal}, ...]
        """
        self.previous_balance = Decimal(str(data.get('previous_balance', 0)))
        self.new_balance      = Decimal(str(data.get('new_balance', 0)))
        self.total_payments   = Decimal(str(data.get('total_payments', 0)))
        self.statement_period = data.get('statement_period', '')
        self.client_name      = data.get('client_name', self.client_name)
        self.payments         = data.get('payments', [])
        self.credits          = data.get('credits', [])
        self.charges          = data.get('charges', [])
        self._normalize_dates()

    def normalize_vendor(self, description):
        result = _registry.normalize_vendor(self.client_name or '', description)
        return result if result != description else description.strip()

    def parse(self):
        """Parse from pdftotext output. BMO credit card layout."""
        if not self.text.strip():
            return

        lines = self.text.split('\n')

        for line in lines:
            upper = line.upper()
            if 'PREVIOUS BALANCE' in upper and self.previous_balance is None:
                amounts = re.findall(r'\$?([\d,]+\.\d{2})', line)
                if amounts:
                    self.previous_balance = Decimal(amounts[-1].replace(',', ''))
            if 'NEW BALANCE' in upper and self.new_balance is None:
                amounts = re.findall(r'\$?([\d,]+\.\d{2})', line)
                if amounts:
                    self.new_balance = Decimal(amounts[-1].replace(',', ''))
            if 'STATEMENT CLOSE DATE' in upper or 'STATEMENT PERIOD' in upper:
                m = re.search(r'((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2},?\s+\d{4})',
                              line, re.IGNORECASE)
                if m and not self.statement_period:
                    self.statement_period = m.group(1)

        # Transaction rows: MM/DD  MM/DD  description  [ref]  amount [CR]
        txn_re = re.compile(
            r'^(\d{2}/\d{2})\s+(\d{2}/\d{2})\s+(.+?)\s{2,}([\d,]+\.\d{2})\s*(CR)?\s*$'
        )
        for line in lines:
            m = txn_re.match(line.strip())
            if not m:
                continue
            post_date = m.group(2)
            desc      = m.group(3).strip()
            amount    = Decimal(m.group(4).replace(',', ''))
            is_cr     = bool(m.group(5))

            if 'PAYMENT' in desc.upper():
                self.payments.append({'date': post_date, 'description': desc, 'amount': amount})
                self.total_payments += amount
            elif is_cr:
                self.credits.append({'date': post_date, 'description': desc, 'amount': amount})
            else:
                self.charges.append({'date': post_date, 'vendor': desc, 'amount': amount})
        self._normalize_dates()

    def generate_report(self, check_payee_map=None, check_date_map=None):
        def norm(v):
            return _registry.normalize_vendor(self.client_name or '', v)

        def agg(txns, key='vendor'):
            totals = defaultdict(lambda: {'amount': Decimal('0'), 'count': 0, 'date': ''})
            for t in txns:
                v = norm(t.get(key) or t.get('description', ''))
                totals[v]['amount'] += Decimal(str(t['amount']))
                totals[v]['count']  += 1
                totals[v]['date']    = t['date']
            return sorted(
                [{'date': d['date'], 'vendor': v, 'amount': d['amount'], 'count': d['count']}
                 for v, d in totals.items()],
                key=lambda x: _safe_date_key(x['date'])
            )

        agg_charges    = agg(self.charges, 'vendor')
        total_charges  = sum(r['amount'] for r in agg_charges)
        total_payments = sum(Decimal(str(p['amount'])) for p in self.payments)
        total_credits  = sum(Decimal(str(c['amount'])) for c in self.credits)

        report  = _report_header(self.statement_type, self.client_name,
                                  statement_date=self.statement_period)
        report += _summary_block([
            ('Previous Balance', self.previous_balance),
            ('Payments',         total_payments),
            ('Credits / Returns', total_credits if total_credits else None),
            ('Charges',          total_charges),
            ('New Balance',      self.new_balance),
        ])

        if self.previous_balance is not None and self.new_balance is not None:
            calc = self.previous_balance - total_payments - total_credits + total_charges
            ok   = abs(calc - self.new_balance) < Decimal('0.05')
            report += _balance_check(ok, calc)

        if self.payments:
            pmts = [{'date': p['date'],
                     'description': p.get('description', 'PAYMENT - THANK YOU'),
                     'amount': Decimal(str(p['amount']))} for p in self.payments]
            report += _payments_section(pmts, total_payments)

        if self.credits:
            crds = [{'date': c['date'],
                     'description': norm(c.get('description', '')),
                     'amount': Decimal(str(c['amount']))} for c in self.credits]
            report += _credits_section(crds, total_credits)

        if agg_charges:
            report += _charges_section(agg_charges, total_charges)

        return report

