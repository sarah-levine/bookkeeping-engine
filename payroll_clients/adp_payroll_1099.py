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

def parse_employees(text: str) -> list:
    """
    Returns [{name, amount, dept, payment_method, check_no}, ...]
    1099 contractor-only payroll — no W-2 taxes, no deductions.
    """
    lines = text.split('\n')
    employees, current_dept, current_emp, current_amount = [], None, None, None

    for line in lines:
        dept_m = re.match(r'Department:(\d+)-', line)
        if dept_m and 'DepartmentTotals' not in line:
            current_dept = dept_m.group(1); continue

        emp_m = re.match(r'Employee:(.+?)(?:SSN:|TIN:)', line)
        if emp_m:
            current_emp    = emp_m.group(1).strip()
            current_amount = None; continue

        if current_emp and re.match(r'1099\s+', line):
            parts   = line.split()
            amounts = []
            for p in parts[1:]:
                try: amounts.append(float(p.replace(',', '')))
                except ValueError: pass
            if amounts: current_amount = max(amounts)
            continue

        check_m = re.search(
            r'CheckDate:(\d+/\d+/\d+)/(DirectDeposit|Check)/(?:Checking|Savings)?/?'
            r'(?:AccountNo:\S+|CheckNo:(\S+))\s+\$(\S+)', line
        )
        if check_m and current_emp:
            date, method, check_no, net = check_m.groups()
            employees.append({
                "name":           current_emp,
                "amount":         amt(net),
                "dept":           current_dept,
                "payment_method": method,
                "check_no":       check_no if method == "Check" else None,
            })
            current_emp = current_amount = None

    return employees


# ── Phase 3c: config-driven special-employee handling ──────────────────────
# Special employees (owner draws, contractor splits, etc.) are matched and
# rendered entirely from the client JSON's "special_employees" block:
#   "<key>": {
#     "match": {"dept": "001", "name_contains": "Doe"},
#     "display_name": "Jane Doe",
#     "placement": "before_admin" | "after_admin",   # vs the regular-admin block
#     "account": "<acct>"                   # full amount to one account, OR
#     "splits":  [{"account": "<acct>", "amount": 750.0 | "remainder"}, ...]
#   }
# Name matching ignores spaces, so "Acme L L C" matches "AcmeLLC".

def _emp_matches_special(emp: dict, entry: dict) -> bool:
    """True if emp satisfies a special_employees entry's match rule."""
    m = entry.get("match")
    if not m:
        return False
    if "dept" in m and emp.get("dept") != m["dept"]:
        return False
    nc = m.get("name_contains")
    if nc and nc.replace(" ", "") not in emp.get("name", "").replace(" ", ""):
        return False
    return ("dept" in m) or bool(nc)


def _is_special(emp: dict, specials: dict) -> bool:
    return any(_emp_matches_special(emp, e) for e in specials.values())


def _emit_special(emp: dict, entry: dict, check_date: str, admin_acct: str) -> list:
    """Render journal rows for one matched special employee, from config.
    'remainder' in a split resolves to emp amount minus the fixed splits."""
    name = entry.get("display_name") or emp["name"].replace(",", ", ")
    if "account" in entry:
        return [make_row(check_date, entry["account"], debit=emp["amount"], name=name)]
    splits = entry.get("splits")
    if splits:
        fixed = sum(s["amount"] for s in splits if s["amount"] != "remainder")
        remainder = round(emp["amount"] - fixed, 2)
        out = []
        for s in splits:
            value = remainder if s["amount"] == "remainder" else s["amount"]
            out.append(make_row(check_date, s["account"], debit=value, name=name))
        return out
    return [make_row(check_date, admin_acct, debit=emp["amount"], name=name)]


def _build_journal(cfg: dict, employees: list, check_date: str, warnings: list,
                   pay_by_pay: float = 0.0) -> list:
    rows       = []
    specials   = cfg.get("special_employees", {})
    coaches_acct = cfg["coaches_account"]
    admin_acct = cfg["admin_default_account"]
    bank_acct  = cfg["bank_account"]
    wc_account = cfg.get('workers_comp_account') or cfg.get('pay_by_pay_account')
    total_net  = sum(e["amount"] for e in employees)

    if pay_by_pay > 0 and wc_account:
        rows.append(make_row(check_date, wc_account, debit=pay_by_pay, memo="ADP Pay-by-Pay (Workers Comp)"))
    elif pay_by_pay > 0:
        warnings.append(f"⚠️  Pay-by-Pay ${pay_by_pay:,.2f} provided but no workers_comp_account in config — not included in JE")

    # 1. Check-paid Dept-001 lines first (map to the admin account)
    for emp in employees:
        if emp["dept"] == "001" and emp["payment_method"] == "Check":
            name_disp = emp["name"].replace(",", ", ")
            chk = f"Check {emp['check_no']}" if emp["check_no"] else "Check"
            rows.append(make_row(check_date, admin_acct, debit=emp["amount"],
                                 name=name_disp, memo=f"{name_disp} ({chk})"))

    # 2. Coaches rollup (Dept 003)
    coaches_total = sum(e["amount"] for e in employees if e["dept"] == "003")
    if coaches_total > 0:
        rows.append(make_row(check_date, coaches_acct, debit=coaches_total))

    def _emit_placement(placement):
        for entry in specials.values():
            if entry.get("placement", "after_admin") != placement:
                continue
            for emp in employees:
                if _emp_matches_special(emp, entry):
                    rows.extend(_emit_special(emp, entry, check_date, admin_acct))

    # 3. Special employees placed before the regular-admin block
    _emit_placement("before_admin")

    # 4. Regular admin individuals (skip specials), sorted by name
    admin_regular = [
        e for e in employees
        if e["dept"] == "001"
        and e["payment_method"] != "Check"
        and not _is_special(e, specials)
    ]
    for emp in sorted(admin_regular, key=lambda e: e["name"]):
        rows.append(make_row(check_date, admin_acct, debit=emp["amount"],
                             name=emp["name"].replace(",", ", ")))

    # 5. Special employees placed after the regular-admin block
    _emit_placement("after_admin")

    # 6. Bank credit(s) — one AMEX credit per check, plus the FSDD remainder
    check_total = round(sum(e["amount"] for e in employees
                            if e["dept"] == "001" and e["payment_method"] == "Check"), 2)
    if check_total > 0:
        for emp in employees:
            if emp["dept"] == "001" and emp["payment_method"] == "Check":
                name_disp = emp["name"].replace(",", ", ")
                chk = f"Check {emp['check_no']}" if emp["check_no"] else "Check"
                rows.append(make_row(check_date, bank_acct, credit=emp["amount"],
                                     name=name_disp, memo=f"{name_disp} ({chk})"))
        rows.append(make_row(check_date, bank_acct, credit=round(total_net - check_total, 2),
                             memo="FSDD direct deposit"))
    else:
        rows.append(make_row(check_date, bank_acct, credit=total_net))
    if pay_by_pay > 0 and wc_account:
        rows.append(make_row(check_date, bank_acct, credit=pay_by_pay, memo="ADP Pay-by-Pay (Workers Comp)"))

    return rows


def run_adp_payroll_1099(args, config_name):
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
        print("Usage: python payroll.py adp_payroll_1099 <payroll.pdf> --config <client.json> [--pay-by-pay AMOUNT]")
        sys.exit(1)

    pdf_path   = args[0]
    cfg        = load_config(config_name)

    print(f"Client:  {cfg['client_name']}")
    print(f"PDF:     {pdf_path}")

    text       = extract_text(pdf_path)
    header     = parse_header(text)
    check_date = header.get("check_date", "")
    print(f"Check Date: {check_date}")
    print(f"Pay Period: {header.get('pay_period_start')} to {header.get('pay_period_end')}")

    employees = parse_employees(text)
    print(f"\nEmployees found: {len(employees)}")
    dept_totals = {}
    for e in employees:
        dept_totals[e['dept']] = dept_totals.get(e['dept'], 0) + e['amount']
    for dept, total in sorted(dept_totals.items()):
        print(f"  Dept {dept}: ${total:,.2f}")
    print(f"  TOTAL: ${sum(dept_totals.values()):,.2f}")

    warnings = []
    rows = _build_journal(cfg, employees, check_date, warnings, pay_by_pay=pay_by_pay)

    if warnings:
        print()
        for w in warnings: print(w)

    if pay_by_pay > 0:
        print(f"  Pay-by-Pay:     ${pay_by_pay:,.2f}")
    total_d, total_c = check_balance(rows)
    if abs(total_d - total_c) > 0.01:
        print(f"⚠️  JE out of balance: debits ${total_d:,.2f} vs credits ${total_c:,.2f}")

    print_journal_table(rows, cfg["client_name"], check_date)
    if _qb_confirm(cfg["client_name"]):
        append_payroll_log("adp_payroll_1099", cfg["client_name"], check_date, rows)
        append_digest_log(cfg["client_name"], check_date)
        archive_payroll_pdf(pdf_path, cfg["client_name"], check_date)


# ═══════════════════════════════════════════════════════════════════════════
# ADP PAYROLL — 1099 FORMAT
# ═══════════════════════════════════════════════════════════════════════════

