"""Verifica o fluxo de pedidos sem iniciar o Docker."""

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
import unittest

import httpx
from fastapi import HTTPException

from relatorio_loja import render_html


ROOT = Path(__file__).resolve().parent
os.environ["AGENT_CONTROL_TOKEN"] = "test-token"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"test_{name}", ROOT / name / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LocalClient:
    def __init__(self, inventory, payment):
        self.inventory = inventory
        self.payment = payment
        self.fail_release = False
        self.fail_charge_transport = False

    def post(self, url, json=None, headers=None):
        path = urlparse(url).path
        request = SimpleNamespace(headers={key.lower(): value for key, value in (headers or {}).items()})
        try:
            if path == "/inventory/reservations":
                body = self.inventory.reserve_stock(self.inventory.ReservationRequest(**json), request)
            elif path.startswith("/inventory/reservations/") and path.endswith("/release"):
                if self.fail_release:
                    return httpx.Response(503, json={"detail": "Falha ao liberar"})
                body = self.inventory.release_stock(path.split("/")[3], request.headers.get("authorization"))
            elif path == "/payments/charges":
                if self.fail_charge_transport:
                    raise httpx.ReadTimeout("Resposta perdida")
                body = self.payment.charge_payment(self.payment.ChargeRequest(**json), request)
            else:
                raise AssertionError(f"Rota inesperada: {path}")
            return httpx.Response(200, json=body)
        except HTTPException as error:
            return httpx.Response(error.status_code, json={"detail": error.detail})


class ShopFlowTest(unittest.TestCase):
    def setUp(self):
        self.inventory = load("instavel")
        self.payment = load("carga")
        self.orders = load("normal")
        self.orders.client = LocalClient(self.inventory, self.payment)
        self.request = SimpleNamespace(headers={})

    def place_order(self):
        return self.orders.create_order(self.orders.OrderRequest(sku="camiseta-basica", size="M", quantity=1), self.request)

    def test_payment_failure_releases_stock_then_order_succeeds(self):
        initial = self.inventory.inventory_state()["available"]["camiseta-basica"]["M"]
        other_size = self.inventory.inventory_state()["available"]["camiseta-basica"]["P"]
        failed = self.place_order()
        failed_body = json.loads(failed.body)
        self.assertEqual(failed.status_code, 503)
        self.assertEqual(failed_body["status"], "payment_unavailable")
        self.assertTrue(failed_body["request_id"])
        self.assertEqual(self.payment.events(limit=100, after=0, request_id=failed_body["request_id"])[-1]["request_id"], failed_body["request_id"])
        self.assertEqual(self.inventory.inventory_state()["available"]["camiseta-basica"]["M"], initial)
        self.assertEqual(self.orders.get_order(failed_body["order_id"])["status"], "payment_unavailable")

        self.inventory.set_fault(self.inventory.FaultAction(enabled=False, reason="test_repair"), "Bearer test-token")
        self.payment.repair(self.payment.RepairAction(action="reset_overload", reason="test_repair"), "Bearer test-token")
        confirmed = self.place_order()
        confirmed_body = json.loads(confirmed.body)
        self.assertEqual(confirmed.status_code, 201)
        self.assertEqual(confirmed_body["status"], "confirmed")
        self.assertEqual(self.inventory.inventory_state()["available"]["camiseta-basica"]["M"], initial - 1)
        self.assertEqual(self.inventory.inventory_state()["available"]["camiseta-basica"]["P"], other_size)
        self.assertEqual(self.payment.payment_status(confirmed_body["order_id"])["amount_cents"], 5990)

    def test_wrong_repair_does_not_fix_payment(self):
        self.payment.set_fault(self.payment.FaultAction(enabled=True, mode="dependency_timeout", reason="test_setup"), "Bearer test-token")
        with self.assertRaises(HTTPException) as caught:
            self.payment.repair(self.payment.RepairAction(action="reset_overload", reason="test_repair"), "Bearer test-token")
        self.assertEqual(caught.exception.status_code, 409)
        with self.assertRaises(HTTPException) as caught:
            self.payment.charge_payment(self.payment.ChargeRequest(order_id="x", amount_cents=100), self.request)
        self.assertEqual(caught.exception.status_code, 503)

    def test_failed_compensation_is_visible(self):
        self.orders.client.fail_release = True
        initial = self.inventory.inventory_state()["available"]["camiseta-basica"]["M"]
        response = self.place_order()
        self.assertEqual(json.loads(response.body)["status"], "compensation_pending")
        self.assertEqual(self.inventory.inventory_state()["available"]["camiseta-basica"]["M"], initial - 1)
        with self.assertRaises(HTTPException) as caught:
            self.inventory.release_stock("unknown", None)
        self.assertEqual(caught.exception.status_code, 403)

    def test_lost_payment_response_remains_pending(self):
        self.orders.client.fail_charge_transport = True
        response = self.place_order()
        self.assertEqual(json.loads(response.body)["status"], "pending_reconciliation")
        self.assertEqual(self.orders.shop_stats()["pending_reconciliation"], 1)


class ShopReportTest(unittest.TestCase):
    def test_report_explains_orders_and_escapes_identifiers(self):
        html = render_html({
            "shop_type": "clothing", "strategy": "rf", "cause": "overload",
            "episode_started_at": "2026-10-28T19:00:00+00:00",
            "orders": [{"number": 1, "status": "confirmed", "sku": "camiseta-basica", "size": "M",
                        "request_id": "<script>alert(1)</script>"}],
            "decisions": [],
        })
        self.assertIn("1 pedido confirmado entre 1", html)
        self.assertIn("Estoque reservado e pagamento autorizado", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>alert(1)</script>", html)


if __name__ == "__main__":
    unittest.main()
