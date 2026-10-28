"""Roteamento controlado para a demonstracao de remediacao."""

from threading import Lock
from uuid import uuid4
import os

import httpx


class RoutingService:
    def __init__(self, event_logger):
        self.targets = {
            "pedidos": os.getenv("PEDIDOS_URL", "http://pedidos:8000"),
            "estoque": os.getenv("ESTOQUE_URL", "http://estoque:8000"),
            "pagamentos": os.getenv("PAGAMENTOS_URL", "http://pagamentos:8000"),
        }
        self.fallback = {name: False for name in self.targets}
        self.lock = Lock()
        self.client = httpx.Client(timeout=2.0, trust_env=False)
        self.event_logger = event_logger

    def state(self) -> dict:
        with self.lock:
            return dict(self.fallback)

    def set_fallback(self, target: str, enabled: bool) -> dict:
        if target not in ("estoque", "pagamentos"):
            raise ValueError("Alvo nao permite desvio")
        with self.lock:
            self.fallback[target] = enabled
        self.event_logger("routing_changed", str(uuid4()), "agent_demo", target=target, fallback=enabled)
        return self.state()

    def route(self, target: str, request_id: str) -> tuple[int, dict]:
        if target not in self.targets:
            raise ValueError("Alvo desconhecido")
        with self.lock:
            served_by = "pedidos" if self.fallback[target] else target
        params = {"auto": "true"} if served_by == "estoque" else None
        try:
            response = self.client.get(
                f"{self.targets[served_by]}/work",
                params=params,
                headers={"X-Request-ID": request_id, "X-Scenario": "agent_demo"},
            )
            status = response.status_code
            payload = response.json()
            fault_active = payload.get("fault_active") if isinstance(payload, dict) else None
        except httpx.RequestError:
            status = 503
            fault_active = None
        self.event_logger("routed_response", request_id, "agent_demo", target=target, served_by=served_by, status=status)
        return status, {"target": target, "served_by": served_by, "status": status, "fault_active": fault_active}
