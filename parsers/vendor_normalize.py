"""Single home for vendor / transaction-description normalization.

Two tiers, called by every parser — nothing reimplements cleaning locally:

  • STANDARD (this module) — generic, client-agnostic cleaning: trailing
    "<CITY> <ST>" location tails, state codes, phone numbers, URL fragments,
    store numbers, payment-processor prefixes (TST*/SQ*/SP/PY*), and
    marketplace suffixes. No client-identifying literals (city names, vendors,
    people) live here, so this code is safe for the public repo.

  • CLIENT (clients/*.json via ClientRegistry.normalize_vendor in base.py) —
    per-client `vendor_rules` and `description_strip_suffixes`. Anything that
    names a specific client, vendor, person, or place belongs here, in the
    private client configs — never hardcoded above.

`strip_client_suffixes` is the shared bridge: parsers and the auto-cleaner both
use it to apply a client's configured `description_strip_suffixes`. The standard
cleaner already removes generic "<CITY> <ST>" tails algorithmically (no city
names hardcoded); `description_strip_suffixes` remains for tails the generic
rule can't safely catch (e.g. a city with no store-number boundary) or for
non-location trailing tokens like a cardholder name.
"""
import re

# Trailing two-letter tokens stripped as US state codes.
US_STATE_CODES = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI', 'IA',
    'ID', 'IL', 'IN', 'KS', 'KY', 'LA', 'MA', 'MD', 'ME', 'MI', 'MN', 'MO',
    'MS', 'MT', 'NC', 'ND', 'NE', 'NH', 'NJ', 'NM', 'NV', 'NY', 'OH', 'OK',
    'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VA', 'VT', 'WA', 'WI',
    'WV', 'WY', 'DC',
}

# Raw descriptions that aren't real vendors — don't prompt to name these.
VENDOR_PROMPT_BLOCKLIST = {
    'STATEMENT CREDIT',
    'PAYMENT - THANK YOU',
    'PAYMENT THANK YOU',
    'AUTOMATIC PAYMENT - THANK YOU',
    'INTEREST CHARGE',
    'INTEREST CHARGED',
    'PURCHASE *FINANCE CHARGE*',
    'FINANCE CHARGE',
    'LATE PAYMENT FEE',
    'RETURNED PAYMENT FEE',
    'ANNUAL FEE',
}


def strip_client_suffixes(desc, suffixes):
    """CLIENT tier: strip configured trailing location/cardholder noise.

    `suffixes` comes from a client config's `description_strip_suffixes`.
    Case-insensitive; applied in order; safe to call with None/[].
    """
    d = desc
    for suffix in suffixes or []:
        d = re.sub(r'\s+' + re.escape(suffix) + r'\s*$', '', d,
                   flags=re.IGNORECASE).strip()
    return d


def auto_clean_vendor(raw, strip_suffixes=None):
    """STANDARD tier: heuristic auto-cleaner for an unmatched description.

    Strips generic credit-card/statement noise and title-cases the result.
    Client-specific trailing tokens (e.g. a city that dominates one client's
    statements) are supplied via `strip_suffixes` from that client's
    `description_strip_suffixes` — they are NOT hardcoded here.

    Returns (cleaned_display_name, suggested_contains_key); the contains_key is
    an UPPERCASE core suitable for a vendor_rules entry.
    """
    s = raw.strip()

    # 1) Strip a trailing "<CITY> <ST>" location tail. The two-letter state
    #    code is a reliable anchor; the city is the 1-3 alphabetic words just
    #    before it. The city is only removed when a store-number / reference
    #    token (one containing a digit or '#') separates it from the vendor
    #    name — otherwise we can't tell city words from vendor-name words, so
    #    e.g. "MDC*PARKING LOT NJ" keeps "PARKING LOT". No city names are
    #    hardcoded here, so this stays public-repo safe.
    parts = s.split()
    if len(parts) > 1 and parts[-1].upper() in US_STATE_CODES:
        parts = parts[:-1]  # drop the state code
        city = []
        while (len(parts) - len(city)) > 1 and len(city) < 3 \
                and parts[-1 - len(city)].isalpha():
            city.append(parts[-1 - len(city)])
        if city:
            before = parts[len(parts) - len(city) - 1]
            if any(ch.isdigit() for ch in before) or '#' in before:
                parts = parts[:len(parts) - len(city)]
    s = ' '.join(parts)

    # 2) Strip trailing phone numbers (800-XXX-XXXX, 8XX-XXX-XXXX, numeric runs)
    s = re.sub(r'\s+\d{3}[-.]?\d{3}[-.]?\d{4}\s*$', '', s)
    s = re.sub(r'\s+\d{10,}\s*$', '', s)

    # 3) Strip trailing URL-ish fragments (Amzn.com/bill, BRAND., etc.)
    s = re.sub(r'\s+[A-Za-z][A-Za-z0-9]*\.(com|net|org)(/\S*)?\s*$', '', s,
               flags=re.IGNORECASE)
    s = re.sub(r'\s+[A-Z]+\.\s*$', '', s)

    # 4) CLIENT tier: strip configured location/cardholder suffixes (e.g. a
    #    city name) — data-driven via description_strip_suffixes, not hardcoded.
    s = strip_client_suffixes(s, strip_suffixes)

    # 5) Strip trailing store numbers (#1234, 00012345, ST5920, WHSE#1267)
    s = re.sub(r'\s+#?\d{3,}\s*$', '', s)
    s = re.sub(r'\s+ST\d{3,}\s*$', '', s, flags=re.IGNORECASE)
    s = re.sub(r'#\d{3,}\s*$', '', s)  # glued form, e.g. "WHSE#1267"

    # 6) Strip trailing single-token location-ish suffixes
    s = re.sub(r'\s+(SJC|ECOMM|LOCAL|PMTS|MKTPLACE|MKTPL)\s*$', '', s,
               flags=re.IGNORECASE)
    s = re.sub(r'\s+#?\d{3,}\s*$', '', s)  # may expose another store number

    # 6b) Re-strip a trailing state code exposed by store-number removal, e.g.
    #     "SALONCENTRIC CA ST5920 SAN JOSE CA" -> "SALONCENTRIC CA" -> drop CA.
    tail = s.split()
    if len(tail) > 1 and tail[-1].upper() in US_STATE_CODES:
        s = ' '.join(tail[:-1])

    # 7) Strip leading payment-processor prefixes (SP, TST*, SQ *, MDC*, etc.)
    s = re.sub(r'^(SP\s+|TST\*|SQ\s*\*|MDC\*|SQU\*|PY\s*\*)', '', s,
               flags=re.IGNORECASE)

    # 8) Collapse whitespace and stray punctuation
    s = re.sub(r'\s+', ' ', s).strip(' *_-')

    contains_key = s.upper()

    # Title-case for display, preserving already-mixed-case tokens
    # (so "PG&E" stays "PG&E"; "PRIMO BRANDS" becomes "Primo Brands").
    def smart_title(token):
        if any(c.islower() for c in token):
            return token
        return token.capitalize()
    display = ' '.join(smart_title(t) for t in s.split())

    return display, contains_key
