"""
test_config_and_logs.py
-----------------------
Regression unit tests for the config/registry/logs plumbing that was reworked
when client data was externalized to the private repo:

  - log_utils.get_logs_dir() resolution order
  - ClientRegistry schema validation (raise on invalid config)
  - ClientRegistry skips non-dict JSON (recon_log.json is a list)
  - ClientRegistry.payroll_dispatch() built from client configs

These use only synthetic temp files — no PDFs, no Drive, no network — so they
run anywhere, including CI.

Run:
    python3 -m pytest tests/test_config_and_logs.py -v
    # or without pytest:
    python3 tests/test_config_and_logs.py
"""

import json
import os
import sys
import tempfile
import shutil
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import log_utils  # noqa: E402
from parsers.base import ClientRegistry  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────

class _Env:
    """Context manager: set/clear env vars and restore them afterward."""
    def __init__(self, **kv):
        self._kv = kv
        self._saved = {}

    def __enter__(self):
        for k, v in self._kv.items():
            self._saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, old in self._saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


def _write(d: Path, name: str, obj) -> None:
    (d / name).write_text(json.dumps(obj))


# ── get_logs_dir resolution ───────────────────────────────────────────────────

def test_get_logs_dir_env_override():
    """BOOKKEEPING_LOGS_DIR wins when it exists."""
    d = Path(tempfile.mkdtemp())
    try:
        with _Env(BOOKKEEPING_LOGS_DIR=str(d), BOOKKEEPING_CLIENTS_DIR=None):
            assert log_utils.get_logs_dir() == d, log_utils.get_logs_dir()
        print("PASS  test_get_logs_dir_env_override")
    finally:
        shutil.rmtree(d)


def test_get_logs_dir_prefers_clients_dir():
    """With no logs-dir env, an existing BOOKKEEPING_CLIENTS_DIR is used."""
    d = Path(tempfile.mkdtemp())
    try:
        with _Env(BOOKKEEPING_LOGS_DIR=None, BOOKKEEPING_CLIENTS_DIR=str(d)):
            assert log_utils.get_logs_dir() == d, log_utils.get_logs_dir()
        print("PASS  test_get_logs_dir_prefers_clients_dir")
    finally:
        shutil.rmtree(d)


def test_get_logs_dir_fallback_repo_root():
    """With nothing configured and no private home dir, fall back to repo root."""
    fake_home = Path(tempfile.mkdtemp())  # no ~/.bookkeeping/clients inside
    try:
        with _Env(BOOKKEEPING_LOGS_DIR=None, BOOKKEEPING_CLIENTS_DIR=None,
                  HOME=str(fake_home)):
            assert log_utils.get_logs_dir() == log_utils.REPO_DIR, log_utils.get_logs_dir()
        print("PASS  test_get_logs_dir_fallback_repo_root")
    finally:
        shutil.rmtree(fake_home)


# ── ClientRegistry schema validation ──────────────────────────────────────────

def _valid_cfg():
    return {"client_name": "Acme Inc", "canonical_name": "ACME INC",
            "statement_types": ["bofa_checking"]}


def test_registry_accepts_valid_config():
    d = Path(tempfile.mkdtemp())
    try:
        _write(d, "acme.json", _valid_cfg())
        reg = ClientRegistry(clients_dir=str(d))
        assert "ACME INC" in reg._configs
        print("PASS  test_registry_accepts_valid_config")
    finally:
        shutil.rmtree(d)


def test_registry_accepts_unknown_statement_type():
    """Unknown statement_types values must be accepted — enum constraint was removed.
    Runtime parser matching handles validation; the schema no longer gatekeeps it."""
    try:
        import jsonschema  # noqa: F401
    except ImportError:
        raise unittest.SkipTest("jsonschema not installed — validation is skipped")
    d = Path(tempfile.mkdtemp())
    try:
        cfg = _valid_cfg()
        cfg["statement_types"] = ["not_a_real_format"]
        _write(d, "cfg.json", cfg)
        reg = ClientRegistry(clients_dir=str(d))
        assert "ACME INC" in reg._configs, "unknown statement_type should not block the config"
        print("PASS  test_registry_accepts_unknown_statement_type")
    finally:
        shutil.rmtree(d)


def test_registry_rejects_missing_required_fields():
    """Configs missing client_name or canonical_name must fail schema validation."""
    try:
        import jsonschema  # noqa: F401
    except ImportError:
        raise unittest.SkipTest("jsonschema not installed — validation is skipped")
    d = Path(tempfile.mkdtemp())
    try:
        bad = {"canonical_name": "ACME INC"}  # missing client_name
        _write(d, "bad.json", bad)
        try:
            ClientRegistry(clients_dir=str(d))
        except ValueError as e:
            assert "client_name" in str(e) or "schema validation" in str(e)
            print("PASS  test_registry_rejects_missing_required_fields")
            return
        assert False, "expected ValueError for config missing required field"
    finally:
        shutil.rmtree(d)


def test_registry_skips_non_dict_json():
    """recon_log.json (a JSON list) lives in the clients dir — must not crash."""
    d = Path(tempfile.mkdtemp())
    try:
        _write(d, "acme.json", _valid_cfg())
        _write(d, "recon_log.json", [{"client": "X"}, {"client": "Y"}])   # list, not dict
        _write(d, "digest_config.json", {"email": {}})                     # dict, no client_name
        reg = ClientRegistry(clients_dir=str(d))
        assert list(reg._configs.keys()) == ["ACME INC"], list(reg._configs.keys())
        print("PASS  test_registry_skips_non_dict_json")
    finally:
        shutil.rmtree(d)


# ── payroll dispatch ──────────────────────────────────────────────────────────

def test_lookup_account_ending():
    """account_endings maps a card last-4 to (client, account_type)."""
    d = Path(tempfile.mkdtemp())
    try:
        cfg = _valid_cfg()
        cfg["statement_types"] = ["chase_sapphire", "chase_ink"]
        cfg["account_endings"] = {"3551": "chase_sapphire", "9087": "chase_ink"}
        _write(d, "acme.json", cfg)
        reg = ClientRegistry(clients_dir=str(d))
        assert reg.lookup_account_ending("3551") == ("ACME INC", "chase_sapphire")
        assert reg.lookup_account_ending("9087") == ("ACME INC", "chase_ink")
        assert reg.lookup_account_ending("0000") is None
        assert reg.lookup_account_ending(None) is None
        print("PASS  test_lookup_account_ending")
    finally:
        shutil.rmtree(d)


def test_payroll_dispatch_from_configs():
    """payroll_dispatch() maps payroll_key → (payroll_format, filename)."""
    d = Path(tempfile.mkdtemp())
    try:
        c1 = _valid_cfg()
        c1.update(payroll_key="acme", payroll_format="adp_payroll_details")
        _write(d, "acme.json", c1)
        c2 = {"client_name": "Beta LLC", "canonical_name": "BETA LLC",
              "payroll_key": "beta", "payroll_format": "adp_payroll_1099"}
        _write(d, "beta.json", c2)
        # a config with no payroll_key must be omitted
        _write(d, "gamma.json", {"client_name": "Gamma", "canonical_name": "GAMMA"})
        reg = ClientRegistry(clients_dir=str(d))
        dispatch = reg.payroll_dispatch()
        assert dispatch.get("acme") == ("adp_payroll_details", "acme.json"), dispatch
        assert dispatch.get("beta") == ("adp_payroll_1099", "beta.json"), dispatch
        assert "gamma" not in dispatch and len(dispatch) == 2, dispatch
        print("PASS  test_payroll_dispatch_from_configs")
    finally:
        shutil.rmtree(d)


# ── pytest integration ────────────────────────────────────────────────────────
try:
    import pytest  # noqa: F401
except ImportError:
    pass


# ── runner ────────────────────────────────────────────────────────────────────

TESTS = [
    test_get_logs_dir_env_override,
    test_get_logs_dir_prefers_clients_dir,
    test_get_logs_dir_fallback_repo_root,
    test_registry_accepts_valid_config,
    test_registry_accepts_unknown_statement_type,
    test_registry_rejects_missing_required_fields,
    test_registry_skips_non_dict_json,
    test_lookup_account_ending,
    test_payroll_dispatch_from_configs,
]


def main():
    failures = skips = 0
    for t in TESTS:
        try:
            t()
        except unittest.SkipTest as e:
            skips += 1
            print(f"SKIP  {t.__name__}: {e}")
        except Exception as e:
            failures += 1
            print(f"FAIL  {t.__name__}: {e}")
    summary = "All tests passed." if not failures else f"{failures} failure(s)."
    if skips:
        summary += f" {skips} skipped."
    print(f"\n{summary}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
