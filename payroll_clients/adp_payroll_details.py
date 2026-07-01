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

def parse_payroll_details(pdf_path: str, contractors_1099=None) -> dict:
    """Parse ADP Payroll Details PDF. Returns all data needed to build the journal."""
    text  = extract_text(pdf_path)
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    # Check date — use shared parse_header which handles both single-payroll
    # ("Checkdate:") and combined/adjustment ("Checkdatesfrom:") header formats.
    check_date = parse_header(text).get("check_date", "")

    # ── Company totals block ──
    totals = {
        "net_pay": 0, "fed_fit": 0, "fed_socsec": 0, "fed_medcare": 0,
        "ca_sit": 0, "ca_sdi": 0, "emp_taxes": 0,
        "emp_ira": 0, "er_socsec": 0, "er_medcare": 0, "er_futa": 0,
        "ca_sui": 0, "er_ira": 0, "er_taxes": 0, "all_tips": 0,
    }
    in_co, co_lines = False, []
    for line in lines:
        if line == "CompanyTotals:":
            in_co = True; continue
        if in_co:
            co_lines.append(line)
            if "TotalEmployees-Company" in line.replace(" ", ""):
                break

    if co_lines:
        m = re.search(r'Regular.*?\$([\\d,]+\\.\\d{2})\s+FEDSOCSEC-ER', co_lines[0])
        if not m:
            m = re.search(r'Regular.*?\$([\d,]+\.\d{2})\s+FEDSOCSEC-ER', co_lines[0])
        if m:
            totals["net_pay"] = amt(m.group(1))

    for line in co_lines:
        m = re.match(r'(?:QualifiedTipPaid\*|NonqualifiedCredit)\s+[\d.]+\s+\$([\d,]+\.\d+)', line)
        if m: totals["all_tips"] = amt(m.group(1))
        m = re.search(r'FEDFIT\s+\$([\d,]+\.\d+)', line)
        if m: totals["fed_fit"] = amt(m.group(1))
        m = re.search(r'FEDSOCSEC\s+\$([\d,]+\.\d+)', line)
        if m and totals["fed_socsec"] == 0: totals["fed_socsec"] = amt(m.group(1))
        m = re.search(r'\bFED\s+\$([\d,]+\.\d+)\s+\$', line)
        if m and totals["fed_medcare"] == 0: totals["fed_medcare"] = amt(m.group(1))
        m = re.search(r'CASIT\s+\$([\d,]+\.\d+)', line)
        if m: totals["ca_sit"] = amt(m.group(1))
        m = re.search(r'CASDI\s+\$([\d,]+\.\d+)', line)
        if m: totals["ca_sdi"] = amt(m.group(1))
        m = re.search(r'SimpleIRAwith\s+\$([\d,]+\.\d+)', line)
        if m: totals["emp_ira"] = amt(m.group(1))
        m = re.search(r'FEDSOCSEC-ER\s+\$([\d,]+\.\d+)', line)
        if m: totals["er_socsec"] = amt(m.group(1))
        m = re.search(r'FEDMEDCARE-ER\s+\$([\d,]+\.\d+)', line)
        if m: totals["er_medcare"] = amt(m.group(1))
        m = re.search(r'FEDFUTA\s+\$([\d,]+\.\d+)', line)
        if m: totals["er_futa"] = amt(m.group(1))
        m = re.search(r'CASUI-ER\s+\$([\d,]+\.\d+)', line)
        if m: totals["ca_sui"] = amt(m.group(1))
        m = re.search(r'SIMPLEIRA\$employer\s+\$([\d,]+\.\d+)', line)
        if m: totals["er_ira"] = amt(m.group(1))

    totals["emp_taxes"] = round(totals["fed_fit"] + totals["fed_socsec"] + totals["fed_medcare"] + totals["ca_sit"] + totals["ca_sdi"], 2)
    totals["er_taxes"]  = round(totals["er_socsec"] + totals["er_medcare"] + totals["er_futa"] + totals["ca_sui"], 2)

    # Net pay fallback
    if totals["net_pay"] == 0:
        net_pays = re.findall(r'CheckDate:[\d/]+.*?\$([\d,]+\.\d{2})', text.replace(" ", ""))
        totals["net_pay"] = round(sum(amt(x) for x in net_pays), 2)

    # ── Associates dept 002 ──
    assoc = {"regular": 0, "overtime": 0, "tips": 0, "rest": 0, "commission": 0}
    in_assoc = False
    for line in lines:
        if "DepartmentTotals:002-Associates" in line.replace(" ", ""):
            in_assoc = True; continue
        if not in_assoc: continue
        if "TotalEmployees-002" in line.replace(" ", ""): break
        m = re.match(r'Regular\s+[\d.]+\s+\$([\d,]+\.\d+)', line)
        if m: assoc["regular"] = amt(m.group(1)); continue
        m = re.match(r'Overtime\s+[\d.]+\s+\$([\d,]+\.\d+)', line)
        if m: assoc["overtime"] = amt(m.group(1)); continue
        m = re.match(r'(?:QualifiedTipPaid\*|NonqualifiedCredit)\s+[\d.]+\s+\$([\d,]+\.\d+)', line)
        if m: assoc["tips"] = amt(m.group(1)); continue
        m = re.match(r'RestTime\s+[\d.]+\s+\$([\d,]+\.\d+)', line)
        if m: assoc["rest"] = amt(m.group(1)); continue
        m = re.match(r'Commission\s+[\d.]+\s+\$([\d,]+\.\d+)', line)
        if m: assoc["commission"] = amt(m.group(1)); continue

    assoc_gross = round(assoc["regular"] + assoc["overtime"] + assoc["rest"] + assoc["commission"], 2)

    # ── Officers dept 010 ──
    officers = []
    in_off, current_emp = False, None
    for line in lines:
        if "Department:010-Officers" in line.replace(" ", "") and "Totals" not in line:
            in_off = True; continue
        if "DepartmentTotals:010-Officers" in line.replace(" ", ""):
            if current_emp: officers.append(current_emp)
            break
        if not in_off: continue
        m = re.match(r'Employee:(.+?)\s+SSN:', line)
        if m:
            if current_emp: officers.append(current_emp)
            parts = m.group(1).split(",")
            first = parts[1].strip() if len(parts) > 1 else m.group(1).strip()
            current_emp = {"name": first, "commission": 0.0, "medical": 0.0, "tips": 0.0}
            continue
        if current_emp:
            m = re.match(r'Commission\s+[\d.]+\s+([\d,]+\.\d+)', line)
            if m: current_emp["commission"] = amt(m.group(1))
            m2 = re.search(r'S-corp2%medical\s+0\.00\s+([\d,]+\.\d+)', line)
            if m2: current_emp["medical"] = amt(m2.group(1))
            m = re.match(r'(?:QualifiedTipPaid\*|NonqualifiedCredit)\s+[\d.]+\s+([\d,]+\.\d+)', line)
            if m: current_emp["tips"] = amt(m.group(1))

    # ── 1099 contractors ──
    # contractors_1099 is a list of {"name_contains": str, "role": str,
    # "skip_if_zero_net_pay": bool} entries from the client JSON config.
    if contractors_1099 is None:
        contractors_1099 = []
    rent, reimbursement = 0.0, 0.0
    contractor_paid = {c["role"]: True for c in contractors_1099}
    in_1099, last_1099_emp = False, None
    for line in lines:
        if "Department:1099-Contractors" in line.replace(" ", "") and "Totals" not in line:
            in_1099 = True; continue
        if "DepartmentTotals:1099-Contractors" in line.replace(" ", ""): break
        if not in_1099: continue
        norm = line.replace(" ", "")
        for c in contractors_1099:
            if c["name_contains"].replace(" ", "") in norm:
                last_1099_emp = c["role"]; break
        else:
            m = re.match(r'1099\S*\s+[\d.]+\s+([\d,]+\.\d+)', line)
            if m:
                v = amt(m.group(1))
                if last_1099_emp == "rent": rent = v
                elif last_1099_emp == "wc": reimbursement = v
            for c in contractors_1099:
                if c.get("skip_if_zero_net_pay") and last_1099_emp == c["role"]:
                    if "NetPay:$0.00" in norm:
                        contractor_paid[c["role"]] = False

    if not contractor_paid.get("wc", True):
        reimbursement = 0.0

    return dict(
        check_date=check_date, assoc=assoc, assoc_gross=assoc_gross,
        officers=officers, rent=rent, reimbursement=reimbursement, totals=totals,
    )


def parse_liability(pdf_path: str) -> dict:
    """Extract Pay-by-Pay (Workers Comp) amount and check date from an ADP Payroll Liability PDF."""
    text = extract_text(pdf_path)
    wc = 0.0
    m = re.search(r'Debit for Pay-by-Pay\s+.*?\$([\d,]+\.\d{2})', text.replace('\n', ' '))
    if m:
        wc = amt(m.group(1))
    else:
        for line in text.split('\n'):
            if 'Pay-by-Pay' in line:
                m2 = re.search(r'\$([\d,]+\.\d{2})', line)
                if m2:
                    wc = amt(m2.group(1))
                    break
    # Pull check date from the page footer: "Check date: M/D/YYYY - Payroll N"
    check_date = ""
    m = re.search(r'Checkdate:(\d+/\d+/\d+)', text.replace(" ", ""))
    if m:
        check_date = m.group(1)
    return {"wc": wc, "check_date": check_date}


def _build_journal(data: dict, wc_amount: float, cfg: dict) -> list:
    check_date   = data["check_date"]
    assoc_gross  = data["assoc_gross"]
    officers     = data["officers"]
    rent         = data["rent"]
    reimbursement = data["reimbursement"]
    totals       = data["totals"]
    rows = []

    # DEBITS
    if assoc_gross > 0:
        rows.append(make_row(check_date, cfg["departments"]["002"]["regular_account"], debit=assoc_gross))
    for o in officers:
        if o["commission"] > 0:
            rows.append(make_row(check_date, cfg["departments"]["010"]["gross_account"], debit=o["commission"], memo="Commission", name=o["name"]))
        if o["medical"] > 0:
            rows.append(make_row(check_date, cfg["departments"]["010"]["gross_account"], debit=o["medical"], memo="2%", name=o["name"]))
    if rent > 0:
        rows.append(make_row(check_date, cfg["departments"]["1099"]["rent_account"], debit=rent, name=cfg["departments"]["1099"]["rent_vendor"]))
    contractor_amount = reimbursement
    if contractor_amount > 0:
        acct_tax = cfg.get("contractor_accounting_amount", 925.00)
        payroll_processing = round(contractor_amount - acct_tax, 2)
        rows.append(make_row(check_date, cfg["accounting_tax_account"], debit=acct_tax))
        rows.append(make_row(check_date, cfg["payroll_processing_account"], debit=payroll_processing))
    if totals["er_taxes"] > 0:
        rows.append(make_row(check_date, cfg["employer_tax_account"], debit=totals["er_taxes"]))
    if totals["er_ira"] > 0:
        rows.append(make_row(check_date, cfg["employer_ira_account"], debit=totals["er_ira"]))
    if totals["all_tips"] > 0:
        rows.append(make_row(check_date, cfg["tips_paid_account"], debit=totals["all_tips"]))
    if wc_amount > 0:
        rows.append(make_row(check_date, cfg["workers_comp_account"], debit=wc_amount, name="ADP"))

    # CREDITS
    if totals["net_pay"] > 0:
        rows.append(make_row(check_date, cfg["bank_account"], credit=totals["net_pay"], memo="NETPAY"))
    net_taxes = round(totals["emp_taxes"] + totals["er_taxes"], 2)
    if net_taxes > 0:
        rows.append(make_row(check_date, cfg["bank_account"], credit=net_taxes, memo="NET TAXES"))
    if wc_amount > 0:
        rows.append(make_row(check_date, cfg["bank_account"], credit=wc_amount, memo="WC"))
    med_total = sum(o["medical"] for o in officers)
    if med_total > 0:
        rows.append(make_row(check_date, cfg["shareholder_medical_account"], credit=med_total, memo="2%medical"))
    if totals["all_tips"] > 0:
        rows.append(make_row(check_date, cfg["tips_collected_account"], credit=totals["all_tips"]))
    total_ira = round(totals["emp_ira"] + totals["er_ira"], 2)
    if total_ira > 0:
        rows.append(make_row(check_date, cfg["payable_ira_account"], credit=total_ira))

    return rows


def run_adp_payroll_details(args, config_name):
    if len(args) < 1:
        print("Usage: python payroll.py adp_payroll_details <payroll_details.pdf> --config <client.json>")
        sys.exit(1)
    pdf          = args[0]
    liability_pdf = args[1] if len(args) > 1 else None
    cfg          = load_config(config_name)

    # Verify both PDFs are from the same payroll period before parsing further.
    if liability_pdf:
        verify_same_check_date({
            "Payroll Details":   pdf,
            "Payroll Liability": liability_pdf,
        })

    data = parse_payroll_details(pdf, contractors_1099=cfg.get("contractors_1099"))

    wc_amount = 0.0
    if liability_pdf:
        liab = parse_liability(liability_pdf)
        wc_amount = liab["wc"]
        print(f"Pay-by-Pay (WC) from Liability PDF: ${wc_amount:.2f}")
    else:
        print("⚠️  No Payroll Liability PDF provided — Workers Comp will be $0.00")

    rows = _build_journal(data, wc_amount, cfg)
    print_journal_table(rows, cfg["client_name"], data["check_date"])
    if _qb_confirm(cfg["client_name"]):
        append_payroll_log("adp_payroll_details", cfg["client_name"], data["check_date"], rows)
        append_digest_log(cfg["client_name"], data["check_date"])
        archive_payroll_pdf(pdf, cfg["client_name"], data["check_date"])


# ═══════════════════════════════════════════════════════════════════════════
# ADP PAYROLL — DETAILS FORMAT
# ═══════════════════════════════════════════════════════════════════════════

