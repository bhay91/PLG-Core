from pathlib import Path
import unittest

from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]


class SupplierOrderReceivingUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Environment(loader=FileSystemLoader(ROOT / "templates"))
        cls.env.globals["url_for"] = lambda *args, **kwargs: "#"
        cls.template = cls.env.get_template("supplier_order_receive_simple.html")
        cls.orders = (ROOT / "templates/job_workspace/_orders.html").read_text()
        cls.service = (ROOT / "plg_core/jobs/service.py").read_text()

    def _render(self, received):
        items = [
            {"id": 10, "description": "Hydraulic Pump", "supplier_part_number": "HP-10", "quantity_ordered": 2, "quantity_received": received},
            {"id": 11, "description": "Seal Kit", "supplier_part_number": "SK-11", "quantity_ordered": 1, "quantity_received": 1},
        ]
        return self.template.render(
            order={"id": 7, "po_number": "PPS-PO-0007", "supplier_name": "Acme", "job_number": "PPS-J-0042", "customer": "Bobby", "items": items},
            items=items, csrf_token="csrf", idempotency_key="idem", receiver_default="Brandon",
        )

    def test_unreceived_order_has_simple_authoritative_form(self):
        html = self._render(0)
        self.assertIn('action="/purchasing/orders/7/receive"', html)
        self.assertIn('name="qty_10"', html)
        self.assertIn("Ordered 2 · Received 0 · Remaining 2", html)
        self.assertIn("Receive Parts", html)
        self.assertNotIn("Legacy", html)

    def test_fully_received_order_has_no_receive_form(self):
        html = self._render(2)
        self.assertNotIn('action="/purchasing/orders/7/receive"', html)
        self.assertIn(">Received<", html)
        self.assertIn("View Receiving", html)

    def test_job_orders_link_to_simple_receive_route(self):
        self.assertIn('receive_url": f"/purchasing/orders/{order_id}/receive"', self.service)
        self.assertIn("Receive Parts", self.orders)
        self.assertNotIn('href="{{ order.receive_url }}#receive-parts"', self.orders)


if __name__ == "__main__":
    unittest.main()
