"""
test_bofa_check_payees.py
--------------------------
Regression test for BankOfAmericaCheckingParser.extract_check_payees().

Previously this re-OCR'd the *same* single image (the last image on only
the first "Check images" page — later such pages were never reached
because of an early `break`) once per check lacking a manual payee, so
every unmapped check ended up with an identical, usually-wrong payee, and
OCR ran far more times than there were actual check images.

Fitz/pytesseract/PIL are faked here — no real PDF or tesseract install
needed — so this runs anywhere, including CI.
"""
import io
import unittest

import parsers.bofa as bofa_mod
from parsers.bofa import BankOfAmericaCheckingParser


class _FakePage:
    def __init__(self, text, images):
        self._text = text
        self._images = images  # list of (xref,) tuples, like real fitz pages return

    def get_text(self):
        return self._text

    def get_images(self):
        return self._images


class _FakePdf:
    def __init__(self, pages, image_bytes_by_xref):
        self._pages = pages
        self._image_bytes_by_xref = image_bytes_by_xref

    def __len__(self):
        return len(self._pages)

    def __getitem__(self, i):
        return self._pages[i]

    def extract_image(self, xref):
        return {"image": self._image_bytes_by_xref[xref]}

    def close(self):
        pass


class _FakeFitzModule:
    def __init__(self, pdf):
        self._pdf = pdf

    def open(self, path):
        return self._pdf


class _FakeImageModule:
    """Stand-in for PIL.Image — Image.open(bytesio) just returns the raw
    bytes unchanged, so the fake pytesseract can decode them directly."""

    @staticmethod
    def open(bio):
        return bio.read()


class _FakeTesseract:
    def __init__(self):
        self.call_count = 0

    def image_to_string(self, img):
        self.call_count += 1
        return img.decode()


_MISSING = object()  # sentinel: fitz/pytesseract/etc. may not exist as module
                     # attributes at all when the optional OCR deps aren't
                     # installed (the try/except ImportError in bofa.py never
                     # binds them in that case).


class ExtractCheckPayeesTest(unittest.TestCase):
    def setUp(self):
        self._orig_fitz = getattr(bofa_mod, "fitz", _MISSING)
        self._orig_pytesseract = getattr(bofa_mod, "pytesseract", _MISSING)
        self._orig_image = getattr(bofa_mod, "Image", _MISSING)
        self._orig_io = getattr(bofa_mod, "_io", _MISSING)
        self._orig_ocr_available = bofa_mod.OCR_AVAILABLE
        bofa_mod.OCR_AVAILABLE = True
        bofa_mod.Image = _FakeImageModule
        bofa_mod._io = io

    def _restore(self, name, value):
        if value is _MISSING:
            if hasattr(bofa_mod, name):
                delattr(bofa_mod, name)
        else:
            setattr(bofa_mod, name, value)

    def tearDown(self):
        self._restore("fitz", self._orig_fitz)
        self._restore("pytesseract", self._orig_pytesseract)
        self._restore("Image", self._orig_image)
        self._restore("_io", self._orig_io)
        bofa_mod.OCR_AVAILABLE = self._orig_ocr_available

    def _parser_with_fake_pdf(self, checks, pages, image_bytes_by_xref):
        p = BankOfAmericaCheckingParser.__new__(BankOfAmericaCheckingParser)
        p.checks = checks
        p.pdf_path = "fake.pdf"
        fake_pdf = _FakePdf(pages, image_bytes_by_xref)
        bofa_mod.fitz = _FakeFitzModule(fake_pdf)
        tess = _FakeTesseract()
        bofa_mod.pytesseract = tess
        return p, tess

    def test_each_unmapped_check_gets_its_own_page_image_ocr_once(self):
        # Two "Check images" pages, each with a logo image (ignored) followed
        # by the real check image (last on the page) — the two checks should
        # each get a different payee, and OCR should run exactly twice, not
        # once per check-times-page.
        pages = [
            _FakePage("Deposits and other credits ...", []),           # regular page, ignored
            _FakePage("Check images\n1001", [(9001,), (101,)]),
            _FakePage("Check images\n1002", [(9002,), (202,)]),
        ]
        image_bytes = {
            101: b"TO. ACME VENDOR",
            202: b"TO. BRAVO VENDOR",
            9001: b"",
            9002: b"",
        }
        checks = [
            {"date": "01/01/26", "check_number": "1001"},
            {"date": "01/02/26", "check_number": "1002"},
        ]
        p, tess = self._parser_with_fake_pdf(checks, pages, image_bytes)

        p.extract_check_payees()

        self.assertEqual(checks[0]["payee"], "Acme Vendor")
        self.assertEqual(checks[1]["payee"], "Bravo Vendor")
        self.assertNotEqual(checks[0]["payee"], checks[1]["payee"])
        self.assertEqual(tess.call_count, 2, "expected exactly one OCR call per check image")

    def test_manually_mapped_check_is_never_ocrd(self):
        pages = [
            _FakePage("Check images\n1001", [(101,)]),
            _FakePage("Check images\n1002", [(202,)]),
        ]
        image_bytes = {101: b"TO. ACME VENDOR", 202: b"TO. BRAVO VENDOR"}
        checks = [
            {"date": "01/01/26", "check_number": "1001", "payee": "Manually Set"},
            {"date": "01/02/26", "check_number": "1002"},
        ]
        p, tess = self._parser_with_fake_pdf(checks, pages, image_bytes)

        p.extract_check_payees()

        self.assertEqual(checks[0]["payee"], "Manually Set")
        self.assertEqual(checks[1]["payee"], "Acme Vendor")
        self.assertEqual(tess.call_count, 1, "only the unmapped check should trigger OCR")

    def test_all_checks_already_mapped_skips_ocr_entirely(self):
        pages = [_FakePage("Check images\n1001", [(101,)])]
        image_bytes = {101: b"TO. ACME VENDOR"}
        checks = [{"date": "01/01/26", "check_number": "1001", "payee": "Manually Set"}]
        p, tess = self._parser_with_fake_pdf(checks, pages, image_bytes)

        p.extract_check_payees()

        self.assertEqual(checks[0]["payee"], "Manually Set")
        self.assertEqual(tess.call_count, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
