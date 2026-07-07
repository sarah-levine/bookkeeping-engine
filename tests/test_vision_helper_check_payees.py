"""
test_vision_helper_check_payees.py
-------------------------------------
Unit tests for extractors.vision_helper.extract_check_payees() — the
check-image-payee counterpart to the existing balance-recovery extract().

No real Anthropic API key or network access needed: is_available() and the
internal Claude call are monkeypatched.
"""
import unittest
from unittest.mock import patch

import extractors.vision_helper as vh


class ExtractCheckPayeesTest(unittest.TestCase):
    def test_raises_when_unavailable(self):
        with patch.object(vh, "is_available", return_value=(False, "ANTHROPIC_API_KEY not set")):
            with self.assertRaises(RuntimeError):
                vh.extract_check_payees([b"fake-png-bytes"])

    def test_raises_with_no_images(self):
        with patch.object(vh, "is_available", return_value=(True, "ready")):
            with self.assertRaises(RuntimeError):
                vh.extract_check_payees([])

    def test_raises_when_over_the_batch_limit(self):
        too_many = [b"img"] * (vh.MAX_PAGES_PER_REQUEST + 1)
        with patch.object(vh, "is_available", return_value=(True, "ready")):
            with self.assertRaises(RuntimeError):
                vh.extract_check_payees(too_many)

    def test_happy_path_returns_payees_in_order(self):
        with patch.object(vh, "is_available", return_value=(True, "ready")), \
             patch.object(vh, "_call_claude_vision_for_check_payees",
                          return_value='{"payees": ["Acme Vendor", "Bravo Vendor"]}'):
            result = vh.extract_check_payees([b"img1", b"img2"])
        self.assertEqual(result, ["Acme Vendor", "Bravo Vendor"])

    def test_code_fences_are_stripped(self):
        with patch.object(vh, "is_available", return_value=(True, "ready")), \
             patch.object(vh, "_call_claude_vision_for_check_payees",
                          return_value='```json\n{"payees": ["Acme Vendor"]}\n```'):
            result = vh.extract_check_payees([b"img1"])
        self.assertEqual(result, ["Acme Vendor"])

    def test_short_response_padded_with_empty_strings(self):
        # Vision returned fewer entries than images provided — pad, don't crash.
        with patch.object(vh, "is_available", return_value=(True, "ready")), \
             patch.object(vh, "_call_claude_vision_for_check_payees",
                          return_value='{"payees": ["Acme Vendor"]}'):
            result = vh.extract_check_payees([b"img1", b"img2", b"img3"])
        self.assertEqual(result, ["Acme Vendor", "", ""])

    def test_long_response_truncated(self):
        with patch.object(vh, "is_available", return_value=(True, "ready")), \
             patch.object(vh, "_call_claude_vision_for_check_payees",
                          return_value='{"payees": ["A", "B", "C", "D"]}'):
            result = vh.extract_check_payees([b"img1", b"img2"])
        self.assertEqual(result, ["A", "B"])

    def test_non_json_response_raises(self):
        with patch.object(vh, "is_available", return_value=(True, "ready")), \
             patch.object(vh, "_call_claude_vision_for_check_payees",
                          return_value="not json at all"):
            with self.assertRaises(RuntimeError):
                vh.extract_check_payees([b"img1"])

    def test_missing_payees_key_raises(self):
        with patch.object(vh, "is_available", return_value=(True, "ready")), \
             patch.object(vh, "_call_claude_vision_for_check_payees",
                          return_value='{"something_else": []}'):
            with self.assertRaises(RuntimeError):
                vh.extract_check_payees([b"img1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
