"""
test_statement_registry.py
----------------------------
Regression coverage for parsers/registry.py — the single source of truth for
statement-type registration that replaced four separately hand-maintained
copies (STATEMENT_TYPE_LABELS, parser_map, manual_statement_entry.py's own
PARSER_BY_TYPE, and three scattered credit-card-membership tuples).

Two things are checked:
  1. Every key a parser module registers is a valid entry in
     clients/_schema.json's statement_types enum — so a new parser that
     forgets the schema edit fails CI immediately instead of silently
     hitting the "skip with a warning" behavior in ClientRegistry._load().
  2. Adding a brand-new statement type only requires one register() call —
     proven with a synthetic format, no real bank PDF needed.
"""
import json
import unittest
from pathlib import Path

import parsers  # noqa: F401 — importing the package triggers every parser
                 # module's register() call, fully populating the registry.
from parsers import registry


class SchemaRegistryParityTest(unittest.TestCase):
    def test_every_registered_key_is_in_the_schema_enum(self):
        schema_path = Path(__file__).parent.parent / "clients" / "_schema.json"
        schema = json.loads(schema_path.read_text())
        # statement_types.items is an anyOf: one branch is the flat enum of
        # base parser keys, another is a pattern for per-cardholder variants
        # (e.g. bmo_credit_<cardholder>) that never appear in the parser
        # registry itself (registry.keys() only has base types — cardholder
        # is a log-key override applied downstream, not a registered parser).
        enum = set()
        for branch in schema["properties"]["statement_types"]["items"]["anyOf"]:
            enum |= set(branch.get("enum", []))

        registered = set(registry.keys())
        missing = registered - enum
        self.assertFalse(
            missing,
            f"Registered statement type(s) {sorted(missing)} missing from "
            f"clients/_schema.json's statement_types enum — a client config "
            f"using one would be silently skipped (see parsers/base.py's "
            f"ClientRegistry._load()).",
        )


class RegistrySyntheticFormatTest(unittest.TestCase):
    """Proves a new statement type only needs one register() call — no real
    PDF fixture required to demonstrate the mechanism works."""

    def setUp(self):
        self._saved = dict(registry._REGISTRY)

    def tearDown(self):
        registry._REGISTRY.clear()
        registry._REGISTRY.update(self._saved)

    def test_new_format_only_needs_one_register_call(self):
        class _FakeDiscoverParser:
            pass

        registry.register(
            "discover_business", "Discover Business Credit Card",
            _FakeDiscoverParser, is_credit_card=True,
        )

        self.assertIn("discover_business", registry.keys())
        self.assertEqual(registry.labels()["discover_business"],
                         "Discover Business Credit Card")
        self.assertIs(registry.parser_by_type()["discover_business"], _FakeDiscoverParser)
        self.assertIn("discover_business", registry.credit_card_keys())
        # No load_from_dict() on the fake class -> not manual-entry-supported
        self.assertNotIn("discover_business", registry.manual_entry_parser_map())

    def test_manual_entry_support_is_derived_from_load_from_dict(self):
        class _FakeManualCapableParser:
            @classmethod
            def load_from_dict(cls, data):
                return cls()

        registry.register(
            "fake_manual_type", "Fake Manual-Entry-Capable Type",
            _FakeManualCapableParser,
        )

        self.assertIn("fake_manual_type", registry.manual_entry_parser_map())


if __name__ == "__main__":
    unittest.main(verbosity=2)
