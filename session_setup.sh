#!/usr/bin/env bash
# session_setup.sh — One-shot Claude session initializer for Bookkeeping
#
# Usage:
#   export GITHUB_PAT_BOOKKEEPING=ghp_...
#   source /tmp/Bookkeeping/session_setup.sh
#
# Or in one line (when sourced, env var is set for the whole session):
#   GITHUB_PAT_BOOKKEEPING=ghp_... source session_setup.sh
#
# Must be SOURCED (not executed) so that exported env vars persist in the shell.

set -e

# ── 1. Validate PAT ──────────────────────────────────────────────────────────
if [ -z "$GITHUB_PAT_BOOKKEEPING" ]; then
  echo "ERROR: GITHUB_PAT_BOOKKEEPING is not set." >&2
  echo "  Ask Sarah for the PAT and run:" >&2
  echo "  export GITHUB_PAT_BOOKKEEPING=ghp_..." >&2
  echo "  source /tmp/Bookkeeping/session_setup.sh" >&2
  return 1 2>/dev/null || exit 1
fi

PAT="$GITHUB_PAT_BOOKKEEPING"

# ── 2. Clone or pull Bookkeeping ─────────────────────────────────────────────
echo "📦 Bookkeeping repo..."
if [ -d /tmp/Bookkeeping/.git ]; then
  git -C /tmp/Bookkeeping remote set-url origin \
    "https://x-access-token:${PAT}@github.com/sarah-levine/Bookkeeping.git"
  git -C /tmp/Bookkeeping pull --rebase --quiet
  echo "   ✓ pulled"
else
  git clone --quiet \
    "https://x-access-token:${PAT}@github.com/sarah-levine/Bookkeeping.git" \
    /tmp/Bookkeeping
  git -C /tmp/Bookkeeping remote set-url origin \
    "https://github.com/sarah-levine/Bookkeeping.git"
  echo "   ✓ cloned"
fi

# ── 3. Clone or pull Bookkeeping-clients ─────────────────────────────────────
echo "📦 Bookkeeping-clients repo..."
if [ -d /tmp/Bookkeeping-clients/.git ]; then
  git -C /tmp/Bookkeeping-clients remote set-url origin \
    "https://x-access-token:${PAT}@github.com/sarah-levine/Bookkeeping-clients.git"
  git -C /tmp/Bookkeeping-clients pull --rebase --quiet
  echo "   ✓ pulled"
else
  git clone --quiet \
    "https://x-access-token:${PAT}@github.com/sarah-levine/Bookkeeping-clients.git" \
    /tmp/Bookkeeping-clients
  echo "   ✓ cloned"
fi

# ── 4. Set env vars ───────────────────────────────────────────────────────────
export BOOKKEEPING_CLIENTS_DIR=/tmp/Bookkeeping-clients
export GOOGLE_SHEETS_CREDENTIALS
GOOGLE_SHEETS_CREDENTIALS=$(cat /tmp/Bookkeeping-clients/sheets_credentials.json 2>/dev/null || echo "")

# ── 5. Install Python dependencies ───────────────────────────────────────────
echo "🐍 Installing Python dependencies..."
pip install --break-system-packages -q \
  pdfplumber pypdf anthropic pymupdf \
  google-auth google-auth-httplib2 google-api-python-client \
  2>/dev/null || true
echo "   ✓ done"

# ── 6. Git identity ───────────────────────────────────────────────────────────
git config --global user.email "bookkeeping@sarah-levine.com"
git config --global user.name "Bookkeeping Bot"

# ── 7. Set PAT on remote so pushes work ───────────────────────────────────────
git -C /tmp/Bookkeeping remote set-url origin \
  "https://x-access-token:${PAT}@github.com/sarah-levine/Bookkeeping.git"
git -C /tmp/Bookkeeping-clients remote set-url origin \
  "https://x-access-token:${PAT}@github.com/sarah-levine/Bookkeeping-clients.git"

# ── 8. Copy fixtures manifest and PDFs from Bookkeeping-clients ──────────────
if [ -f /tmp/Bookkeeping-clients/fixtures_manifest.json ]; then
  cp /tmp/Bookkeeping-clients/fixtures_manifest.json /tmp/Bookkeeping/tests/fixtures_manifest.json
  # Also copy fixture PDFs so tests can reference them locally
  if [ -d /tmp/Bookkeeping-clients/fixtures ]; then
    cp -r /tmp/Bookkeeping-clients/fixtures /tmp/Bookkeeping/tests/
  fi
  echo "   ✓ fixtures_manifest.json + fixture PDFs loaded"
else
  echo "   ⚠ No fixtures_manifest.json in Bookkeeping-clients"
fi

# ── 9. Run repair_logs.py if it exists (lives in Bookkeeping-clients) ────────
if [ -f /tmp/Bookkeeping-clients/repair_logs.py ]; then
  echo "🔧 Running repair_logs.py..."
  BOOKKEEPING_DIR=/tmp/Bookkeeping python3 /tmp/Bookkeeping-clients/repair_logs.py && echo "   ✓ logs clean"
fi

# ── 9. Summary ───────────────────────────────────────────────────────────────
echo ""
echo "✅ Session ready"
echo "   Bookkeeping:         /tmp/Bookkeeping"
echo "   Bookkeeping-clients: /tmp/Bookkeeping-clients"
echo "   BOOKKEEPING_CLIENTS_DIR: $BOOKKEEPING_CLIENTS_DIR"
if [ -n "$GOOGLE_SHEETS_CREDENTIALS" ]; then
  echo "   GOOGLE_SHEETS_CREDENTIALS: ✓ loaded from sheets_credentials.json"
else
  echo "   GOOGLE_SHEETS_CREDENTIALS: ✗ not found (sheet backfill will fail)"
fi
echo ""
