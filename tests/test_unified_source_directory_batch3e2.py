from __future__ import annotations

from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import legacy_app
from plg_core.database.migrations import run_migrations
from plg_core.sources.routes import add_job_source
from plg_core.sources.service import create_source, list_sources_for_context


class UnifiedSourceDirectoryBatch3E2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-3e2-")
        self.db_path = Path(self.temp.name) / "test.db"
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.db_patch.start()
        legacy_app.initialize_database()
        run_migrations()
        with closing(self.connection()) as connection:
            rows = [
                ('Amazon','GLOBAL','automotive',50), ('eBay','GLOBAL','automotive',40),
                ('Miami Star','GLOBAL','machine',60), ('OEM Parts Online','GLOBAL','automotive',50),
                ('FCP Euro','UK','automotive',70), ('John Deere Parts Catalog','JDM','machine',90),
                ('CAT SIS','UNKNOWN','Heavy Equipment',90), ('Worldpac','GLOBAL','automotive',80),
                ('7zap','GLOBAL','automotive',70), ('General Research','GLOBAL','',0),
            ]
            for display_name, market, category, priority in rows:
                if not connection.execute("SELECT 1 FROM connector_profiles WHERE display_name=?", (display_name,)).fetchone():
                    connection.execute("INSERT INTO connector_profiles(connector_key,display_name,category,trust_level,launch_url,connector_type,is_enabled,sort_order,manufacturer_applicability,asset_category_applicability,market_applicability,source_priority,is_default,source_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (display_name.lower().replace(' ', '-'), display_name, 'Synthetic', 'NEEDS_REVIEW', 'https://example.test/'+display_name.lower().replace(' ', '-'), 'CATALOG', 1, 0, 'John Deere' if display_name == 'John Deere Parts Catalog' else ('CAT' if display_name == 'CAT SIS' else ''), category, market, priority, 1, 'SUPPLIER'))
            connection.commit()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def connection(self):
        return legacy_app.get_connection()

    def test_directory_migration_updates_in_place_without_duplicates(self):
        expected = {
            "Amazon", "eBay", "Miami Star", "OEM Parts Online", "FCP Euro",
            "John Deere Parts Catalog", "CAT SIS", "Worldpac", "7zap", "General Research",
        }
        with closing(self.connection()) as connection:
            before = {
                row["display_name"]: row["id"]
                for row in connection.execute(
                    "SELECT id,display_name FROM connector_profiles WHERE display_name IN (%s)"
                    % ",".join("?" for _ in expected), tuple(expected)
                )
            }
            run_migrations()
            rows = connection.execute(
                "SELECT id,display_name FROM connector_profiles WHERE display_name IN (%s)"
                % ",".join("?" for _ in expected), tuple(expected)
            ).fetchall()
        self.assertEqual({row["display_name"] for row in rows}, expected)
        self.assertEqual(len(rows), len(expected))
        self.assertEqual({row["display_name"]: row["id"] for row in rows}, before)

    def test_equipment_and_vehicle_sources_share_registry_but_remain_isolated(self):
        with closing(self.connection()) as connection:
            cat = list_sources_for_context(
                connection, manufacturer="CAT", asset_category="Heavy Equipment", market="UNKNOWN"
            )
            deere = list_sources_for_context(
                connection, manufacturer="John Deere", asset_category="machine", market="JDM"
            )
            vehicle = list_sources_for_context(
                connection, manufacturer="BMW", asset_category="automotive", market="UK"
            )
        cat_names = [row["display_name"] for row in cat]
        deere_names = [row["display_name"] for row in deere]
        vehicle_names = [row["display_name"] for row in vehicle]
        self.assertIn("CAT SIS", cat_names)
        self.assertIn("Miami Star", cat_names)
        self.assertNotIn("John Deere Parts Catalog", cat_names)
        self.assertEqual(deere_names[:4], ["John Deere Parts Catalog", "Miami Star", "eBay", "Amazon"])
        self.assertNotIn("CAT SIS", deere_names)
        self.assertIn("FCP Euro", vehicle_names)
        self.assertIn("OEM Parts Online", vehicle_names)
        self.assertIn("Worldpac", vehicle_names)
        self.assertIn("7zap", vehicle_names)
        self.assertNotIn("Miami Star", vehicle_names)
        self.assertNotIn("CAT SIS", vehicle_names)
        self.assertEqual(cat_names[-1], "General Research")
        self.assertEqual(vehicle_names[-1], "General Research")

    def test_recommended_and_higher_priority_rank_first(self):
        with closing(self.connection()) as connection:
            create_source(
                connection, display_name="Low Priority Test", source_type="SUPPLIER",
                launch_url="https://example.test/low", market_applicability="GLOBAL",
                source_priority=10, is_default=True,
            )
            create_source(
                connection, display_name="High Priority Test", source_type="SUPPLIER",
                launch_url="https://example.test/high", market_applicability="GLOBAL",
                source_priority=90, is_default=True,
            )
            create_source(
                connection, display_name="Not Recommended Test", source_type="SUPPLIER",
                launch_url="https://example.test/other", market_applicability="GLOBAL",
                source_priority=999, is_default=False,
            )
            connection.commit()
            names = [row["display_name"] for row in list_sources_for_context(
                connection, manufacturer="Unknown", asset_category="machine", market="UNKNOWN"
            )]
        self.assertLess(names.index("High Priority Test"), names.index("Low Priority Test"))
        self.assertLess(names.index("Low Priority Test"), names.index("Not Recommended Test"))
        self.assertEqual(names[-1], "General Research")

    def test_admin_created_source_is_immediately_available_to_job_research(self):
        response = legacy_app.add_connector(
            display_name="Admin Deere Source", launch_url="https://example.test/admin-deere",
            category="OEM Catalog", source_type="OEM_CATALOG", trust_level="NEEDS_REVIEW",
            connector_type="CATALOG", manufacturer_applicability="John Deere",
            asset_category_applicability="machine", market_applicability="GLOBAL",
            notes="Admin source", source_priority=85, is_default="1", is_enabled="1",
        )
        self.assertEqual(response.status_code, 303)
        with closing(self.connection()) as connection:
            master = connection.execute(
                "SELECT * FROM connector_profiles WHERE display_name='Admin Deere Source'"
            ).fetchone()
            routed = list_sources_for_context(
                connection, manufacturer="John Deere", asset_category="equipment", market="UNKNOWN"
            )
        self.assertIsNotNone(master)
        self.assertIn(master["id"], [row["id"] for row in routed])

    def test_job_created_source_is_master_record_and_repeated_save_reuses_it(self):
        with closing(self.connection()) as connection:
            job = connection.execute("SELECT id FROM jobs ORDER BY id LIMIT 1").fetchone()
            if job is None:
                self.skipTest("Fixture has no Job")
            asset = connection.execute(
                "SELECT * FROM job_assets WHERE job_id=? AND state='ACTIVE' ORDER BY id LIMIT 1",
                (job["id"],),
            ).fetchone()
            if asset is None:
                self.skipTest("Fixture has no active Job Asset")
            job_id, asset_id = int(job["id"]), int(asset["id"])
        for _ in range(2):
            response = add_job_source(
                job_id, display_name="Workspace Master Source", source_type="OEM_CATALOG",
                launch_url="https://example.test/workspace", market_applicability="GLOBAL",
                notes="Workspace source", job_asset_id=asset_id,
            )
            self.assertEqual(response.status_code, 303)
            self.assertIn(f"asset_id={asset_id}", response.headers["location"])
        with closing(self.connection()) as connection:
            rows = connection.execute(
                "SELECT * FROM connector_profiles WHERE display_name='Workspace Master Source'"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provenance"], f"JOB_WORKSPACE:{job_id}")

    def test_legacy_verification_endpoint_remains_registered(self):
        paths = {getattr(route, "path", "") for route in legacy_app.app.routes}
        self.assertIn("/parts/{part_id}/sources/{source_id}/verification", paths)


if __name__ == "__main__":
    unittest.main()
