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

def parse_header(text: str) -> dict:
    header = {}
    m = re.search(r"Check\s*[Dd]ate:\s*(\d+/\d+/\d+)", text)
    if m: header["check_date"] = m.group(1)
    m = re.search(r"PayPeriod:(\S+?)to:(\S+)", text)
    if m:
        header["pay_period_start"] = m.group(1)
        header["pay_period_end"]   = m.group(2)
    return header


def parse_officers(lines: list) -> dict:
    result = {"employees": [], "scorp_medical": 0.0}
    in_dept, current_emp = False, None
    for line in lines:
        if re.search(r"Department:001", line) and "Totals" not in line:
            in_dept = True; continue
        if re.search(r"DepartmentTotals:001|TotalEmployees-001", line):
            in_dept = False; continue
        if not in_dept: continue
        emp_m = re.search(r"Employee:(\w+),(\w+)", line)
        if emp_m:
            current_emp = emp_m.group(2)
            continue
        if current_emp:
            m = re.match(r"Regular\s+[\d.]+\s+([\d,]+\.\d{2})", line)
            if m:
                result["employees"].append({"name": current_emp, "gross": amt(m.group(1))})
            m = re.match(r"S-corp2%medical\s+[\d.]+\s+([\d,]+\.\d{2})", line)
            if m:
                result["scorp_medical"] += amt(m.group(1))
    return result


def parse_support(lines: list) -> dict:
    result = {"regular": 0.0, "overtime": 0.0, "tips": 0.0}
    in_totals = False
    for line in lines:
        if re.search(r"DepartmentTotals:002", line):
            in_totals = True; continue
        if re.search(r"TotalEmployees-002", line): break
        if not in_totals: continue
        m = re.match(r"Regular\s+[\d.]+\s+\$([\d,]+\.\d{2})", line)
        if m: result["regular"] = amt(m.group(1))
        m = re.match(r"Overtime\s+[\d.]+\s+\$([\d,]+\.\d{2})", line)
        if m: result["overtime"] = amt(m.group(1))
        m = re.match(r"Creditcardtips\s+[\d.]+\s+\$([\d,]+\.\d{2})", line)
        if m: result["tips"] = amt(m.group(1))
    return result


def parse_company_totals(text: str) -> dict:
    totals = {"net_pay": 0.0, "total_taxes": 0.0, "employer_taxes": 0.0, "scorp_medical": 0.0}
    m = re.search(r"CompanyTotals:(.*?)TotalEmployees-Company", text, re.DOTALL)
    if not m:
        m = re.search(r"PayFrequencyTotals:Semimonthly(.*?)TotalEmployees-Semimonthly", text, re.DOTALL)
    if not m:
        return totals
    block = m.group(1)

    net_m = re.search(r"\$([\d,]+\.\d{2})\s*FEDSOCSEC-ER", block)
    if net_m: totals["net_pay"] = amt(net_m.group(1))

    # Total employee taxes: sum the individual tax components. ADP's PDF layout
    # wraps tax labels unpredictably (Medicare especially — "FED $XX.XX" on one
    # line, "MEDCARE" on a later line) and the position of the standalone
    # subtotal varies depending on which taxes are present. Parsing at the
    # token level on whitespace-normalized text is robust to all of this.
    norm = re.sub(r'\s+', '', block)
    individual_taxes = 0.0
    # FEDFIT and FEDSOCSEC are straightforward token-level matches.
    # Negative lookahead on FEDSOCSEC prevents matching FEDSOCSEC-ER.
    for pat in [r"FEDFIT\$([\d,]+\.\d{2})",
                r"FEDSOCSEC(?!-ER)\$([\d,]+\.\d{2})"]:
        tm = re.search(pat, norm)
        if tm: individual_taxes += amt(tm.group(1))
    # Medicare: "FED$XX.XX" where the FED token is standalone (not FEDSOCSEC,
    # FEDMEDCARE-ER, FEDFUTA, FEDFIT). Use negative lookahead after FED.
    # The MEDCARE label appears somewhere later but its position varies.
    med_m = re.search(r"(?<![A-Z])FED(?![A-Z])\$([\d,]+\.\d{2})", norm)
    if med_m: individual_taxes += amt(med_m.group(1))
    # State taxes
    for pat in [r"CASIT\$([\d,]+\.\d{2})",
                r"CASDI\$([\d,]+\.\d{2})"]:
        tm = re.search(pat, norm)
        if tm: individual_taxes += amt(tm.group(1))
    totals["total_taxes"] = round(individual_taxes, 2)

    er = 0.0
    for key in ["FEDSOCSEC-ER", "FEDMEDCARE-ER", "FEDFUTA", "CASUI-ER"]:
        em = re.search(rf"{key}\s+\$([\d,]+\.\d{{2}})", block)
        if em: er += amt(em.group(1))
    totals["employer_taxes"] = round(er, 2)

    scorp_m = re.search(r"S-corp2%medical\s+[\d.]+\s+\$([\d,]+\.\d{2})", block)
    if scorp_m: totals["scorp_medical"] = amt(scorp_m.group(1))

    return totals


def parse_1099(lines: list) -> float:
    in_dept = False
    for line in lines:
        if re.search(r"Department:010", line) and "Totals" not in line:
            in_dept = True; continue
        if re.search(r"DepartmentTotals:010", line): break
        if not in_dept: continue
        m = re.match(r"1099\s+[\d.]+\s+([\d,]+\.\d{2})", line)
        if m: return amt(m.group(1))
    return 0.0


def _build_journal(cfg: dict, officers: dict, support: dict, company: dict,
                   total_1099: float, check_date: str) -> list:
    rows = []
    dept = cfg["departments"]

    officers_gross = sum(e["gross"] for e in officers["employees"])
    scorp_medical  = company["scorp_medical"]
    net_pay        = company["net_pay"]
    total_taxes    = company["total_taxes"]
    employer_taxes = company["employer_taxes"]
    wc_credit      = cfg.get("workers_comp_credit", 60.00)
    wc_refund      = cfg.get("workers_comp_refund", 22.55)
    wc_net_credit  = round(wc_credit - wc_refund, 2)

    # DEBITS
    rows.append(make_row(check_date, dept["001"]["gross_account"], debit=officers_gross))
    if scorp_medical > 0:
        rows.append(make_row(check_date, dept["001"]["gross_account"], debit=scorp_medical, memo="2%"))
    if support["regular"] > 0:
        rows.append(make_row(check_date, dept["002"]["regular_account"], debit=support["regular"]))
    if support["overtime"] > 0:
        rows.append(make_row(check_date, dept["002"]["overtime_account"], debit=support["overtime"], memo="OT"))
    if support["tips"] > 0:
        rows.append(make_row(check_date, dept["002"]["tips_account"], debit=support["tips"], memo="credit card tips"))
    if total_1099 == 0:
        pass  # No contractor this run; omit placeholder rows
    rows.append(make_row(check_date, cfg["employer_tax_account"], debit=employer_taxes))
    if total_1099 > 0:
        acct_tax_amount    = cfg.get("contractor_accounting_amount", 900.00)
        payroll_remainder  = round(total_1099 - acct_tax_amount, 2)
        contractor_name    = cfg.get("contractor_display_name", "")
        rows.append(make_row(check_date, cfg["accounting_tax_account"], debit=acct_tax_amount, memo=contractor_name))
        if payroll_remainder > 0:
            rows.append(make_row(check_date, cfg["payroll_expenses_account"], debit=payroll_remainder, memo=contractor_name))
    rows.append(make_row(check_date, cfg["workers_comp_account"], debit=wc_credit, memo="Pay by pay"))

    # CREDITS
    rows.append(make_row(check_date, cfg["workers_comp_account"], credit=wc_net_credit, memo="Pay by pay"))
    rows.append(make_row(check_date, cfg["bank_account"], credit=wc_refund, memo="Pay by Pay Refund"))
    rows.append(make_row(check_date, cfg["bank_account"], credit=net_pay, memo="Netpay"))
    taxes_out = round(total_taxes + employer_taxes, 2)
    rows.append(make_row(check_date, cfg["bank_account"], credit=taxes_out, memo="Taxes"))
    if scorp_medical > 0:
        rows.append(make_row(check_date, cfg["shareholder_medical_account"], credit=scorp_medical, memo="2%"))

    return rows


def run_adp_payroll_tipped(args, config_name):
    if len(args) < 1:
        print("Usage: python payroll.py adp_payroll_tipped <payroll.pdf> --config <client.json>")
        sys.exit(1)

    pdf_path        = args[0]
    pay_by_pay_override = None
    for i, arg in enumerate(args):
        if arg == "--pay-by-pay" and i + 1 < len(args):
            pay_by_pay_override = float(args[i + 1])

    cfg = load_config(config_name)
    if pay_by_pay_override is not None:
        cfg["workers_comp_refund"] = pay_by_pay_override

    print(f"Client:  {cfg['client_name']}")
    print(f"PDF:     {pdf_path}")

    text   = extract_text(pdf_path)
    lines  = text.split("\n")
    header = parse_header(text)
    check_date = header.get("check_date", "")
    print(f"Check Date:  {check_date}")
    print(f"Pay Period:  {header.get('pay_period_start')} to {header.get('pay_period_end')}")

    officers   = parse_officers(lines)
    support    = parse_support(lines)
    company    = parse_company_totals(text)
    total_1099 = parse_1099(lines)

    print(f"\n--- Parsed Values ---")
    print(f"  Officers gross:    ${sum(e['gross'] for e in officers['employees']):,.2f}  ({len(officers['employees'])} employee(s))")
    print(f"  S-Corp 2% medical: ${company['scorp_medical']:,.2f}")
    print(f"  Staff regular:     ${support['regular']:,.2f}")
    print(f"  Staff overtime:    ${support['overtime']:,.2f}")
    print(f"  Staff tips:        ${support['tips']:,.2f}")
    print(f"  1099 contractor:   ${total_1099:,.2f}")
    print(f"  Net pay:           ${company['net_pay']:,.2f}")
    print(f"  Employee taxes:    ${company['total_taxes']:,.2f}")
    print(f"  Employer taxes:    ${company['employer_taxes']:,.2f}")

    rows = _build_journal(cfg, officers, support, company, total_1099, check_date)
    print_journal_table(rows, cfg["client_name"], check_date)
    if _qb_confirm(cfg["client_name"]):
        append_payroll_log("adp_payroll_tipped", cfg["client_name"], check_date, rows)
        append_digest_log(cfg["client_name"], check_date)


# ═══════════════════════════════════════════════════════════════════════════
# ADP PAYROLL — TIPPED FORMAT
# ═══════════════════════════════════════════════════════════════════════════

