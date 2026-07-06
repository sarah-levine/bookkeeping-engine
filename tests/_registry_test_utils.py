"""
_registry_test_utils.py
------------------------
Shared helper for tests that need parsers.base._registry to reliably resolve
the repo's clients/example_client.json ("ACME INC").

parsers.base._registry is a module-level singleton built once, at import
time, from log_utils.get_clients_dir() — which prefers a private clients
directory (BOOKKEEPING_CLIENTS_DIR, or ~/.bookkeeping/clients) over the
repo-local clients/ fallback whenever one exists. On a real bookkeeper's
machine that private directory always exists, so the singleton never sees
"ACME INC" at all — any test that relies on it directly (rather than
building its own scoped ClientRegistry) fails deterministically there,
which pytest tests/test_aggregations.py::AmexAggregation and
tests/test_vendor_normalize.py::ClientRules used to do.

Use install_example_registry()/restore_registry() in setUp/tearDown to pin
the singleton to the repo's own clients/ dir for the duration of a test,
regardless of what's on the machine running it.
"""
from pathlib import Path

import parsers.base as _base_mod
from parsers.base import ClientRegistry

EXAMPLE_CLIENTS_DIR = Path(__file__).resolve().parent.parent / "clients"


def install_example_registry() -> ClientRegistry:
    """Point parsers.base._registry at the repo's clients/ dir. Returns the
    previous registry so the caller can restore it in tearDown."""
    previous = _base_mod._registry
    _base_mod._registry = ClientRegistry(clients_dir=str(EXAMPLE_CLIENTS_DIR))
    return previous


def restore_registry(previous: ClientRegistry) -> None:
    _base_mod._registry = previous
