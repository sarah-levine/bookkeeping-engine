import sys
import re
import csv
import json
import os
from pathlib import Path
from decimal import Decimal
from zoneinfo import ZoneInfo
from datetime import datetime

def _now_pst():
    """Return current datetime in US/Pacific (PST/PDT)."""
    return datetime.now(ZoneInfo('America/Los_Angeles'))

import pdfplumber

from payroll_clients.base import (
    extract_text, amt, parse_header, verify_same_check_date,
    make_row, check_balance, print_journal_table, write_csv, write_iif,
    _upsert_csv, append_payroll_log, append_digest_log, append_recon_log,
    load_config, _qb_confirm, _now_pst,
    REPO_DIR, PAYROLL_LOG_PATH, RECON_LOG_PATH, PAYROLL_LOG_FIELDS, RECON_LOG_FIELDS,
)

def parse_check_date(text: str) -> str:
    norm = re.sub(r'\s+', '', text)
    m = re.search(r'Checkdatesfrom:(\d+/\d+/\d+)', norm)
    if m: return m.group(1)
    m = re.search(r'Checkdate:(\d+/\d+/\d+)', norm)
    return m.group(1) if m else ""


def parse_dept_gross(text: str) -> dict:
    """Grab department gross totals from the subtotal line before TotalEmployees-XXX."""
    lines = text.split('\n')
    dept_gross, current_dept = {}, None
    for i, line in enumerate(lines):
        mn = re.sub(r'\s+', '', line)
        m = re.match(r'DepartmentTotals?:(\d+)', mn)
        if m:
            current_dept = m.group(1); continue
        if current_dept and re.match(r'TotalEmployees-', mn):
            for j in range(i - 1, max(i - 12, 0), -1):
                prev = lines[j].strip()
                m2 = re.match(r'^[\d,]+\.?\d*\s+\$([\d,]+\.\d{2})', prev)
                if m2:
                    dept_gross[current_dept] = amt(m2.group(1)); break
            current_dept = None
    return dept_gross


def parse_individual_checks(text: str) -> tuple:
    """Returns (checks list, dd_total float)."""
    lines = text.split('\n')
    checks, dd_total = [], 0.0
    for i, line in enumerate(lines):
        norm = re.sub(r'\s+', '', line)
        m = re.match(r'CheckDate:\d+/\d+/\d+/Check/CheckNo:(\d+)\$([0-9,]+\.\d{2})', norm)
        if m:
            check_no  = m.group(1)
            check_amt = amt(m.group(2))
            employee  = ""
            for j in range(i - 1, max(i - 25, 0), -1):
                prev = re.sub(r'\s+', '', lines[j])
                em = re.match(r'Employee:(.+?)(?:SSN:|TIN:)', prev)
                if em:
                    raw   = em.group(1).strip()
                    parts = raw.split(',')
                    if len(parts) >= 2:
                        first_raw = parts[1].strip()
                        words = re.findall(r'[A-Z][a-z]*', first_raw)
                        if words:
                            if len(words[-1]) == 1 and len(words) > 1:
                                words = words[:-1]
                            first = words[0]
                        else:
                            first = first_raw
                        employee = f"{first} {parts[0].strip()}"
                    else:
                        employee = raw
                    break
            checks.append({"check_no": check_no, "amount": check_amt, "employee": employee})
        m2 = re.match(r'CheckDate:\d+/\d+/\d+/DirectDeposit/Checking/AccountNo:(\S+)\$([0-9,]+\.\d{2})', norm)
        if m2:
            dd_total += amt(m2.group(2))
    return checks, round(dd_total, 2)


def parse_company_totals(text: str) -> dict:
    norm  = re.sub(r'\s+', '', text)
    idx   = norm.find('PayFrequencyTotals:Biweekly')
    block = norm[idx:idx + 700] if idx >= 0 else norm
    totals = {}
    m = re.search(r'Medicalpre-tax1\$([\d,]+\.\d{2})', block)
    totals['medical_pretax'] = amt(m.group(1)) if m else 0.0
    m = re.search(r'CalSaversRoth\$([\d,]+\.\d{2})', block)
    totals['calsavers'] = amt(m.group(1)) if m else 0.0
    er = 0.0
    for tag in ['FEDSOCSEC-ER', 'FEDMEDCARE-ER', 'FEDFUTA', 'CASUI-ER']:
        m = re.search(rf'{tag}\$([\d,]+\.\d{{2}})', block)
        if m: er += amt(m.group(1))
    totals['er_taxes'] = round(er, 2)
    return totals


def parse_cash_splits(liability_text: str) -> dict:
    norm = re.sub(r'\s+', '', liability_text)
    result = {}
    m = re.search(r'DebitforTaxes[^$]*\$([\d,]+\.\d{2})', norm)
    if m: result['nettax'] = amt(m.group(1))
    m = re.search(r'DebitforPay-by-Pay[^$]*\$([\d,]+\.\d{2})', norm)
    if m: result['pay_by_pay'] = amt(m.group(1))
    return result


def run_adp_payroll_departments(args, config_name):
    if len(args) < 2:
        print("Usage: python payroll.py adp_payroll_departments <payroll_details.pdf> <payroll_liability.pdf> --config <client.json>")
        sys.exit(1)

    # Verify both PDFs are from the same payroll period
    verify_same_check_date({
        "Payroll Details":   args[0],
        "Payroll Liability": args[1],
    })

    cfg            = load_config(config_name)
    text           = extract_text(args[0])
    liability_text = extract_text(args[1])

    check_date       = parse_check_date(text)
    dept             = parse_dept_gross(text)
    co               = parse_company_totals(text)
    cash             = parse_cash_splits(liability_text)
    checks, dd_total = parse_individual_checks(text)

    service  = dept.get('100', 0)
    office   = dept.get('400', 0)
    officers = dept.get('600', 0)
    svw      = dept.get('700', 0)
    er_taxes       = co.get('er_taxes', 0)
    medical_pretax = co.get('medical_pretax', 0)
    calsavers      = co.get('calsavers', 0)
    nettax         = cash.get('nettax', 0)
    wc             = cash.get('pay_by_pay', 0)
    wc_account     = cfg.get('workers_comp_account') or cfg.get('pay_by_pay_account')

    depts = cfg["departments"]
    rows  = []

    # DEBITS
    rows.append(make_row(check_date, depts["100"]["regular_account"], debit=service))
    rows.append(make_row(check_date, depts["400"]["regular_account"], debit=office))
    rows.append(make_row(check_date, depts["600"]["gross_account"],   debit=officers))
    rows.append(make_row(check_date, cfg["employer_tax_account"],     debit=er_taxes))
    rows.append(make_row(check_date, depts["700"]["gross_account"],   debit=svw, memo=depts["700"].get("contractor_name", "")))
    if wc > 0 and wc_account:
        rows.append(make_row(check_date, wc_account, debit=wc, memo="ADP Pay-by-Pay (Workers Comp)"))
    elif wc > 0:
        print(f"⚠️  Pay-by-Pay ${wc:,.2f} found in Liability PDF but no workers_comp_account in config — not included in JE")

    # CREDITS
    rows.append(make_row(check_date, cfg["health_insurance_account"], credit=medical_pretax, memo="Medical pre-tax 1"))
    for c in checks:
        rows.append(make_row(check_date, cfg["bank_account"], credit=c["amount"], memo=f"Check {c['check_no']} - {c['employee']}"))
    if dd_total > 0:
        rows.append(make_row(check_date, cfg["bank_account"], credit=dd_total, memo="Direct Deposit"))
    rows.append(make_row(check_date, cfg["bank_account"], credit=nettax, memo="NETTAX"))
    if calsavers > 0:
        rows.append(make_row(check_date, cfg["bank_account"], credit=calsavers, memo="CalSavers Roth"))
    if wc > 0 and wc_account:
        rows.append(make_row(check_date, cfg["bank_account"], credit=wc, memo="ADP Pay-by-Pay (Workers Comp)"))

    total_d, total_c = check_balance(rows)
    if abs(total_d - total_c) > 0.01:
        print(f"⚠️  JE out of balance: debits ${total_d:,.2f} vs credits ${total_c:,.2f}")

    print_journal_table(rows, cfg["client_name"], check_date)
    if _qb_confirm(cfg["client_name"]):
        append_payroll_log("adp_payroll_departments", cfg["client_name"], check_date, rows)
        append_digest_log(cfg["client_name"], check_date)



