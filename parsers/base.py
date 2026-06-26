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

class ClientRegistry:
    """
    Loads all client configs from clients/*.json and provides:
      - normalize_vendor(client_name, description) → clean vendor string
      - KNOWN_CLIENTS list for auto-detection
      - CLIENT_CANONICAL dict for alias resolution
      - CLIENT_CARDHOLDERS dict for multi-cardholder AmEx statements
    """

    def __init__(self, clients_dir=None):
        if clients_dir is None:
            from log_utils import get_clients_dir
            clients_dir = get_clients_dir()
        self._configs = {}        # canonical_name → config dict
        self._config_files = {}   # canonical_name → source JSON filename
        self._alias_map = {}      # any name (upper) → canonical_name
        self._normalizer_cache = {}
        self._global_vendor_rules = []  # shared rules applied as fallback
        self._schema = self._load_schema(Path(clients_dir))
        self._load(Path(clients_dir))
        self._load_global_vendor_rules(Path(clients_dir))

    @staticmethod
    def _load_schema(clients_dir):
        schema_path = clients_dir / '_schema.json'
        if not schema_path.exists():
            # Fall back to the schema bundled with the public repo
            schema_path = Path(__file__).parent.parent / 'clients' / '_schema.json'
        if schema_path.exists():
            with open(schema_path) as f:
                return json.load(f)
        return None

    def _load(self, clients_dir):
        if not clients_dir.exists():
            return
        validation_errors = []  # collect across all configs, report once
        for path in sorted(clients_dir.glob('*.json')):
            # Skip non-client files like _schema.json (underscore-prefixed)
            if path.name.startswith('_'):
                continue
            try:
                with open(path) as f:
                    cfg = json.load(f)
            except Exception:
                continue
            # Operational logs (recon_log.json is a JSON list) and other
            # non-object JSON share this directory — skip anything that isn't a
            # config object.
            if not isinstance(cfg, dict):
                continue
            # Shared config files (digest_config.json, sheets_config.json,
            # manual_statements.json, fixtures_manifest.json) live in the same
            # directory but are not client configs — they have neither
            # client_name nor canonical_name. Skip them before validating.
            if not (cfg.get('client_name') or cfg.get('canonical_name')):
                continue
            if self._schema:
                err = self._validate(cfg)
                if err:
                    validation_errors.append(f"  {path.name}: {err}")
                    continue  # don't register a config we couldn't validate
            canonical = cfg.get('canonical_name') or cfg.get('client_name', '').upper()
            if not canonical:
                continue
            self._configs[canonical] = cfg
            self._config_files[canonical] = path.name
            # Register canonical name
            self._alias_map[canonical] = canonical
            # Register client_name
            self._alias_map[cfg.get('client_name', '').upper()] = canonical
            # Register aliases
            for alias in cfg.get('aliases', []):
                self._alias_map[alias.upper()] = canonical

        if validation_errors:
            raise ValueError(
                "Client config schema validation failed:\n"
                + "\n".join(validation_errors)
            )

    def _validate(self, cfg):
        """Validate one config against the schema. Returns an error message
        string, or None if valid (or if jsonschema isn't installed)."""
        try:
            import jsonschema as _js
        except ImportError:
            return None  # jsonschema not installed — skip validation
        try:
            _js.validate(cfg, self._schema)
        except _js.ValidationError as e:
            return e.message
        return None


    def _load_global_vendor_rules(self, clients_dir):
        """Load shared vendor rules from vendor_rules_global.json."""
        path = clients_dir / "vendor_rules_global.json"
        if not path.exists():
            return
        try:
            with open(path) as f:
                data = json.load(f)
            self._global_vendor_rules = data.get("vendor_rules", [])
        except Exception:
            pass

    def resolve(self, name):
        """Return canonical client name, or None if not found."""
        if not name:
            return None
        return self._alias_map.get(name.upper())

    def get_config(self, name):
        canonical = self.resolve(name)
        return self._configs.get(canonical) if canonical else None

    @staticmethod
    def _match_rules(rules, d_upper):
        """Try to match a description against a list of vendor rules.

        Returns the normalized string if a rule matches, else None.
        """
        for rule in rules:
            contains = rule.get('contains', '').upper()
            starts = rule.get('starts_with', '').upper()
            if not contains and not starts:
                continue
            if contains and contains not in d_upper:
                continue
            if starts and not d_upper.startswith(starts):
                continue
            also = rule.get('also_contains', '').upper()
            if also and also not in d_upper:
                continue
            also2 = rule.get('also_contains2', '').upper()
            if also2 and also2 not in d_upper:
                continue
            also_any = [x.upper() for x in rule.get('also_contains_any', [])]
            if also_any and not any(x in d_upper for x in also_any):
                continue
            result = rule['normalize_to']
            suffix = rule.get('display_suffix', '')
            if suffix:
                result = f"{result} ({suffix})"
            return result
        return None

    def normalize_vendor(self, client_name, description):
        """Normalize a transaction description using two tiers of rules.

        1. Client-specific rules (highest priority)
        2. Global rules (fallback for common vendors)

        Returns the original description if no rule matches.
        """
        d = description.upper().strip()

        # Tier 1: client-specific rules
        cfg = self.get_config(client_name)
        if cfg:
            result = self._match_rules(cfg.get('vendor_rules', []), d)
            if result is not None:
                return result

        # Tier 2: global rules
        result = self._match_rules(self._global_vendor_rules, d)
        if result is not None:
            return result

        return description

    def clean_and_normalize(self, client_name, description):
        """Full client-tier pipeline: strip the client's configured
        `description_strip_suffixes`, then apply its `vendor_rules`. This is the
        single entry point parsers should use so suffix-stripping is consistent
        across every bank (instead of each parser doing it inline)."""
        cfg = self.get_config(client_name) or {}
        d = strip_client_suffixes(description, cfg.get('description_strip_suffixes'))
        return self.normalize_vendor(client_name, d)

    @property
    def KNOWN_CLIENTS(self):
        return list(self._alias_map.keys())

    @property
    def CLIENT_CANONICAL(self):
        return dict(self._alias_map)

    def lookup_account_ending(self, last4):
        """Map a card/account last-4 to (canonical_client, account_type) using the
        per-client `account_endings` config map. Returns None if no client
        declares that ending. Used to disambiguate statements whose product name
        only appears in a logo image (e.g. Chase Sapphire vs Ink)."""
        if not last4:
            return None
        last4 = str(last4)
        for canonical, cfg in self._configs.items():
            endings = cfg.get('account_endings') or {}
            if last4 in endings:
                return (canonical, endings[last4])
        return None

    def payroll_dispatch(self):
        """Build the {client_key: (payroll_format, config_filename)} map from
        client configs that declare both `payroll_key` and `payroll_format`.
        Replaces a hardcoded table so no client names live in public code."""
        dispatch = {}
        for canonical, cfg in self._configs.items():
            key = cfg.get('payroll_key')
            fmt = cfg.get('payroll_format')
            if key and fmt:
                dispatch[key] = (fmt, self._config_files.get(canonical))
        return dispatch

    @property
    def CLIENT_CARDHOLDERS(self):
        result = {}
        for canonical, cfg in self._configs.items():
            cardholders = cfg.get('cardholders', [])
            if cardholders:
                result[canonical] = cardholders
        return result


# Global registry instance
_registry = ClientRegistry()

# Backward-compatible aliases used throughout the script
KNOWN_CLIENTS    = _registry.KNOWN_CLIENTS
CLIENT_CANONICAL = _registry.CLIENT_CANONICAL
CLIENT_CARDHOLDERS = _registry.CLIENT_CARDHOLDERS


def _normalize_vendor_for_client(client_name, description):
    """Data-driven vendor normalization using client JSON configs."""
    return _registry.normalize_vendor(client_name, description)


# ═══════════════════════════════════════════════════════════════════════════════
# AUTO-CLEAN + INTERACTIVE APPROVAL
# Suggests a clean vendor name for unmatched descriptions and prompts the user
# to approve, edit, or skip. Approved rules are appended to the client's JSON.
# ═══════════════════════════════════════════════════════════════════════════════

import sys as _sys

# Standard (client-agnostic) vendor cleaning lives in one module; client-
# specific rules come from clients/*.json. Nothing is reimplemented locally.
from parsers.vendor_normalize import (
    US_STATE_CODES as _US_STATE_CODES,
    VENDOR_PROMPT_BLOCKLIST as _VENDOR_PROMPT_BLOCKLIST,
    strip_client_suffixes,
    auto_clean_vendor as _auto_clean_vendor,
)




def _collect_unknown_vendors(parser):
    """Return a sorted list of unique raw vendor strings with no matching rule."""
    seen = {}
    client = parser.client_name or ''
    for t in getattr(parser, 'charges', []):
        raw = t.get('vendor', '')
        if not raw:
            continue
        if raw.upper().strip() in _VENDOR_PROMPT_BLOCKLIST:
            continue
        normalized = _registry.normalize_vendor(client, raw)
        if normalized == raw:  # no rule fired
            seen.setdefault(raw, 0)
            seen[raw] += 1
    for c in getattr(parser, 'credits', []):
        raw = c.get('description') or c.get('vendor', '')
        if not raw:
            continue
        if raw.upper().strip() in _VENDOR_PROMPT_BLOCKLIST:
            continue
        normalized = _registry.normalize_vendor(client, raw)
        if normalized == raw:
            seen.setdefault(raw, 0)
            seen[raw] += 1
    return sorted(seen.keys())


def _prompt_approve_new_vendors(parser, clients_dir=None):
    """
    For each unmatched raw vendor, suggest a cleaned name and prompt the user.
    Approved rules are appended to the client's JSON file and the registry is reloaded.
    No-op in non-interactive environments.
    """
    if not _sys.stdin.isatty():
        return
    client = parser.client_name
    if not client:
        return
    canonical = _registry.resolve(client)
    if not canonical:
        return
    cfg = _registry.get_config(client)
    if not cfg:
        return

    unknowns = _collect_unknown_vendors(parser)
    if not unknowns:
        return

    if clients_dir is None:
        from log_utils import get_clients_dir
        clients_dir = get_clients_dir()
    # Find the config file path
    filename = None
    for path in Path(clients_dir).glob('*.json'):
        try:
            with open(path) as f:
                test = json.load(f)
            if (test.get('canonical_name') or test.get('client_name', '').upper()) == canonical:
                filename = path
                break
        except Exception:
            continue
    if not filename:
        return

    print()
    print(f"  ── {len(unknowns)} new vendor(s) detected for {client} ──")
    print("  Approve each to add a normalization rule.")
    print()

    new_rules = []
    skip_rest = False
    _strip_suffixes = cfg.get('description_strip_suffixes') or []
    for raw in unknowns:
        if skip_rest:
            break
        display, contains_key = _auto_clean_vendor(raw, _strip_suffixes)
        while True:
            print(f"  Raw:        {raw}")
            print(f"  Suggest:    {display}")
            print(f"  Match key:  contains '{contains_key}'")
            choice = input("  [y]es / [n]o skip / [e]dit / [s]kip all  > ").strip().lower()
            if choice in ('y', 'yes', ''):
                new_rules.append({'contains': contains_key, 'normalize_to': display})
                print(f"  ✓ Will add: '{contains_key}' → '{display}'")
                print()
                break
            elif choice in ('n', 'no'):
                print("  · Skipped (no rule added)")
                print()
                break
            elif choice in ('e', 'edit'):
                new_display = input(f"    Display name [{display}]: ").strip() or display
                new_key = input(f"    Match contains [{contains_key}]: ").strip().upper() or contains_key
                display, contains_key = new_display, new_key
                continue
            elif choice in ('s', 'skip all'):
                skip_rest = True
                print("  · Skipping remaining vendors.")
                break
            else:
                print("  (Choose y/n/e/s)")

    if not new_rules:
        return

    # Append to JSON file
    with open(filename) as f:
        full_cfg = json.load(f)
    full_cfg.setdefault('vendor_rules', []).extend(new_rules)
    with open(filename, 'w') as f:
        json.dump(full_cfg, f, indent=2)

    # Reload registry so the new rules apply to this run's report
    _registry._load(Path(clients_dir))

    print(f"  ✓ Added {len(new_rules)} rule(s) to {filename.name}")
    print()


# ═══════════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════
# CLIENT DETECTION
# All vendor normalization is now data-driven via clients/*.json
# ═══════════════════════════════════════════════════════════════════════════════

# Known client names for auto-detection (loaded from registry)
KNOWN_CLIENTS = _registry.KNOWN_CLIENTS

# Canonical name resolution (loaded from registry)
CLIENT_CANONICAL = _registry.CLIENT_CANONICAL

# Empty — kept for any remaining references during transition
CLIENT_NORMALIZERS = {}


def _classify_cc_transaction(vendor, amount):
    """
    Classify a credit card transaction as 'payment', 'credit', or 'charge'.
    Returns one of: 'payment', 'credit', 'charge'
    """
    v = vendor.upper()
    # Actual payments to the card account
    if any(kw in v for kw in [
        'AUTOMATIC PAYMENT', 'PAYMENT - THANK YOU', 'ELECTRONIC PAYMENT',
        'ONLINE PAYMENT', 'AUTOPAY PAYMENT', 'PAYMENT RECEIVED',
    ]):
        return 'payment'
    # Also treat negative amounts with PAYMENT keyword as payments
    if amount < 0 and 'PAYMENT' in v:
        return 'payment'
    # Credits / refunds / returns
    if any(kw in v for kw in [
        'MKTPLACE PMTS', 'MKTPL PMTS', 'AMAZON MKTPLACE',
        'CREDIT', 'RETURN', 'REFUND', 'WIRELESS CREDIT',
        'AMEX CREDIT',
    ]):
        return 'credit'
    # Negative amounts that aren't payments are credits
    if amount < 0:
        return 'credit'
    return 'charge'


# Cardholder names per client (for multi-cardholder AmEx statements)
# NOTE: Loaded dynamically from clients/*.json via _registry.CLIENT_CARDHOLDERS above.
# Do not hardcode here — it would overwrite the registry.


# ═══════════════════════════════════════════════════════════════════════════════
# BASE PARSER
# ═══════════════════════════════════════════════════════════════════════════════

class StatementParser:
    """Base class: text extraction, client detection, vendor normalization"""

    statement_type = "Unknown"

    def __init__(self, pdf_path, client_name=None):
        self.pdf_path = str(pdf_path)
        self.text = self._extract_text()
        self.client_name = client_name or self._detect_client()

    def _extract_text(self):
        try:
            result = subprocess.run(
                ['pdftotext', '-layout', self.pdf_path, '-'],
                capture_output=True, text=True, check=True
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            print(f"Error extracting PDF text: {e}")
            sys.exit(1)
        except FileNotFoundError:
            from parsers.pdf_utils import pdf_to_text
            return pdf_to_text(self.pdf_path)

    def _detect_client(self):
        text_upper = self.text.upper()
        for name in KNOWN_CLIENTS:
            if name in text_upper:
                return CLIENT_CANONICAL.get(name, name)
        return None

    def normalize_vendor(self, description):
        return _registry.normalize_vendor(self.client_name or '', description)

    def client_feature(self, name, default=False):
        """Return a feature flag from the client's JSON config: features.<name>.

        Drives client-specific behavior from config instead of hardcoded
        client-name checks in the code (Phase 3b of the refactoring roadmap).
        """
        if not self.client_name:
            return default
        cfg = _registry.get_config(self.client_name) or {}
        return cfg.get('features', {}).get(name, default)

    def transaction_aggregations(self):
        """Client-configured roll-up rules (config: `transaction_aggregations`).

        Each rule collapses every transaction whose UPPERCASE description
        contains `match` into a single line labelled `label` (or `card_label`
        on card statements, defaulting to `label`). This keeps specific
        vendor/brand names in client config — never hardcoded in parser code —
        so this module stays client-agnostic and public-repo safe.

        Returns a list of normalized dicts: {match (uppercased), label,
        card_label}. Rules missing `match` or `label` are ignored.
        """
        cfg = _registry.get_config(self.client_name) or {}
        rules = []
        for r in cfg.get('transaction_aggregations', []) or []:
            match, label = r.get('match'), r.get('label')
            if match and label:
                rules.append({'match': match.upper(), 'label': label,
                              'card_label': r.get('card_label') or label})
        return rules

    @staticmethod
    def _rollup_line(txns, label):
        """Collapse a list of transactions into one {date,vendor,amount,count}
        line dated at the latest transaction date."""
        total = sum(t['amount'] for t in txns)
        latest = max(datetime.strptime(t['date'], '%m/%d/%y') for t in txns)
        return {'date': latest.strftime('%m/%d/%y'), 'vendor': label,
                'amount': total, 'count': len(txns)}

    def _add_year_to_date(self, date_mm_dd, closing_date_mm_dd_yy):
        """
        Convert MM/DD to MM/DD/YY using the statement closing date.
        Dates with a month > closing month are assumed to be from the prior year.
        e.g. closing=02/17/26 -> 01/xx -> 01/xx/26, 12/xx -> 12/xx/25
        """
        try:
            close = datetime.strptime(closing_date_mm_dd_yy, '%m/%d/%y')
            parts = date_mm_dd.split('/')
            if len(parts) == 3:
                return date_mm_dd  # already has year
            mm, dd = int(parts[0]), int(parts[1])
            year = close.year if mm <= close.month else close.year - 1
            yy = str(year)[-2:]
            return f"{mm:02d}/{dd:02d}/{yy}"
        except Exception:
            return date_mm_dd

    def parse(self):
        raise NotImplementedError

    def generate_report(self):
        raise NotImplementedError

    # ── Vision fallback for scanned / image-quality PDFs ─────────────────────
    # Credit-card parsers call _try_vision_fallback() at the END of parse().
    # If the existing pdftotext-based parse already tied to the penny, this is
    # a no-op. If not, and vision is configured, it re-extracts and assigns
    # the parser's native fields. generate_report() then runs unchanged.

    def _tied_out(self):
        """
        Self-check for credit-card parsers: prev + charges - payments - credits
        must equal new_balance to the penny.

        Returns False if either balance is missing/zero (which means parsing
        failed badly enough that we should try vision).
        """
        prev = getattr(self, 'previous_balance', None)
        new = getattr(self, 'new_balance', None)
        if prev is None or new is None:
            return False
        try:
            prev_d = Decimal(str(prev))
            new_d = Decimal(str(new))
        except Exception:
            return False
        if prev_d == 0 and new_d == 0:
            return False
        # Prefer total_payments attribute; fall back to summing the payments list
        # (some parsers set the attribute only inside generate_report, not parse).
        total_payments = Decimal(str(getattr(self, 'total_payments', 0) or 0))
        if total_payments == 0:
            total_payments = sum(
                (Decimal(str(p['amount'])) for p in getattr(self, 'payments', [])),
                Decimal('0'),
            )
        total_credits = sum(
            (Decimal(str(c['amount'])) for c in getattr(self, 'credits', [])),
            Decimal('0'),
        )
        fees   = Decimal(str(getattr(self, 'fees',          0) or 0))
        intr   = Decimal(str(getattr(self, 'interest',      0) or 0))
        finance_charge = Decimal(str(getattr(self, 'finance_charge', 0) or 0)) + fees + intr
        # Exclude charges whose vendor name contains INTEREST — those are finance-
        # charge line items already captured in self.fees/self.interest; counting
        # them here would double-count.
        total_charges = sum(
            (Decimal(str(c['amount'])) for c in getattr(self, 'charges', [])
             if 'INTEREST' not in str(c.get('vendor', '')).upper()),
            Decimal('0'),
        )
        computed = prev_d + total_charges + finance_charge - total_payments - total_credits
        return abs(computed - new_d) < Decimal('0.01')

    def _pdf_is_text_based(self, min_chars: int = 300) -> bool:
        """Return True if pdftotext extracted enough text to be trustworthy."""
        return bool(getattr(self, 'text', None)) and len(self.text.strip()) >= min_chars

    def _tie_out_diagnostic(self):
        """Print a breakdown of what the parser extracted vs. the statement total."""
        prev = Decimal(str(getattr(self, 'previous_balance', 0) or 0))
        new  = Decimal(str(getattr(self, 'new_balance',      0) or 0))
        pays = Decimal(str(getattr(self, 'total_payments',   0) or 0))
        if pays == 0:
            pays = sum((Decimal(str(p['amount'])) for p in getattr(self, 'payments', [])), Decimal('0'))
        cred = sum((Decimal(str(c['amount'])) for c in getattr(self, 'credits',  [])), Decimal('0'))
        chrg = sum(
            (Decimal(str(c['amount'])) for c in getattr(self, 'charges', [])
             if 'INTEREST' not in str(c.get('vendor', '')).upper()),
            Decimal('0'),
        )
        fees = Decimal(str(getattr(self, 'fees',     0) or 0))
        intr = Decimal(str(getattr(self, 'interest', 0) or 0))
        fc   = Decimal(str(getattr(self, 'finance_charge', 0) or 0))
        finance = fees + intr + fc
        computed = prev + chrg + finance - pays - cred
        print("  ── Parser diagnostic ──────────────────────────────────────────", file=sys.stderr)
        print(f"    Previous balance : {prev:>10}", file=sys.stderr)
        print(f"  + Charges          : {chrg:>10}  ({len(getattr(self,'charges',[]))} items)", file=sys.stderr)
        print(f"  + Finance charges  : {finance:>10}  (fees={fees} interest={intr})", file=sys.stderr)
        print(f"  - Payments         : {pays:>10}", file=sys.stderr)
        print(f"  - Credits          : {cred:>10}  ({len(getattr(self,'credits',[]))} items)", file=sys.stderr)
        print(f"  = Computed         : {computed:>10}", file=sys.stderr)
        print(f"    Statement says   : {new:>10}  (diff={abs(computed-new):.2f})", file=sys.stderr)
        print("  ───────────────────────────────────────────────────────────────", file=sys.stderr)

    # Labels that indicate the discrepancy is an unextracted finance charge.
    # Checked in order; first match wins.
    _FINANCE_CHARGE_LABELS = [
        r'Finance Charges?',
        r'Total Fees? for this Period',
        r'Total Interest Charged for this Period',
        r'Interest Charge[ds]?',
        r'Periodic (?:Finance )?Charge[ds]?',
        r'Annual Fee',
        r'Late (?:Payment )?Fee',
        r'Minimum (?:Interest )?Charge',
    ]

    def _try_recover_balance(self) -> bool:
        """
        Attempt to close a balance discrepancy without Vision or human input.

        Strategy: compute the gap (new_balance - computed), then search the raw
        PDF text for a line whose dollar amount equals that gap and whose label
        matches a known finance-charge / fee / interest pattern.  If exactly one
        such line is found, assign the amount to self.fees and return True.

        Returns True if recovery succeeded (balance now ties), False otherwise.
        """
        if self._tied_out():
            return True

        text = getattr(self, 'text', '') or ''
        if not text.strip():
            return False

        prev   = Decimal(str(getattr(self, 'previous_balance', 0) or 0))
        new    = Decimal(str(getattr(self, 'new_balance',      0) or 0))
        pays   = Decimal(str(getattr(self, 'total_payments',   0) or 0))
        cred   = sum((Decimal(str(c['amount'])) for c in getattr(self, 'credits',  [])), Decimal('0'))
        chrg   = sum((Decimal(str(c['amount'])) for c in getattr(self, 'charges',  [])), Decimal('0'))
        fees   = Decimal(str(getattr(self, 'fees',          0) or 0))
        intr   = Decimal(str(getattr(self, 'interest',      0) or 0))
        fc     = Decimal(str(getattr(self, 'finance_charge',0) or 0))
        computed = prev + chrg + fees + intr + fc - pays - cred
        gap = new - computed  # positive → missing charge; negative → missing credit

        if abs(gap) < Decimal('0.01'):
            return True  # already tied
        if abs(gap) > Decimal('500'):
            return False  # implausibly large for a missed label — don't guess

        gap_str = f'{abs(gap):.2f}'
        label_pattern = '|'.join(self._FINANCE_CHARGE_LABELS)
        # Match lines like:  "Finance Charges   $  2.21"  or  "Finance Charges: $2.21"
        pattern = re.compile(
            rf'(?:{label_pattern})[^\n]{{0,60}}\$\s*{re.escape(gap_str)}',
            re.IGNORECASE,
        )
        matches = pattern.findall(text)

        if not matches:
            return False

        if len(matches) > 1:
            # Multiple hits — ambiguous, don't auto-assign
            print(
                f"  ⚠ Recovery: found {len(matches)} lines matching gap ${gap_str} "
                f"— ambiguous, not auto-assigning.",
                file=sys.stderr,
            )
            return False

        # Exactly one match: assign the gap to fees and report it.
        matched_line = matches[0].strip()
        if gap > 0:
            self.fees = fees + gap
            print(
                f"  ✓ Recovery: assigned ${gap_str} from \"{matched_line}\" to finance charges.",
                file=sys.stderr,
            )
        else:
            # gap is negative → unaccounted credit; don't auto-assign to avoid
            # mis-classifying a refund as an interest credit.
            print(
                f"  ⚠ Recovery: gap is negative (${gap_str}) — manual review needed.",
                file=sys.stderr,
            )
            return False

        return self._tied_out()

    def _log_parser_bug_to_roadmap(self):
        """Append a parser bug entry to REFACTORING_ROADMAP.md so it gets tracked."""
        import datetime
        roadmap = REPO_DIR / 'REFACTORING_ROADMAP.md'
        if not roadmap.exists():
            return
        prev  = Decimal(str(getattr(self, 'previous_balance', 0) or 0))
        new   = Decimal(str(getattr(self, 'new_balance',      0) or 0))
        pays  = Decimal(str(getattr(self, 'total_payments',   0) or 0))
        if pays == 0:
            pays = sum((Decimal(str(p['amount'])) for p in getattr(self, 'payments', [])), Decimal('0'))
        cred  = sum((Decimal(str(c['amount'])) for c in getattr(self, 'credits', [])), Decimal('0'))
        chrg  = sum(
            (Decimal(str(c['amount'])) for c in getattr(self, 'charges', [])
             if 'INTEREST' not in str(c.get('vendor', '')).upper()),
            Decimal('0'),
        )
        fees  = Decimal(str(getattr(self, 'fees',     0) or 0))
        intr  = Decimal(str(getattr(self, 'interest', 0) or 0))
        fc    = Decimal(str(getattr(self, 'finance_charge', 0) or 0))
        finance = fees + intr + fc
        computed = prev + chrg + finance - pays - cred
        gap = abs(computed - new)
        today = datetime.date.today().isoformat()
        parser_name = type(self).__name__
        pdf_name = Path(getattr(self, 'pdf_path', 'unknown')).name
        entry = (
            f"\n### Parser tie-out failure — {parser_name} — {today}\n"
            f"- **PDF**: `{pdf_name}`\n"
            f"- **Gap**: ${gap:.2f} (computed={computed:.2f}, statement={new:.2f})\n"
            f"- **Breakdown**: prev={prev} charges={chrg} finance={finance} "
            f"payments={pays} credits={cred}\n"
            f"- **Action**: Review `{parser_name}` extraction and add to "
            f"`_FINANCE_CHARGE_LABELS` or skip-list as needed.\n"
        )
        try:
            with open(roadmap, 'r') as f:
                content = f.read()
            marker = '## Open: Needs Root Cause Fix'
            if entry.strip() in content:
                return  # already logged (same parser + date)
            insert_at = content.find(marker)
            if insert_at == -1:
                with open(roadmap, 'a') as f:
                    f.write(entry)
            else:
                end_of_marker = content.find('\n', insert_at) + 1
                new_content = content[:end_of_marker] + entry + content[end_of_marker:]
                with open(roadmap, 'w') as f:
                    f.write(new_content)
            print(f"  📋 Parser bug logged → REFACTORING_ROADMAP.md", file=sys.stderr)
        except Exception:
            pass  # roadmap logging is best-effort; never block reconciliation

    def _try_vision_fallback(self):
        """
        If self-check failed, re-extract from page images via Claude Vision
        and OVERWRITE self.previous_balance, self.new_balance,
        self.total_payments, self.payments, self.credits, self.charges.

        Silent no-op when self-check already passed.

        For text-based PDFs: first tries _try_recover_balance() to close the gap
        without any API call.  Only falls through to Vision (or stops) if recovery
        fails.

        For scanned/image PDFs (pdftotext returned very little): Vision is the
        appropriate fallback.
        """
        if self._tied_out():
            return  # parse already succeeded

        if self._pdf_is_text_based():
            # Try to recover before giving up or calling Vision.
            if self._try_recover_balance():
                return  # recovered — no Vision needed

            # Recovery failed → parser bug, surface it clearly.
            print(
                "  ⚠ pdftotext parse did not tie out on a text-based PDF — "
                "this is likely a parser bug, not a scanned page.",
                file=sys.stderr,
            )
            self._tie_out_diagnostic()
            self._log_parser_bug_to_roadmap()
            print(
                "  ℹ  Fix the parser and re-run.  "
                "Skipping Vision fallback (it would mask the bug).",
                file=sys.stderr,
            )
            return

        # PDF appears to be scanned/image — Vision fallback is appropriate.
        # Lazy-import to avoid pulling in anthropic SDK for clean parses
        try:
            from extractors import vision_helper
        except ImportError:
            print(
                "  ⚠ Parse self-check failed (balance equation does not tie). "
                "Vision fallback module not found.",
                file=sys.stderr,
            )
            return

        ok, reason = vision_helper.is_available()
        if not ok:
            print(
                f"  ⚠ Parse self-check failed (balance equation does not tie). "
                f"Vision fallback unavailable: {reason}",
                file=sys.stderr,
            )
            print(
                f"    Set ANTHROPIC_API_KEY and install: "
                f"pip install anthropic pymupdf",
                file=sys.stderr,
            )
            return

        print(
            "  ⚠ pdftotext parse did not tie out — invoking Claude Vision "
            "fallback (this calls the Anthropic API)...",
            file=sys.stderr,
        )
        try:
            data = vision_helper.extract(self.pdf_path)
        except Exception as e:
            print(f"  ✗ Vision fallback failed: {e}", file=sys.stderr)
            return

        # Overwrite parser fields with vision-extracted data.
        # generate_report() will run unchanged from here.
        self.previous_balance = data["previous_balance"]
        self.new_balance = data["new_balance"]
        self.total_payments = data["total_payments"]
        self.payments = data["payments"]
        self.credits = data["credits"]
        self.charges = data["charges"]

        if self._tied_out():
            print(
                "  ✓ Vision fallback succeeded — data ties to the penny.",
                file=sys.stderr,
            )
        else:
            print(
                "  ⚠ Vision fallback ran but data still does not tie. "
                "Report will be generated with the best available data.",
                file=sys.stderr,
            )

    def _aggregate_by_vendor(self, transactions, date_fmt='%m/%d/%y'):
        """
        Aggregate transactions by (vendor, calendar month).
        One row per vendor per month, using the latest transaction date in that month.
        Returns list sorted by date ascending.
        Date fmt should include year (e.g. %m/%d/%y) for correct cross-year sorting.

        Vendors listed in the client's `no_aggregate_vendors` config are keyed by
        full date so each occurrence shows as a separate line.
        """
        # Pull no-aggregate lists once per call (uppercase for case-insensitive match)
        no_agg = []
        never_agg = []
        if self.client_name:
            cfg = _registry.get_config(self.client_name) or {}
            no_agg    = [v.upper() for v in cfg.get('no_aggregate_vendors', [])]
            never_agg = [v.upper() for v in cfg.get('never_aggregate_vendors', [])]

        # Key: (normalised_vendor, year, month) — or (vendor, year, month, day) for no_agg
        #      or a unique integer index for never_agg (every transaction its own row)
        totals = defaultdict(lambda: {'amount': Decimal('0'), 'count': 0, 'latest_date': None})
        _never_agg_counter = 0
        for t in transactions:
            v = self.normalize_vendor(t['vendor'])
            try:
                d = datetime.strptime(t['date'], date_fmt)
            except ValueError:
                # Fallback: try common formats
                for fmt in ('%m/%d/%y', '%m/%d/%Y', '%m/%d'):
                    try:
                        d = datetime.strptime(t['date'], fmt)
                        break
                    except ValueError:
                        d = None
                if d is None:
                    d = datetime(2000, 1, 1)
            if any(n in v.upper() for n in never_agg):
                key = (v, d.year, d.month, d.day, _never_agg_counter)
                _never_agg_counter += 1
            elif any(n in v.upper() for n in no_agg):
                key = (v, d.year, d.month, d.day)
            else:
                key = (v, d.year, d.month)
            totals[key]['amount'] += Decimal(str(t['amount']))
            totals[key]['count'] += 1
            if totals[key]['latest_date'] is None or d > totals[key]['latest_date']:
                totals[key]['latest_date'] = d

        result = []
        for key, data in totals.items():
            vendor = key[0]
            date_str = data['latest_date'].strftime(date_fmt)
            result.append({'date': date_str, 'vendor': vendor,
                           'amount': data['amount'], 'count': data['count']})
        result.sort(key=lambda x: datetime.strptime(x['date'], date_fmt))
        return result


# ═══════════════════════════════════════════════════════════════════════════════
# CREDIT CARD PARSERS
# ═══════════════════════════════════════════════════════════════════════════════

