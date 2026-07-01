#!/usr/bin/env python3
"""
MCP server for the bookkeeping reconciliation engine.

Exposes reconciliation tools to Claude chat (claude.ai) via the
Model Context Protocol. Run with:

    python3 mcp_server.py

Or register in Claude Desktop's claude_desktop_config.json.
"""

import json
import subprocess
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

ENGINE_DIR = Path(__file__).resolve().parent
CLIENTS_DIR = Path.home() / "Bookkeeping-clients"

ENV = {
    **os.environ,
    "BOOKKEEPING_CLIENTS_DIR": str(CLIENTS_DIR),
    "BOOKKEEPING_NO_PROMPT": "1",
}

mcp = FastMCP("bookkeeping")


def _run_script(cmd: list[str], timeout: int = 300) -> str:
    """Run a bookkeeping engine script and return its output."""
    result = subprocess.run(
        cmd,
        cwd=str(ENGINE_DIR),
        env=ENV,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = result.stdout
    if result.stderr:
        output += "\n\nSTDERR:\n" + result.stderr
    if result.returncode != 0:
        output += f"\n\n[exit code {result.returncode}]"
    return output.strip()


def _load_recon_log() -> list[dict]:
    log_path = CLIENTS_DIR / "recon_log.json"
    if not log_path.exists():
        return []
    return json.loads(log_path.read_text())


@mcp.tool()
def reconcile(pdf_path: str) -> str:
    """Run reconciliation on a bank statement PDF from a local file path.

    Args:
        pdf_path: Absolute path to the statement PDF file.
    """
    pdf = Path(pdf_path).expanduser()
    if not pdf.exists():
        return f"Error: file not found: {pdf_path}"
    return _run_script([
        "python3", str(ENGINE_DIR / "reconcile_comprehensive.py"),
        str(pdf), "--no-prompt",
    ])


@mcp.tool()
def reconcile_from_drive(drive_file_id: str) -> str:
    """Run reconciliation on a bank statement PDF stored in Google Drive.

    Use this when a user uploads/drags a PDF into chat. First upload the file
    to Google Drive, then pass the Drive file ID here.

    Args:
        drive_file_id: Google Drive file ID (the long alphanumeric string).
                       Also accepts full Drive URLs.
    """
    return _run_script([
        "python3", str(ENGINE_DIR / "reconcile_comprehensive.py"),
        "--from-drive", drive_file_id, "--no-prompt",
    ])


@mcp.tool()
def check_status(status_filter: str = "") -> str:
    """Check reconciliation status from recon_log.json.

    Args:
        status_filter: Optional filter — "in_progress", "done", "error", or "all".
                       Leave empty to see all IN_PROGRESS entries (most common use).
    """
    entries = _load_recon_log()
    if not status_filter:
        status_filter = "in_progress"

    filtered = []
    for e in entries:
        if e.get("type") != "recon":
            continue
        s = (e.get("status") or "").upper()
        if status_filter.upper() in s or status_filter == "all":
            filtered.append(e)

    if not filtered:
        return f"No entries with status matching '{status_filter}'."

    lines = []
    for e in filtered:
        lines.append(
            f"- {e.get('client')} | {e.get('account_type')} | "
            f"closing {e.get('statement_end_date')} | "
            f"balance ${e.get('beginning_balance', '?')} → ${e.get('ending_balance', '?')} | "
            f"status: {e.get('status')}"
        )
    return f"{len(filtered)} entries:\n" + "\n".join(lines)


@mcp.tool()
def mark_done(client_key: str, account_type: str, statement_date: str = "") -> str:
    """Mark a reconciliation entry as DONE after QuickBooks entry is complete.

    Args:
        client_key: Client key, e.g. "acme_inc", "contoso_llc".
        account_type: Account type, e.g. "amex", "bofa_checking", "citi_checking".
        statement_date: Optional statement closing date (MM/DD/YY or YYYY-MM-DD).
                        If omitted and only one IN_PROGRESS entry matches, uses that.
    """
    cmd = [
        "python3", str(ENGINE_DIR / "mark_clean.py"),
        client_key, account_type,
    ]
    if statement_date:
        cmd.append(statement_date)
    return _run_script(cmd)


@mcp.tool()
def run_payroll(client_key: str, pdf_path: str) -> str:
    """Run payroll journal entry processing on an ADP payroll PDF.

    Args:
        client_key: Client key, e.g. "acme_inc", "contoso_llc".
        pdf_path: Absolute path to the ADP payroll PDF file.
    """
    pdf = Path(pdf_path).expanduser()
    if not pdf.exists():
        return f"Error: file not found: {pdf_path}"
    return _run_script([
        "python3", str(ENGINE_DIR / "payroll.py"),
        client_key, str(pdf), "--no-prompt",
    ])


@mcp.tool()
def open_issues() -> str:
    """Show all unresolved manual issues from recon_log.json."""
    entries = _load_recon_log()
    issues = [
        e for e in entries
        if e.get("type") == "manual" and not e.get("resolved", False)
    ]
    if not issues:
        return "No open manual issues."

    lines = []
    for e in issues:
        lines.append(f"- [{e.get('client')}] {e.get('issue')}")
    return f"{len(issues)} open issues:\n" + "\n".join(lines)


@mcp.tool()
def client_list() -> str:
    """List all configured clients and their account types."""
    lines = []
    for f in sorted(CLIENTS_DIR.glob("*.json")):
        if f.name in ("recon_log.json", "reconciliation_log.csv",
                       "sheets_config.json", "sheets_credentials.json",
                       "digest_config.json", "fixtures_manifest.json",
                       "manual_statements.json"):
            continue
        try:
            cfg = json.loads(f.read_text())
            name = cfg.get("canonical_name") or cfg.get("client_name") or f.stem
            types = cfg.get("statement_types", [])
            lines.append(f"- {f.stem}: {name} ({', '.join(types) if types else 'no statement types'})")
        except (json.JSONDecodeError, KeyError):
            continue
    return "\n".join(lines) if lines else "No client configs found."


if __name__ == "__main__":
    mcp.run()
