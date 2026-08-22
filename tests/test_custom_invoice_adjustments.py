from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

from plg_core.documents.custom_invoice import (
    custom_invoice_presentation,
    revert_custom_invoice_presentation,
    save_custom_invoice_visibility,
)
from plg_core.documents.invoice_pdf import (
    build_invoice_pdf,
    generate_custom_invoice_pdf,
)


def pdf_text(path: Path) -> str:
    return subprocess.run(
        ["pdftotext", str(path), "-"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


class CustomInvoiceAdjustmentTests(unittest.TestCase):
    def setUp(self):
        self.invoice = {
            "id": 1,
            "invoice_number": "PPS-I-ADJUST",
            "invoice_date": "2026-08-18",
            "status": "PAID",
            "customer": "Adjustment Customer",
            "company": "",
            "address": "123 Test Road",
            "phone": "555-0100",
            "email": "customer@example.test",
            "manufacturer": "International",
            "machine": "4700",
            "year": "",
            "pin_serial": "TEST-PIN",
            "parts_subtotal": 450.0,
            "shipping_total": 50.0,
            "service_charge": 75.0,
            "sourcing_fee": 25.0,
            "customer_total": 600.0,
            "balance_due": 0.0,
            "credit_applied": 0.0,
        }
        self.custom = {
            "id": 1,
            "custom_invoice_number": "PPS-I-ADJUSTC",
            "custom_total": 600.0,
            "include_freight": 1,
        }
        self.items = [
            self.item(1, "Brake Drums", 200.0),
            self.item(2, "Hub", 150.0),
            self.item(3, "Seal", 100.0),
        ]
        self.temp = tempfile.TemporaryDirectory(prefix="pps-custom-adjust-")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def item(item_id: int, description: str, amount: float) -> dict:
        return {
            "id": item_id,
            "invoice_item_id": item_id,
            "quantity": 1,
            "description": description,
            "brand": "",
            "supplier_part_number": f"PART-{item_id}",
            "customer_unit_price": amount,
            "customer_line_total": amount,
            "custom_unit_price": amount,
            "custom_line_total": amount,
            "is_visible": 1,
        }

    def test_removing_one_item_recalculates_without_combining_remaining_parts(self):
        self.items[0]["is_visible"] = 0
        result = custom_invoice_presentation(
            self.invoice, self.custom, self.items
        )
        self.assertEqual(
            [row["description"] for row in result["visible_items"]],
            ["Hub", "Seal"],
        )
        self.assertEqual(result["subtotal"], 350.0)
        self.assertEqual(result["invoice_total"], 400.0)
        self.assertEqual(result["balance_due"], 0.0)

    def test_removing_multiple_items_and_freight_recalculates(self):
        self.items[0]["is_visible"] = 0
        self.items[1]["is_visible"] = 0
        self.custom["include_freight"] = 0
        result = custom_invoice_presentation(
            self.invoice, self.custom, self.items
        )
        self.assertEqual(
            [row["description"] for row in result["visible_items"]],
            ["Seal"],
        )
        self.assertEqual(result["freight"], 0.0)
        self.assertEqual(result["subtotal"], 200.0)
        self.assertEqual(result["invoice_total"], 200.0)

    def test_freight_line_item_is_excluded_by_freight_toggle(self):
        invoice = dict(self.invoice, shipping_total=0.0)
        custom = dict(self.custom, include_freight=0)
        items = self.items + [self.item(4, "Inbound Freight", 50.0)]
        custom["custom_total"] = 600.0
        result = custom_invoice_presentation(invoice, custom, items)
        self.assertEqual(
            [row["description"] for row in result["visible_items"]],
            ["Brake Drums", "Hub", "Seal"],
        )
        self.assertEqual(result["subtotal"], 550.0)
        self.assertEqual(result["invoice_total"], 550.0)

    def test_visibility_persists_and_revert_restores_source_presentation(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE invoice_items (
                id INTEGER PRIMARY KEY, invoice_id INTEGER, quantity INTEGER,
                customer_unit_price REAL, customer_line_total REAL
            );
            CREATE TABLE custom_invoices (
                id INTEGER PRIMARY KEY, adjustment_mode TEXT,
                adjustment_value REAL, custom_total REAL,
                include_freight INTEGER, updated_at TEXT
            );
            CREATE TABLE custom_invoice_items (
                id INTEGER PRIMARY KEY, custom_invoice_id INTEGER,
                invoice_item_id INTEGER, quantity INTEGER,
                custom_unit_price REAL, custom_line_total REAL,
                is_visible INTEGER
            );
            """
        )
        for item in self.items:
            connection.execute(
                "INSERT INTO invoice_items VALUES (?,?,?,?,?)",
                (item["id"], 1, 1, item["custom_unit_price"], item["custom_line_total"]),
            )
            connection.execute(
                "INSERT INTO custom_invoice_items VALUES (?,?,?,?,?,?,1)",
                (item["id"], 1, item["id"], 1, item["custom_unit_price"], item["custom_line_total"]),
            )
        connection.execute(
            "INSERT INTO custom_invoices VALUES (1,'MANUAL',NULL,600,1,CURRENT_TIMESTAMP)"
        )
        source_before = [
            tuple(row) for row in connection.execute(
                "SELECT * FROM invoice_items ORDER BY id"
            )
        ]
        rows = connection.execute(
            "SELECT * FROM custom_invoice_items ORDER BY id"
        ).fetchall()
        save_custom_invoice_visibility(connection, 1, rows, {2, 3}, False)
        reopened = connection.execute(
            "SELECT * FROM custom_invoice_items ORDER BY id"
        ).fetchall()
        self.assertEqual([row["is_visible"] for row in reopened], [0, 1, 1])
        self.assertEqual(
            connection.execute(
                "SELECT include_freight FROM custom_invoices WHERE id=1"
            ).fetchone()[0],
            0,
        )

        connection.execute(
            "UPDATE custom_invoice_items SET quantity=9, custom_unit_price=1, custom_line_total=9"
        )
        custom_row = connection.execute(
            "SELECT * FROM custom_invoices WHERE id=1"
        ).fetchone()
        changed_rows = connection.execute(
            "SELECT * FROM custom_invoice_items ORDER BY id"
        ).fetchall()
        revert_custom_invoice_presentation(
            connection, self.invoice, custom_row, changed_rows
        )
        reverted = connection.execute(
            "SELECT * FROM custom_invoice_items ORDER BY id"
        ).fetchall()
        self.assertEqual([row["is_visible"] for row in reverted], [1, 1, 1])
        self.assertEqual([row["quantity"] for row in reverted], [1, 1, 1])
        self.assertEqual(
            [row["custom_line_total"] for row in reverted],
            [200.0, 150.0, 100.0],
        )
        restored_custom = connection.execute(
            "SELECT * FROM custom_invoices WHERE id=1"
        ).fetchone()
        self.assertEqual(restored_custom["include_freight"], 1)
        self.assertEqual(restored_custom["custom_total"], 600.0)
        self.assertEqual(
            [tuple(row) for row in connection.execute("SELECT * FROM invoice_items ORDER BY id")],
            source_before,
        )
        connection.close()

    def test_adjusted_pdf_filters_items_and_freight_but_standard_is_unchanged(self):
        self.items[0]["is_visible"] = 0
        self.custom["include_freight"] = 0
        custom_path = Path(self.temp.name) / "adjusted-custom.pdf"
        standard_path = Path(self.temp.name) / "standard.pdf"
        generate_custom_invoice_pdf(
            self.invoice, self.custom, self.items, custom_path
        )
        build_invoice_pdf(
            self.invoice, self.items, standard_path, internal=False
        )
        custom_body = pdf_text(custom_path)
        self.assertNotIn("Brake Drums", custom_body)
        self.assertIn("Hub", custom_body)
        self.assertIn("Seal", custom_body)
        self.assertLess(custom_body.index("Hub"), custom_body.index("Seal"))
        self.assertNotIn("Shipping", custom_body)
        self.assertNotIn("Service Charge", custom_body)
        self.assertNotIn("Sourcing Fee", custom_body)
        self.assertIn("$350.00", custom_body)
        self.assertIn("$0.00", custom_body)

        standard_body = pdf_text(standard_path)
        self.assertIn("Brake Drums", standard_body)
        self.assertIn("Shipping", standard_body)
        self.assertIn("Service Charge", standard_body)
        self.assertIn("Sourcing Fee", standard_body)
        self.assertEqual(self.invoice["customer_total"], 600.0)
        self.assertEqual(self.invoice["shipping_total"], 50.0)
        self.assertEqual(self.invoice["service_charge"], 75.0)
        self.assertEqual(self.invoice["sourcing_fee"], 25.0)


if __name__ == "__main__":
    unittest.main()
