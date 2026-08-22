from __future__ import annotations

from contextlib import closing
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

import legacy_app
from plg_core.database.migrations import run_migrations
from plg_core.intake.identifiers import classify_identifier, decoder_route_contract
from plg_core.intake.parser import parse_intake
from plg_core.intake.service import confirm_proposal, create_proposal, load_proposal


ROOT = Path(__file__).resolve().parents[1]


class SmartIntakeBatch3DTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pps-batch3d-")
        self.db_path = Path(self.temp.name) / "test.db"
        shutil.copy2(ROOT / "data" / "plg_core.db", self.db_path)
        self.db_patch = patch.object(legacy_app, "DB_PATH", self.db_path)
        self.db_patch.start()
        run_migrations()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def connection(self):
        return legacy_app.get_connection()

    def test_original_failure_and_three_unlabeled_forms(self):
        original = "Jordan Example Example Equipment Development Kingston, Jamaica John Deere 350D PIN: TEST0000000000001 Need: Fuel Filter Kit"
        multiline = "Jordan Example\nExample Equipment Development\nTest City\nJohn Deere 350D\nTEST0000000000001\nFuel Filter Kit"
        sentence = "Jordan from Example Equipment Development in Test City needs a fuel filter kit for their John Deere 350D. The machine PIN is TEST0000000000001."
        for text in (original, multiline, sentence):
            proposal = parse_intake(text)
            self.assertTrue(proposal["contact_name"].startswith("Jordan"))
            self.assertEqual(proposal["company_name"], "Example Equipment Development")
            self.assertEqual(proposal["assets"][0]["manufacturer"], "John Deere")
            self.assertEqual(proposal["assets"][0]["model"], "350D")
            self.assertEqual(proposal["assets"][0]["identifiers"][0]["identifier_type"], "PIN")
            self.assertIn("fuel filter kit", proposal["assets"][0]["needs"][0]["wording"].lower())

    def test_numbered_natural_language_request_coalesces_machine_and_quantities(self):
        raw = """Customer: Taylor Example
Location: Exuma, Bahamas

Machine:
Caterpillar 420D Backhoe Loader
PIN: TEST-PIN-INTAKE-002

Request:
I need the following parts for my CAT 420D:

1. Starter motor — quantity 1
2. Alternator — quantity 1
3. Fuel filter — quantity 2
4. Hydraulic return filter — quantity 2

Please source the parts and give me pricing. I am okay with aftermarket parts if they are confirmed compatible. Let me know if you need any additional information."""
        proposal = parse_intake(raw)
        self.assertEqual((proposal["contact_name"], proposal["company_name"]), ("Taylor Example", ""))
        self.assertEqual(len(proposal["assets"]), 1)
        asset = proposal["assets"][0]
        self.assertEqual((asset["manufacturer"], asset["model"]), ("Caterpillar", "420D Backhoe Loader"))
        self.assertEqual(asset["identifiers"][0]["value"], "TEST-PIN-INTAKE-002")
        self.assertEqual([item["wording"] for item in asset["needs"]], [
            "Starter motor — Qty 1", "Alternator — Qty 1",
            "Fuel filter — Qty 2", "Hydraulic return filter — Qty 2",
        ])
        self.assertEqual([item["quantity"] for item in asset["needs"]], [1.0, 1.0, 2.0, 2.0])
        self.assertNotIn("additional information", " ".join(item["wording"] for item in asset["needs"]).lower())
        self.assertEqual(proposal["raw_input"], raw)

    def test_bullets_and_quantity_forms_are_supported_without_invention(self):
        proposal = parse_intake("""Customer: Test Person
Machine:
JCB 3CX
PIN TEST-JCB-LIST
Requested Parts:
- Seal kit Qty 2
• Filter x3
- Hose 4x
- Belt

Please call if you need more information.""")
        needs = proposal["assets"][0]["needs"]
        self.assertEqual([item["wording"] for item in needs], ["Seal kit — Qty 2", "Filter — Qty 3", "Hose — Qty 4", "Belt"])
        self.assertIsNone(needs[-1]["quantity"])

    def test_multi_machine_needs_do_not_leak(self):
        proposal = parse_intake("""PPS-BATCH3D-TEST-001
Jordan Example
Example Equipment Development
Montego Bay, Jamaica
John Deere 350D
PIN TEST-PIN-INTAKE-001
needs:
Fuel Filter Kit
Hydraulic Filter
JCB 3CX
PIN TEST-JCB-3CX-002
needs:
Boom Cylinder Seal Kit
Engine Oil Filter
2022 Toyota Hilux
VIN TEST-TOYOTA-HILUX-003
needs:
Left Headlight
Right Headlight""")
        self.assertEqual(len(proposal["assets"]), 3)
        grouped = {asset["manufacturer"]: [need["wording"] for need in asset["needs"]] for asset in proposal["assets"]}
        self.assertEqual(grouped["John Deere"], ["Fuel Filter Kit", "Hydraulic Filter"])
        self.assertEqual(grouped["JCB"], ["Boom Cylinder Seal Kit", "Engine Oil Filter"])
        self.assertEqual(grouped["Toyota"], ["Left Headlight", "Right Headlight"])
        self.assertEqual(proposal["assets"][2]["year"], "2022")
        self.assertEqual(proposal["assets"][1]["year"], "")

    def test_two_existing_assets_are_separate_and_matched_without_duplicates(self):
        raw = """Synthetic Intake Customer
synthetic-intake@example.test
Caterpillar 420D Backhoe Loader
Serial TEST-420D-001
International 4700 Flatbed
VIN TEST-4700-001

Need hydraulic cylinder seal kit and headlight sets for the Caterpillar 420D, and wheel seal and brake drums for the International 4700."""
        with closing(self.connection()) as connection:
            customer_id = connection.execute(
                "INSERT INTO customers(customer_number,name,email,active) VALUES ('SI-MULTI','Synthetic Intake Customer','synthetic-intake@example.test',1)"
            ).lastrowid
            machine_ids = {
                connection.execute(
                    "INSERT INTO machines(customer_id,machine_number,name,manufacturer,model,vin_pin_serial,active) VALUES (?,'SI-CAT','420D','Caterpillar','420D Backhoe Loader','TEST-420D-001',1)",
                    (customer_id,),
                ).lastrowid,
                connection.execute(
                    "INSERT INTO machines(customer_id,machine_number,name,manufacturer,model,vin_pin_serial,active) VALUES (?,'SI-INT','4700','International','4700 Flatbed','TEST-4700-001',1)",
                    (customer_id,),
                ).lastrowid,
            }
            connection.commit()
            proposal_id = create_proposal(connection, raw)
            proposal = load_proposal(connection, proposal_id)
            self.assertEqual(proposal["matched_customer_id"], customer_id)
            self.assertEqual({asset["matched_machine_id"] for asset in proposal["assets"]}, machine_ids)
            by_make = {asset["manufacturer"]: asset for asset in proposal["assets"]}
            self.assertEqual(by_make["Caterpillar"]["identifiers"][0]["identifier_value"], "TEST-420D-001")
            self.assertEqual(by_make["International"]["identifiers"][0]["identifier_value"], "TEST-4700-001")
            self.assertEqual({need["wording"].lower() for need in by_make["Caterpillar"]["needs"]}, {"hydraulic cylinder seal kit", "headlight sets"})
            self.assertEqual({need["wording"].lower() for need in by_make["International"]["needs"]}, {"wheel seal", "brake drums"})
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM customers WHERE name='Synthetic Intake Customer'").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM machines WHERE customer_id=?", (customer_id,)).fetchone()[0], 2)

    def test_ambiguous_and_zero_machine_needs_remain_unassigned(self):
        ambiguous = parse_intake("Jordan Example\nJohn Deere 350D\nJCB 3CX\nNeed starter")
        self.assertEqual(len(ambiguous["assets"]), 2)
        self.assertEqual(ambiguous["unassigned_needs"][0]["wording"].lower(), "starter")
        self.assertFalse(any(asset["needs"] for asset in ambiguous["assets"]))
        zero = parse_intake("Norman needs a starter but hasn't sent the machine information yet.")
        self.assertEqual(zero["assets"], [])
        self.assertEqual(zero["contact_name"], "Norman")
        self.assertEqual(zero["unassigned_needs"][0]["wording"].lower(), "starter")

    def test_global_vin_market_location_separation_and_equipment_context(self):
        vin = "TEST0000000000002"
        proposal = parse_intake(f"Morgan Example\nTest City\n2022 Toyota Hilux\nVIN {vin}\nNeeds Left Headlight and Right Headlight")
        asset = proposal["assets"][0]
        self.assertEqual(asset["identifiers"][0]["identifier_type"], "AUTOMOTIVE_VIN")
        self.assertEqual(asset["market_region"], "UNKNOWN")
        self.assertEqual(classify_identifier(vin, manufacturer="John Deere", asset_category="machine"), "PIN")

    def test_jdm_frame_model_code_and_component_identifier_are_independent(self):
        proposal = parse_intake("Morgan Example\nToyota Prius\nFrame: ZVW30-TEST123\nModel Code: DAA-ZVW30\nEngine Serial: ENG-TEST123\nNeeds left headlight")
        asset = proposal["assets"][0]
        identifiers = {(item["identifier_type"], item["value"]) for item in asset["identifiers"]}
        self.assertIn(("JDM_FRAME", "ZVW30-TEST123"), identifiers)
        self.assertIn(("ENGINE_SERIAL", "ENG-TEST123"), identifiers)
        self.assertEqual(asset["model_code"], "DAA-ZVW30")
        self.assertEqual(asset["market_region"], "UNKNOWN")

    def test_decoder_router_is_a_non_network_contract(self):
        result = decoder_route_contract({"identifier_type":"JDM_FRAME", "identifier":"ZVW30-TEST123", "manufacturer":"Toyota"})
        self.assertEqual(result["decoder_family"], "JDM_FRAME_CHASSIS")
        self.assertFalse(result["external_lookup_performed"])

    def test_proposal_persistence_correction_and_atomic_confirm(self):
        with closing(self.connection()) as connection:
            proposal_id = create_proposal(connection, "Jordan Example\nJohn Deere 350D\nPIN TEST-PIN-3D\nNeeds wrong part")
        with closing(self.connection()) as connection:
            proposal = load_proposal(connection, proposal_id)
            asset_id = proposal["assets"][0]["id"]
            need_id = proposal["assets"][0]["needs"][0]["id"]
            connection.execute("UPDATE intake_proposals SET contact_name='Unique Correct Person',company_name='Correct Company',location='Nassau, Bahamas',matched_customer_id=NULL WHERE id=?", (proposal_id,))
            connection.execute("UPDATE intake_proposal_assets SET manufacturer='JCB',model='3CX' WHERE id=?", (asset_id,))
            connection.execute("UPDATE intake_proposal_needs SET wording='Correct Seal Kit' WHERE id=?", (need_id,))
            connection.commit()
            version = connection.execute("SELECT lock_version FROM intake_proposals WHERE id=?", (proposal_id,)).fetchone()[0]
            job_id = confirm_proposal(connection, proposal_id, version)
        with closing(self.connection()) as connection:
            job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            request = connection.execute("SELECT * FROM customer_requests WHERE job_id=?", (job_id,)).fetchone()
            asset = connection.execute("SELECT * FROM job_assets WHERE job_id=?", (job_id,)).fetchone()
            need = connection.execute("SELECT * FROM requested_needs WHERE job_id=?", (job_id,)).fetchone()
            self.assertEqual((job["company"], asset["manufacturer"], asset["model"]), ("Correct Company", "JCB", "3CX"))
            self.assertEqual(need["wording"], "Correct Seal Kit")
            self.assertEqual(request["request_text"], "Jordan Example\nJohn Deere 350D\nPIN TEST-PIN-3D\nNeeds wrong part")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM basket_items bi JOIN baskets b ON b.id=bi.basket_id WHERE b.job_id=?", (job_id,)).fetchone()[0], 0)

    def test_end_to_end_three_machine_work_queue_is_populated(self):
        raw = """Disposable Contact
Disposable Company Limited
Nassau, Bahamas
John Deere 350D
PIN TEST-DEERE-3D
Needs Fuel Filter Kit and Hydraulic Filter
JCB 3CX
PIN TEST-JCB-3D
Needs Boom Seal Kit and Engine Oil Filter
2022 Toyota Hilux
VIN TEST-TOYOTA-3D
Needs Left Headlight and Right Headlight"""
        with closing(self.connection()) as connection:
            proposal_id = create_proposal(connection, raw)
            proposal = load_proposal(connection, proposal_id)
            job_id = confirm_proposal(connection, proposal_id, proposal["lock_version"])
        with closing(self.connection()) as connection:
            rows = connection.execute(
                """SELECT a.manufacturer,n.wording FROM job_assets a
                LEFT JOIN requested_needs n ON n.job_asset_id=a.id
                WHERE a.job_id=? ORDER BY a.id,n.id""", (job_id,),
            ).fetchall()
            grouped = {}
            for row in rows:
                grouped.setdefault(row["manufacturer"], []).append(row["wording"])
            self.assertEqual(grouped["John Deere"], ["Fuel Filter Kit", "Hydraulic Filter"])
            self.assertEqual(grouped["JCB"], ["Boom Seal Kit", "Engine Oil Filter"])
            self.assertEqual(grouped["Toyota"], ["Left Headlight", "Right Headlight"])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM basket_items bi JOIN baskets b ON b.id=bi.basket_id WHERE b.job_id=?", (job_id,)).fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM requested_needs WHERE job_id=?", (job_id,)).fetchone()[0], 6)

    def test_confirm_is_idempotent_and_stale_version_is_blocked(self):
        with closing(self.connection()) as connection:
            proposal_id = create_proposal(connection, "Unique Idempotent Person\nJohn Deere 350D\nNeeds Fuel Filter")
        with closing(self.connection()) as connection:
            version = connection.execute("SELECT lock_version FROM intake_proposals WHERE id=?", (proposal_id,)).fetchone()[0]
            job_id = confirm_proposal(connection, proposal_id, version)
        with closing(self.connection()) as connection:
            self.assertEqual(confirm_proposal(connection, proposal_id, version), job_id)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs WHERE id=?", (job_id,)).fetchone()[0], 1)

    def test_concurrent_confirm_creates_one_complete_job(self):
        with closing(self.connection()) as connection:
            proposal_id = create_proposal(connection, "Concurrent Person\nJohn Deere 350D\nNeeds Fuel Filter")
            version = connection.execute("SELECT lock_version FROM intake_proposals WHERE id=?", (proposal_id,)).fetchone()[0]
        results, errors = [], []
        barrier = threading.Barrier(2)
        def worker():
            connection = self.connection()
            try:
                barrier.wait()
                results.append(confirm_proposal(connection, proposal_id, version))
            except Exception as exc:  # pragma: no cover - diagnostic collection
                errors.append(exc)
                connection.rollback()
            finally:
                connection.close()
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(results)), 1)
        with closing(self.connection()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs WHERE id=?", (results[0],)).fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM requested_needs WHERE job_id=?", (results[0],)).fetchone()[0], 1)

    def test_confirmation_failure_rolls_back_every_business_record(self):
        with closing(self.connection()) as connection:
            proposal_id = create_proposal(connection, "Rollback Person\nJohn Deere 350D\nNeeds Fuel Filter")
            version = connection.execute("SELECT lock_version FROM intake_proposals WHERE id=?", (proposal_id,)).fetchone()[0]
            before = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in ("customers", "customer_requests", "jobs", "machines", "job_assets", "requested_needs")}
            connection.execute("CREATE TRIGGER batch3d_test_abort BEFORE INSERT ON requested_needs BEGIN SELECT RAISE(ABORT,'test rollback'); END")
            connection.commit()
            with self.assertRaises(Exception):
                confirm_proposal(connection, proposal_id, version)
            connection.rollback()
            after = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in before}
            self.assertEqual(after, before)
            self.assertEqual(connection.execute("SELECT status FROM intake_proposals WHERE id=?", (proposal_id,)).fetchone()[0], "DRAFT")

    def test_existing_asset_strong_identifier_is_proposed_without_overwrite(self):
        with closing(self.connection()) as connection:
            customer_id = connection.execute("INSERT INTO customers(customer_number,name,email,active) VALUES ('3D-C','Existing Customer','existing-3d@example.test',1)").lastrowid
            machine_id = connection.execute("INSERT INTO machines(customer_id,machine_number,name,manufacturer,model,vin_pin_serial,active) VALUES (?,'3D-M','350D','John Deere','350D','MATCH-3D-PIN',1)", (customer_id,)).lastrowid
            connection.commit()
            proposal_id = create_proposal(connection, "Existing Customer\nexisting-3d@example.test\nJohn Deere 350D\nPIN MATCH-3D-PIN\nNeeds Filter")
            proposal = load_proposal(connection, proposal_id)
            self.assertEqual(proposal["assets"][0]["matched_machine_id"], machine_id)
            self.assertEqual(proposal["matched_customer_id"], customer_id)

    def test_migration_is_idempotent_and_integrity_clean(self):
        run_migrations(); run_migrations()
        with closing(self.connection()) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE migration_id='0039_smart_intake_proposals'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
