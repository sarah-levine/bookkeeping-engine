#!/usr/bin/env python3
"""
Payroll journal entry generator.

Usage:
    python payroll.py <format_or_client_key> <pdf> [pdf2] [--config <client.json>] [--pay-by-pay AMOUNT]

Payroll formats (generic — use with --config <client.json>):
    adp_payroll_professional  — ADP Payroll Details: W-2 officers + 1099 contractors
    adp_payroll_1099          — ADP Payroll Details: 1099 contractors only
    adp_payroll_details       — ADP Payroll Details: W-2 employees + tips
    adp_payroll_tipped        — ADP Payroll Details: multi-dept, tipped employees
    adp_payroll_departments   — ADP Payroll Details: department-level gross/net
    adp_labor_distribution    — ADP Labor Distribution report

Client keys (convenience aliases — config resolved automatically):
    See _CLIENT_DISPATCH below for the current alias → format mapping.
"""

import sys
from functools import partial
from payroll_clients.base import _now_pst
from payroll_clients import (
    run_adp_payroll_professional,
    run_adp_payroll_1099,
    run_adp_payroll_details,
    run_adp_payroll_tipped,
    run_adp_payroll_departments,
    run_adp_labor_distribution,
)

# Format name → runner function (generic, client-agnostic)
FORMATS = {
    "adp_payroll_professional": run_adp_payroll_professional,
    "adp_payroll_1099":         run_adp_payroll_1099,
    "adp_payroll_details":      run_adp_payroll_details,
    "adp_payroll_tipped":       run_adp_payroll_tipped,
    "adp_payroll_departments":  run_adp_payroll_departments,
    "adp_labor_distribution":   run_adp_labor_distribution,
}

# Client key → (format, config_filename), built from client configs.
# Each private client JSON declares its own "payroll_key" and "payroll_format";
# the registry assembles this map so no client names live in this public file.
# (Empty if no configs declare a payroll_key — use the generic
#  `python payroll.py <format> --config <client.json>` form instead.)
def _load_client_dispatch():
    try:
        from parsers.base import _registry
        return _registry.payroll_dispatch()
    except Exception:
        return {}

_CLIENT_DISPATCH = _load_client_dispatch()


def _resolve(key, extra_args):
    """Return (runner_fn, args) for a format name or client key."""
    if key in _CLIENT_DISPATCH:
        fmt, config_name = _CLIENT_DISPATCH[key]
        return partial(FORMATS[fmt], config_name=config_name), extra_args

    if key in FORMATS:
        # Generic format: --config <client.json> must be in args
        config_name = None
        remaining = []
        it = iter(extra_args)
        for a in it:
            if a == "--config":
                config_name = next(it, None)
            else:
                remaining.append(a)
        if not config_name:
            print(f"Error: format '{key}' requires --config <client.json>")
            sys.exit(1)
        return partial(FORMATS[key], config_name=config_name), remaining

    return None, extra_args


def main():
    all_keys = list(FORMATS) + list(_CLIENT_DISPATCH)
    if len(sys.argv) < 2 or sys.argv[1] not in all_keys:
        print(__doc__)
        print("Formats:    ", ", ".join(FORMATS))
        print("Client keys:", ", ".join(_CLIENT_DISPATCH))
        sys.exit(1)

    key  = sys.argv[1]
    args = sys.argv[2:]

    # --no-prompt: auto-answer 'later' at QB confirmation, for non-interactive use
    if '--no-prompt' in args:
        args = [a for a in args if a != '--no-prompt']
        import os
        os.environ['BOOKKEEPING_NO_PROMPT'] = '1'

    runner, args = _resolve(key, args)

    # Tee stdout to a dated output file
    import io
    from pathlib import Path

    output_dir = Path(__file__).parent / "output"
    output_dir.mkdir(exist_ok=True)
    timestamp = _now_pst().strftime("%Y-%m-%d_%H%M%S")
    output_file = output_dir / f"payroll_{key}_{timestamp}.txt"

    class Tee:
        def __init__(self, *streams):
            self.streams = streams
        def write(self, data):
            for s in self.streams:
                s.write(data)
        def flush(self):
            for s in self.streams:
                s.flush()

    original_stdout = sys.stdout
    file_handle = open(output_file, "w")
    sys.stdout = Tee(original_stdout, file_handle)

    try:
        runner(args)
    finally:
        sys.stdout = original_stdout
        print("\nOutput saved to: " + str(output_file))


if __name__ == "__main__":
    main()
