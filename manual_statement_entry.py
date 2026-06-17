#!/usr/bin/env python3
"""
manual_statement_entry.py
-------------------------
Generate a reconciliation report from manually-keyed statement data, for months
where the statement is a photographed/scanned copy that OCR can't parse
reliably.

The actual transaction data is client-specific, so it lives in a private JSON
(kept out of the public repo) loaded via log_utils.load_private_json:
  - <clients_dir>/manual_statements.json   (private; preferred)
  - ./manual_statements.example.json        (committed template)

JSON schema:
{
  "active": "<month_key>",
  "statements": {
    "<month_key>": {
      "statement_type":   "bmo_checking",          # which parser to use
      "client_name":      "Example Client LLC",
      "statement_period": "Month 1 - Month 28, YYYY",
      "beginning_balance": "0.00",
      "ending_balance":    "0.00",
      "service_fees":      "0.00",
      "credits": [ {"date": "MM/DD/YY", "vendor": "...", "amount": "0.00"} ],
      "checks":  [ {"date": "MM/DD/YY", "number": "1001", "vendor": "", "amount": "0.00"} ],
      "debits":  [ {"date": "MM/DD/YY", "vendor": "...", "amount": "0.00"} ]
    }
  }
}

Usage:
    python manual_statement_entry.py                 # active month -> stdout
    python manual_statement_entry.py output.txt      # active month -> file
    python manual_statement_entry.py --month KEY     # specific month
"""

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from log_utils import load_private_json  # noqa: E402
from reconcile_comprehensive import BMOCheckingParser  # noqa: E402

# statement_type -> parser class. Add more as manual-entry support is needed.
PARSER_BY_TYPE = {
    "bmo_checking": BMOCheckingParser,
}


def _to_decimal(value):
    return Decimal(str(value))


def _decimalize(data: dict) -> dict:
    """Convert string/number amounts in the JSON into Decimal for the parser."""
    out = dict(data)
    for key in ("beginning_balance", "ending_balance", "service_fees"):
        if key in out and out[key] is not None:
            out[key] = _to_decimal(out[key])
    for section in ("credits", "checks", "debits"):
        rows = []
        for row in out.get(section, []):
            row = dict(row)
            row["amount"] = _to_decimal(row["amount"])
            rows.append(row)
        out[section] = rows
    return out


def run(month_key=None, output_path=None):
    config = load_private_json("manual_statements.json")
    if not config or not config.get("statements"):
        print("No manual statement data found. Create manual_statements.json in "
              "your private clients dir (see manual_statements.example.json).")
        return 1

    key = month_key or config.get("active")
    if key not in config.get("statements", {}):
        print(f"Month '{key}' not found. Available: {list(config['statements'])}")
        return 1

    data = _decimalize(config["statements"][key])
    stmt_type = data.get("statement_type", "bmo_checking")
    parser_cls = PARSER_BY_TYPE.get(stmt_type)
    if not parser_cls:
        print(f"No manual-entry parser for statement_type '{stmt_type}'.")
        return 1

    parser = parser_cls.__new__(parser_cls)
    parser.pdf_path          = None
    parser.text              = ''
    parser._ocr_text         = None
    parser.credits           = []
    parser.debits            = []
    parser.checks            = []
    parser.service_fees      = Decimal('0')
    parser.beginning_balance = None
    parser.ending_balance    = None
    parser.client_name       = data.get('client_name', '')
    parser.load_from_dict(data)

    report = parser.generate_report()
    if output_path:
        Path(output_path).write_text(report)
        print(f'Report saved to: {output_path}')
    else:
        print(report)
    return 0


if __name__ == '__main__':
    args = sys.argv[1:]
    month = None
    out = None
    if '--month' in args:
        i = args.index('--month')
        month = args[i + 1]
        args = args[:i] + args[i + 2:]
    if args:
        out = args[0]
    sys.exit(run(month_key=month, output_path=out))
