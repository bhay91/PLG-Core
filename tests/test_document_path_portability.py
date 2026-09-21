from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from plg_core.documents.paths import (
    portable_manifest_path,
    resolve_manifest_path,
)


class DocumentPathPortabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-document-paths-")
        self.root = Path(self.temp.name) / "documents"
        self.root.mkdir()
        self.env = patch.dict(os.environ, {"PPS_DOCUMENT_ROOT": str(self.root)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_supported_manifest_path_forms_resolve_under_configured_root(self):
        expected = self.root / "Customers" / "Synthetic" / "quote.pdf"
        for supplied in (
            expected,
            "documents/Customers/Synthetic/quote.pdf",
            "Customers/Synthetic/quote.pdf",
        ):
            with self.subTest(supplied=supplied):
                self.assertEqual(resolve_manifest_path(supplied), expected.resolve())

    def test_traversal_and_outside_absolute_paths_are_rejected(self):
        outside = Path(self.temp.name) / "outside.pdf"
        for supplied in ("../outside.pdf", outside):
            with self.subTest(supplied=supplied), self.assertRaises(ValueError):
                resolve_manifest_path(supplied)

    def test_generated_path_serializes_to_portable_documents_prefix(self):
        generated = self.root / "Customers" / "Synthetic" / "invoice.pdf"
        self.assertEqual(
            portable_manifest_path(generated),
            "documents/Customers/Synthetic/invoice.pdf",
        )
        self.assertNotIn(str(Path.home()), portable_manifest_path(generated))


if __name__ == "__main__":
    unittest.main()
