"""
test_report_helper_imports.py
--------------------------------
Regression test for a real bug class found 2026-07-07: `parsers/northern_trust.py`
and `parsers/bmo.py` both did `from parsers.report import *`, but Python's
`import *` excludes names starting with an underscore unless the source
module defines `__all__` (`parsers/report.py` doesn't). Every report-section
helper (`_report_header`, `_balance_check`, `_deposits_section`, etc.) is
underscore-prefixed, so both modules silently had none of them — a
`NameError` waiting to happen the moment `generate_report()` actually ran
(never caught before because Northern Trust needs OCR, which isn't available
in this sandbox, and nobody had exercised BMO's checking report far enough
to hit the missing `_deposits_section`/`_adp_section`/`_checks_section`/
`_individual_section` calls specifically).

This scans every parser module for `_name(` call-sites that match a real
`parsers.report` helper name, and asserts the module's namespace actually
resolves that name — catching a silent `import *` gap in any parser, not
just the two found live.
"""
import ast
import importlib
import unittest
from pathlib import Path

import parsers.report as report_mod

_REPORT_HELPERS = frozenset(
    n for n in dir(report_mod)
    if n.startswith('_') and callable(getattr(report_mod, n))
)

_PARSERS_DIR = Path(__file__).parent.parent / 'parsers'
_PARSER_MODULES = [
    f'parsers.{p.stem}' for p in _PARSERS_DIR.glob('*.py')
    if p.stem not in ('__init__', 'report', 'base', 'registry', 'pdf_utils',
                      'vendor_normalize', 'ocr_support')
]


def _called_report_helpers(py_path):
    """Names in this file that are both called as a function and match a
    real parsers.report helper name (via static AST parse, not import) —
    excluding names the file defines locally anywhere (e.g. a nested
    function inside generate_report() that deliberately shadows the
    module-level helper of the same name; Python resolves that via normal
    scoping without needing a module-level import at all)."""
    tree = ast.parse(py_path.read_text())
    called, locally_defined = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            locally_defined.add(node.name)
    return (called & _REPORT_HELPERS) - locally_defined


class ReportHelperImportsResolveTest(unittest.TestCase):
    def test_every_called_report_helper_resolves_in_each_parser_module(self):
        for mod_name in _PARSER_MODULES:
            py_path = _PARSERS_DIR / f"{mod_name.rsplit('.', 1)[-1]}.py"
            called = _called_report_helpers(py_path)
            if not called:
                continue
            mod = importlib.import_module(mod_name)
            missing = [n for n in called if n not in dir(mod)]
            self.assertFalse(
                missing,
                f"{mod_name} calls {missing} but does not import them — "
                f"likely relying on `from parsers.report import *`, which "
                f"silently excludes underscore-prefixed names"
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
