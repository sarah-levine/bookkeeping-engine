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
    m = re.search(r'Check Date:\s*(\d{2}/\d{2}/\d{4})', text)
    return m.group(1) if m else ""


def slice_division_totals(text: str, division_code: str) -> str:
    """Extracts the 'Totals for Division: (XX)' block for one division."""
    pattern = rf'Totals\s*for\s*Division:\s*\({division_code}\)'
    m = re.search(pattern, text)
    if not m:
        return ""
    start = m.start()
    tail  = text[start:]
    end_m = re.search(
        r'Total Earnings\s+[\d.,]+\s+[\d.,]+\s+Total EE Taxes\s+[\d.,]+\s+'
        r'Total ER Taxes\s+[\d.,]+\s+Total Deductions\s+[\d.,]+', tail
    )
    if end_m:
        return tail[:end_m.end()]
    return tail[:2000]  # fallback


def parse_division_block(block: str) -> dict:
    """Extracts gross, EE taxes, ER taxes, deductions (with breakdown) from a
    'Totals for Division' block."""
    out = {
        "gross": 0.0,
        "ee_taxes": 0.0,
        "er_taxes": 0.0,
        "total_deductions": 0.0,
        "ded_401k": 0.0,         # 401K + 401KLN combined
        "ded_vsp": 0.0,          # VSP
        "earnings_breakdown": {},  # REG, HOL, etc.
    }

    m = re.search(
        r'Total Earnings\s+[\d.,]+\s+([\d.,]+)\s+'
        r'Total EE Taxes\s+([\d.,]+)\s+'
        r'Total ER Taxes\s+([\d.,]+)\s+'
        r'Total Deductions\s+([\d.,]+)',
        block
    )
    if m:
        out["gross"]            = amt(m.group(1))
        out["ee_taxes"]         = amt(m.group(2))
        out["er_taxes"]         = amt(m.group(3))
        out["total_deductions"] = amt(m.group(4))

    for em in re.finditer(r'(REG-REG|HOL-HOL|OT-OT|SAL-SAL)\s+([\d.,]+)\s+([\d.,]+)', block):
        code   = em.group(1)
        amount = amt(em.group(3))
        out["earnings_breakdown"][code] = out["earnings_breakdown"].get(code, 0.0) + amount

    # 401K deduction lines — match "401K- 100.00" and "401KLN- 378.33"
    # Word boundary and "-" suffix avoid matching "401KER-" (employer match)
    k401 = 0.0
    for dm in re.finditer(r'\b(401K|401KLN)-\s+([\d.,]+)', block):
        if dm.group(1) in ("401K", "401KLN"):
            k401 += amt(dm.group(2))
    out["ded_401k"] = round(k401, 2)

    vsp_m = re.search(r'VSP-\s+([\d.,]+)', block)
    if vsp_m:
        out["ded_vsp"] = amt(vsp_m.group(1))

    return out


def _build_admin_journal(data: dict, cfg: dict, check_date: str) -> list:
    a = cfg["admin"]
    rows = []
    rows.append(make_row(check_date, a["gross_account"], debit=data["gross"],
                         memo=f"Div {a['division_code']}",
                         name=cfg.get("name_tag", "")))
    rows.append(make_row(check_date, a["employer_tax_account"], debit=data["er_taxes"],
                         name=cfg.get("name_tag", "")))
    if data["ded_401k"] > 0:
        rows.append(make_row(check_date, a["k401_withholding_account"], credit=data["ded_401k"]))
    if data["ded_vsp"] > 0:
        rows.append(make_row(check_date, a["vsp_account"], credit=data["ded_vsp"]))
    nettax = round(data["ee_taxes"] + data["er_taxes"], 2)
    netpay = round(data["gross"] - data["ee_taxes"] - data["total_deductions"], 2)
    rows.append(make_row(check_date, a["clearing_account"], credit=nettax, memo="Taxes"))
    rows.append(make_row(check_date, a["clearing_account"], credit=netpay))
    return rows


def _build_agency_journal(data: dict, cfg: dict, check_date: str) -> list:
    g = cfg["agency"]
    rows = []
    rows.append(make_row(check_date, g["gross_account"], debit=data["gross"],
                         memo=f"Div {g['division_code']}",
                         name=cfg.get("name_tag", "")))
    rows.append(make_row(check_date, g["employer_tax_account"], debit=data["er_taxes"],
                         name=cfg.get("name_tag", "")))
    nettax = round(data["ee_taxes"] + data["er_taxes"], 2)
    netpay = round(data["gross"] - data["ee_taxes"] - data["total_deductions"], 2)
    rows.append(make_row(check_date, g["clearing_account"], credit=nettax, memo="525 nettax"))
    rows.append(make_row(check_date, g["clearing_account"], credit=netpay, memo="521 netpay"))
    return rows


def run_adp_labor_distribution(args, config_name):
    if len(args) < 1:
        print("Usage: python payroll.py adp_labor_distribution <payroll.pdf> --config <client.json>")
        sys.exit(1)

    pdf_path = args[0]
    cfg      = load_config(config_name)

    text       = extract_text(pdf_path)
    check_date = parse_check_date(text)

    # Admin (Div 10)
    admin_block = slice_division_totals(text, cfg["admin"]["division_code"])
    admin_data  = parse_division_block(admin_block)

    # Agency (Div 50)
    agency_block = slice_division_totals(text, cfg["agency"]["division_code"])
    agency_data  = parse_division_block(agency_block)

    print(f"\n📋 PARSE SUMMARY — {cfg['client_name']} — Check Date {check_date}")
    print(f"   Admin (Div 10):   gross=${admin_data['gross']:,.2f}   "
          f"EE_tax=${admin_data['ee_taxes']:,.2f}   ER_tax=${admin_data['er_taxes']:,.2f}   "
          f"deductions=${admin_data['total_deductions']:,.2f} "
          f"(401k=${admin_data['ded_401k']:,.2f}, VSP=${admin_data['ded_vsp']:,.2f})")
    print(f"   Agency (Div 50):  gross=${agency_data['gross']:,.2f}   "
          f"EE_tax=${agency_data['ee_taxes']:,.2f}   ER_tax=${agency_data['er_taxes']:,.2f}   "
          f"deductions=${agency_data['total_deductions']:,.2f}")

    admin_rows  = _build_admin_journal(admin_data,  cfg, check_date)
    agency_rows = _build_agency_journal(agency_data, cfg, check_date)

    print_journal_table(agency_rows, f"{cfg['client_name']} — AGENCY/1099 (Div 50)", check_date)
    agency_confirmed = _qb_confirm(f"{cfg['client_name']} — Agency (Div 50)")
    print_journal_table(admin_rows,  f"{cfg['client_name']} — ADMIN (Div 10)",  check_date)
    admin_confirmed  = _qb_confirm(f"{cfg['client_name']} — Admin (Div 10)")

    if agency_confirmed and admin_confirmed:
        append_payroll_log("adp_labor_agency", f"{cfg['client_name']} — Agency", check_date, agency_rows)
        append_digest_log(f"{cfg['client_name']} — Agency", check_date)
        append_payroll_log("adp_labor_admin",  f"{cfg['client_name']} — Admin",  check_date, admin_rows)
        append_digest_log(f"{cfg['client_name']} — Admin",  check_date)
        print(f"  ✅ Both Div 50 + Div 10 confirmed — logged.")
    else:
        pending = []
        if not agency_confirmed: pending.append("Div 50 (Agency)")
        if not admin_confirmed:  pending.append("Div 10 (Admin)")
        print(f"  ⚠️  Not logged — still pending QB entry for: {', '.join(pending)}")



