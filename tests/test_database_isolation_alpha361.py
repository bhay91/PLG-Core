from __future__ import annotations

import hashlib
from pathlib import Path
import unittest

import legacy_app


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DB = (ROOT / "data" / "plg_core.db").resolve()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class AutomatedTestDatabaseIsolationAlpha361Tests(unittest.TestCase):
    def test_legacy_app_uses_process_disposable_database(self):
        self.assertNotEqual(legacy_app.DB_PATH, PRODUCTION_DB)
        self.assertTrue(legacy_app.DB_PATH.exists())

    def test_legacy_operational_writes_do_not_touch_production_database(self):
        before = file_hash(PRODUCTION_DB)
        with legacy_app.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO audit_logs (action, entity_type, summary)
                VALUES (?, ?, ?)
                """,
                (
                    "TEST_DATABASE_ISOLATION",
                    "TEST",
                    "Disposable process database",
                ),
            )
            connection.commit()
        self.assertEqual(file_hash(PRODUCTION_DB), before)


if __name__ == "__main__":
    unittest.main()
