import unittest
from unittest.mock import patch

from starlette.requests import Request

import legacy_app


class ActualCostWebActorTests(unittest.TestCase):
    def test_web_actual_cost_uses_default_operator_actor(self):
        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/purchasing/orders/7/actual-cost",
            "raw_path": b"/purchasing/orders/7/actual-cost",
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "root_path": "",
            "app": legacy_app.app,
        })
        with patch("plg_core.web_security.require_valid_csrf"), patch(
            "plg_core.supply.service.record_actual_cost_adjustment"
        ) as record:
            response = legacy_app.record_actual_cost_web(
                request,
                7,
                "ITEM",
                125.0,
                "Supplier invoice correction",
                "actual-cost-1",
                "csrf",
                supplier_order_item_id=9,
                actor_name="typed name should be ignored",
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(record.call_args.kwargs["actor"], "web.operator")


if __name__ == "__main__":
    unittest.main()
