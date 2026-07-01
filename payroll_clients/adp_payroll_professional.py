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
    load_config, _qb_confirm, _now_pst, archive_payroll_pdf,
    REPO_DIR, PAYROLL_LOG_PATH, RECON_LOG_PATH, PAYROLL_LOG_FIELDS, RECON_LOG_FIELDS,
)

def parse_officers(lines: list) -> list:
    """Returns [{first_name, gross}, ...] from Dept 001."""
    employees, in_dept, current_name = [], False, None
    for line in lines:
        if "Department:001" in line and "DepartmentTotals" not in line:
            in_dept = True; continue
        if "DepartmentTotals:001" in line: break
        if not in_dept: continue
        emp_m = re.match(r'Employee:([^S]+)SSN:', line)
        if emp_m:
            full  = emp_m.group(1).strip()
            parts = full.split(",")
            if len(parts) >= 2:
                words = re.findall(r'[A-Z][a-z]*', parts[1].strip())
                if words:
                    if len(words[-1]) == 1 and len(words) > 1:
                        words = words[:-1]
                    first = words[-1]
                else:
                    first = parts[1].strip()
            else:
                first = full
            current_name = first
            continue
        if current_name and re.match(r'Regular\s+', line):
            parts = line.split()
            for i, p in enumerate(parts):
                if p == "Regular" and i + 1 < len(parts):
                    candidates = []
                    for j in range(i + 1, len(parts)):
                        v = re.sub(r'[$,]', '', parts[j])
                        try: candidates.append(float(v))
                        except ValueError: break
                    if candidates:
                        gross = max(candidates)
                        if gross > 0:
                            employees.append({"first_name": current_name, "gross": gross})
                        current_name = None
                    break
    return employees


def parse_admin(lines: list) -> dict:
    """Returns {regular, overtime, holiday, sick, travel} from DepartmentTotals:002.

    Note: 'sick' is treated as gross wages (same account as Regular), matching
    how ADP rolls it up on the PDF.
    """
    totals = {"regular": 0.0, "overtime": 0.0, "holiday": 0.0,
              "sick": 0.0, "travel": 0.0}
    in_totals = False
    for line in lines:
        if "DepartmentTotals:002" in line:
            in_totals = True; continue
        if in_totals and "TotalEmployees-002" in line: break
        if not in_totals: continue
        for key, pattern in [
            ("regular",  r'Regular\s+[\d.]+\s+(\$[\d,]+\.\d+)'),
            ("overtime", r'Overtime\s+[\d.]+\s+(\$[\d,]+\.\d+)'),
            ("holiday",  r'Holiday\s+[\d.]+\s+(\$[\d,]+\.\d+)'),
            ("sick",     r'Sick\s+[\d.]+\s+(\$[\d,]+\.\d+)'),
            ("travel",   r'Travel\s+[\d.]+\s+(\$[\d,]+\.\d+)'),
        ]:
            m = re.search(pattern, line)
            if m: totals[key] = amt(m.group(1))
    return totals


def parse_1099(lines: list) -> float:
    """Returns total 1099 reimbursement from Dept 005."""
    in_dept = False
    for line in lines:
        if "Department:005" in line and "DepartmentTotals" not in line:
            in_dept = True; continue
        if "DepartmentTotals:005" in line: break
        if not in_dept: continue
        m = re.match(r'1099\s+[\d.]+\s+([\d,]+\.\d+)', line)
        if m: return amt(m.group(1))
    return 0.0


def parse_company_totals(lines: list) -> dict:
    """Parse Pay Frequency Totals block for net pay, taxes, 401k."""
    totals = {
        "net_pay": 0.0, "total_taxes": 0.0, "deductions_401k": 0.0,
        "employer_taxes": 0.0, "employer_401k_match": 0.0, "loan_deductions": 0.0,
    }
    in_totals, collected = False, []
    for line in lines:
        if "PayFrequencyTotals:Biweekly" in line:
            in_totals = True; continue
        if in_totals and "TotalEmployees-Biweekly" in line: break
        if in_totals: collected.append(line)

    block = " ".join(collected)

    m = re.search(r'\$([\d,]+\.\d{2})\s+FEDSOCSEC-ER', block)
    if m: totals["net_pay"] = amt(m.group(1))

    # Employee taxes
    casdi_idx = block.rfind("CASDI")
    if casdi_idx >= 0:
        post  = block[casdi_idx:]
        large = [amt(a) for a in re.findall(r'\$([\d,]+\.\d{2})', post) if amt(a) > 1000]
        if len(large) >= 2: totals["total_taxes"] = large[-2]
        elif len(large) == 1: totals["total_taxes"] = large[0]
    if totals["total_taxes"] == 0.0:
        emp = 0.0
        for t in ["FEDFIT", "FEDSOCSEC", "FEDMEDCARE", "CASIT", "CASDI"]:
            m = re.search(rf'{t}\s+\$([\d,]+\.\d{{2}})', block)
            if m: emp += amt(m.group(1))
        totals["total_taxes"] = round(emp, 2)

    # Employee 401k
    emp_401k = 0.0
    for pattern in [r'401\(k\)plan\$\s+\$([\d,]+\.\d{2})',
                    r'401\(k\)plan%\s+\$([\d,]+\.\d{2})',
                    r'Roth401\(k\)plan\s+\$([\d,]+\.\d{2})']:
        m = re.search(pattern, block)
        if m: emp_401k += amt(m.group(1))
    totals["deductions_401k"] = round(emp_401k, 2)

    # Loan deductions
    m = re.search(r'Loan\s+\$([\d,]+\.\d{2})', block)
    if m: totals["loan_deductions"] = amt(m.group(1))

    # Employer taxes
    er = 0.0
    for key in ["FEDSOCSEC-ER", "FEDMEDCARE-ER", "FEDFUTA", "CASUI-ER"]:
        m = re.search(rf'{key}\s+\$([\d,]+\.\d{{2}})', block)
        if m: er += amt(m.group(1))
    totals["employer_taxes"] = round(er, 2)

    # Employer 401k match
    er_401k = 0.0
    for pattern in [r'401k\$plancmpmtch\s+\$([\d,]+\.\d{2})',
                    r'401k%plancmpmtch\s+\$([\d,]+\.\d{2})',
                    r'Roth401\(k\)%plan\s+\$([\d,]+\.\d{2})']:
        m = re.search(pattern, block)
        if m: er_401k += amt(m.group(1))
    totals["employer_401k_match"] = round(er_401k, 2)

    return totals


def _build_journal(cfg: dict, officers: list, admin: dict,
                   total_1099: float, totals: dict, check_date: str,
                   pay_by_pay: float = 0.0) -> list:
    rows       = []
    dept_cfg   = cfg["departments"]
    wc_account = cfg.get('workers_comp_account') or cfg.get('pay_by_pay_account')

    # DEBITS
    officers_cfg = dept_cfg.get("001", {})
    for emp in officers:
        rows.append(make_row(check_date, officers_cfg["gross_account"],
                             debit=emp["gross"], memo=emp["first_name"]))

    admin_cfg = dept_cfg.get("002", {})
    if admin["regular"]  > 0: rows.append(make_row(check_date, admin_cfg["regular_account"],  debit=admin["regular"]))
    if admin["overtime"] > 0: rows.append(make_row(check_date, admin_cfg["overtime_account"], debit=admin["overtime"]))
    if admin.get("holiday", 0) > 0:
        rows.append(make_row(check_date, admin_cfg.get("holiday_account", admin_cfg["regular_account"]),
                             debit=admin["holiday"], memo="Holiday"))
    if admin.get("sick", 0) > 0:
        rows.append(make_row(check_date, admin_cfg.get("sick_account", admin_cfg["regular_account"]),
                             debit=admin["sick"], memo="Sick"))
    if admin["travel"] > 0:
        rows.append(make_row(check_date, admin_cfg["travel_account"], debit=admin["travel"], memo="Travel"))

    dept_1099_cfg = dept_cfg.get("005", {})
    if total_1099 > 0:
        base_fee  = dept_1099_cfg.get("base_fee", 45.00)
        remainder = round(total_1099 - base_fee, 2)
        rows.append(make_row(check_date, dept_1099_cfg["base_fee_account"], debit=base_fee))
        if remainder > 0:
            rows.append(make_row(check_date, dept_1099_cfg["remainder_account"],
                                 debit=remainder, name=dept_1099_cfg.get("display_name", "")))

    if totals["employer_taxes"]      > 0: rows.append(make_row(check_date, cfg["employer_tax_account"],  debit=totals["employer_taxes"]))
    if totals["employer_401k_match"] > 0: rows.append(make_row(check_date, cfg["employer_401k_account"], debit=totals["employer_401k_match"]))
    if pay_by_pay > 0 and wc_account:
        rows.append(make_row(check_date, wc_account, debit=pay_by_pay, memo="ADP Pay-by-Pay (Workers Comp)"))
    elif pay_by_pay > 0:
        print(f"⚠️  Pay-by-Pay ${pay_by_pay:,.2f} provided but no workers_comp_account in config — not included in JE")

    # CREDITS
    total_401k = round(totals["deductions_401k"] + totals["employer_401k_match"], 2)
    if total_401k > 0:
        rows.append(make_row(check_date, cfg["payable_401k_account"], credit=total_401k))
    if totals["net_pay"] > 0:
        rows.append(make_row(check_date, cfg["bank_account"], credit=totals["net_pay"], memo="NET PAY"))
    if totals.get("loan_deductions", 0) > 0:
        rows.append(make_row(check_date, cfg.get("loan_payable_account", "2200 · Current Liabilities · Loan Payable"),
                             credit=totals["loan_deductions"]))
    net_taxes = round(totals["total_taxes"] + totals["employer_taxes"], 2)
    if net_taxes > 0:
        rows.append(make_row(check_date, cfg["bank_account"], credit=net_taxes, memo="NET TAXES"))
    if pay_by_pay > 0 and wc_account:
        rows.append(make_row(check_date, cfg["bank_account"], credit=pay_by_pay, memo="ADP Pay-by-Pay (Workers Comp)"))

    return rows


def run_adp_payroll_professional(args, config_name):
    pay_by_pay, filtered = 0.0, []
    it = iter(args)
    for a in it:
        if a == '--pay-by-pay':
            try: pay_by_pay = float(next(it, '0').replace(',', ''))
            except ValueError: pass
        else:
            filtered.append(a)
    args = filtered

    if len(args) < 1:
        print("Usage: python payroll.py adp_payroll_professional <payroll.pdf> --config <client.json> [--pay-by-pay AMOUNT]")
        sys.exit(1)

    pdf_path   = args[0]
    cfg        = load_config(config_name)

    print(f"Client:  {cfg['client_name']}")
    print(f"PDF:     {pdf_path}")

    text       = extract_text(pdf_path)
    lines      = text.split("\n")
    header     = parse_header(text)
    check_date = header.get("check_date", "")
    print(f"Check Date: {check_date}")
    print(f"Pay Period: {header.get('pay_period_start')} to {header.get('pay_period_end')}")

    officers   = parse_officers(lines)
    admin      = parse_admin(lines)
    total_1099 = parse_1099(lines)
    totals     = parse_company_totals(lines)

    print(f"\n--- Parsed Values ---")
    print(f"  Officers: {len(officers)} employee(s)")
    for o in officers:
        print(f"    {o['first_name']}: ${o['gross']:,.2f}")
    print(f"  Admin regular:  ${admin['regular']:,.2f}")
    print(f"  Admin overtime: ${admin['overtime']:,.2f}")
    if admin.get('holiday', 0) > 0:
        print(f"  Admin holiday:  ${admin['holiday']:,.2f}")
    if admin.get('sick', 0) > 0:
        print(f"  Admin sick:     ${admin['sick']:,.2f}")
    print(f"  Admin travel:   ${admin['travel']:,.2f}")
    print(f"  1099:           ${total_1099:,.2f}")
    print(f"  Net pay:        ${totals['net_pay']:,.2f}")
    print(f"  Emp taxes:      ${totals['total_taxes']:,.2f}")
    print(f"  Emp 401k:       ${totals['deductions_401k']:,.2f}")
    print(f"  ER taxes:       ${totals['employer_taxes']:,.2f}")
    print(f"  ER 401k match:  ${totals['employer_401k_match']:,.2f}")

    rows = _build_journal(cfg, officers, admin, total_1099, totals, check_date, pay_by_pay=pay_by_pay)
    if pay_by_pay > 0:
        print(f"  Pay-by-Pay:     ${pay_by_pay:,.2f}")
    total_d, total_c = check_balance(rows)
    if abs(total_d - total_c) > 0.01:
        print(f"⚠️  JE out of balance: debits ${total_d:,.2f} vs credits ${total_c:,.2f}")
    print_journal_table(rows, cfg["client_name"], check_date)
    if _qb_confirm(cfg["client_name"]):
        append_payroll_log("adp_payroll_professional", cfg["client_name"], check_date, rows)
        append_digest_log(cfg["client_name"], check_date)
        archive_payroll_pdf(pdf_path, cfg["client_name"], check_date)



