from pathlib import Path
import unittest

from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]


class JobWorkspaceFulfillmentRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Environment(loader=FileSystemLoader(ROOT / "templates"))
        cls.template = cls.env.get_template("job_workspace/_orders.html")
        cls.orders_source = (ROOT / "templates/job_workspace/_orders.html").read_text()
        cls.overview_source = (ROOT / "templates/job_workspace/_overview.html").read_text()

    def _render(self, orders):
        return self.template.render(
            job={"id": 42},
            tokens=lambda: "",
            operational_snapshot={
                "fulfillment": {
                    "available": True,
                    "items": [
                        {
                            "order_id": 10,
                            "description": "Hydraulic Pump",
                            "quantity_ordered": 2,
                            "quantity_received": 1,
                            "quantity_to_receive": 1,
                            "received": False,
                        },
                        {
                            "order_id": 11,
                            "description": "Seal Kit",
                            "quantity_ordered": 1,
                            "quantity_received": 1,
                            "quantity_to_receive": 0,
                            "received": True,
                        },
                    ],
                },
                "receiving_exceptions": [],
                "active_backorders": [],
                "supplier_orders": orders,
                "delivery_url": "/jobs/42/delivery",
                "invoice": None,
            },
        )

    def test_ordered_workspace_renders_items_under_their_order(self):
        html = self._render([
            {"id": 10, "supplier": "Supplier A", "po_number": "PO-10", "status": "ORDERED", "ordered_units": 2, "received_units": 1, "delivered_units": 0, "available_to_deliver_units": 0, "order_url": "/purchasing/orders/10", "receive_url": "/purchasing/orders/10#receive-parts", "actual_cost_state": "CONFIRMED"},
            {"id": 11, "supplier": "Supplier B", "po_number": "PO-11", "status": "RECEIVED", "ordered_units": 1, "received_units": 1, "delivered_units": 0, "available_to_deliver_units": 1, "order_url": "/purchasing/orders/11", "receive_url": "/purchasing/orders/11#receive-parts", "actual_cost_state": "CONFIRMED"},
        ])
        first, second = html.index("Supplier A"), html.index("Supplier B")
        self.assertIn("Hydraulic Pump", html[first:second])
        self.assertNotIn("Seal Kit", html[first:second])
        self.assertIn("Seal Kit", html[second:])
        self.assertIn("Hydraulic Pump", html)
        self.assertIn("Seal Kit", html)

    def test_workspace_without_orders_still_renders_empty_state(self):
        html = self._render([])
        self.assertIn("No supplier orders yet.", html)

    def test_fulfillment_items_use_explicit_dictionary_keys(self):
        self.assertNotIn("operational_snapshot.fulfillment.items", self.orders_source)
        self.assertNotIn("operational_snapshot.fulfillment.items", self.overview_source)
        self.assertIn('operational_snapshot.fulfillment["items"]', self.orders_source)
        self.assertIn('operational_snapshot.fulfillment["items"]', self.overview_source)


if __name__ == "__main__":
    unittest.main()
