from collections import deque
from datetime import datetime, timezone
from threading import BoundedSemaphore, Lock
from typing import Literal
from uuid import uuid4
import json
import os
import time

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Serviço de Pedidos")
backend_url = os.getenv("ESTOQUE_URL", "http://estoque:8000")
inventory_url = os.getenv("INVENTORY_URL", "http://estoque:8000")
payment_url = os.getenv("PAGAMENTOS_URL", "http://pagamentos:8000")
client = httpx.Client(timeout=3.0, trust_env=False)
shared_capacity = BoundedSemaphore(4)
dependency_bulkhead = BoundedSemaphore(2)
lock = Lock()
events_buffer = deque(maxlen=2000)
sequence = 0
metrics = {
    "work": {"success": 0},
    "independent": {"unsafe": {"success": 0, "rejected": 0}, "protected": {"success": 0, "rejected": 0}},
    "dependent": {"unsafe": {"completed": 0, "rejected": 0}, "protected": {"completed": 0, "rejected": 0}},
    "active_dependent": {"unsafe": 0, "protected": 0},
}
products = {
    "camiseta-basica": {"name": "Camiseta basica", "price_cents": 5990, "sizes": ["P", "M", "G"]},
    "moletom-capuz": {"name": "Moletom com capuz", "price_cents": 12990, "sizes": ["P", "M", "G"]},
}
orders = {}
control_token = os.getenv("AGENT_CONTROL_TOKEN", "")


class OrderRequest(BaseModel):
    sku: str
    size: Literal["P", "M", "G"]
    quantity: int = Field(ge=1, le=10)


def log_event(event: str, request_id: str, scenario: str, **details):
    global sequence
    with lock:
        sequence += 1
        entry = {
            "sequence": sequence,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service": "pedidos",
            "event": event,
            "request_id": request_id,
            "scenario": scenario,
            **details,
        }
        events_buffer.append(entry)
    print(json.dumps(entry, ensure_ascii=False), flush=True)


def identity(request: Request, mode: str) -> tuple[str, str]:
    return request.headers.get("x-request-id", str(uuid4()))[:128], f"cascade_{mode}"

@app.get("/")
def root():
    return {
        "service": "pedidos",
        "message": "Serviço de pedidos funcionando"
    }


@app.get("/work")
def work(request: Request):
    request_id = request.headers.get("x-request-id", str(uuid4()))[:128]
    with lock:
        metrics["work"]["success"] += 1
    log_event("service_response", request_id, "agent_demo", status=200)
    return {"service": "pedidos", "status": "ok"}


@app.get("/products")
def list_products():
    return [{"sku": sku, **details} for sku, details in products.items()]


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    with lock:
        order = orders.get(order_id)
        return dict(order) if order else JSONResponse(status_code=404, content={"detail": "Pedido nao encontrado"})


@app.get("/shop/stats")
def shop_stats():
    with lock:
        counts = {"confirmed": 0, "stock_unavailable": 0, "inventory_unavailable": 0,
                  "payment_unavailable": 0, "compensation_pending": 0,
                  "pending_reconciliation": 0, "pending": 0}
        for order in orders.values():
            counts[order["status"]] += 1
        return {"total": len(orders), **counts}


@app.post("/orders")
def create_order(payload: OrderRequest, request: Request):
    product = products.get(payload.sku)
    if product is None:
        return JSONResponse(status_code=404, content={"detail": "Produto nao encontrado"})
    order_id = str(uuid4())
    request_id = (request.headers.get("x-request-id") or order_id)[:128]
    order = {"order_id": order_id, "request_id": request_id, "sku": payload.sku, "size": payload.size,
             "quantity": payload.quantity,
             "total_cents": product["price_cents"] * payload.quantity, "status": "pending"}
    with lock:
        orders[order_id] = order
    headers = {"X-Request-ID": request_id, "X-Scenario": "shop_order"}
    release_headers = {**headers, "Authorization": f"Bearer {control_token}"}
    log_event("order_started", request_id, "shop_order", order_id=order_id, sku=payload.sku, size=payload.size)
    try:
        reserved = client.post(f"{inventory_url}/inventory/reservations", json={
            "order_id": order_id, "sku": payload.sku, "size": payload.size, "quantity": payload.quantity,
        }, headers=headers)
        if reserved.status_code != 200:
            status = 409 if reserved.status_code in (404, 409) else 503
            with lock:
                order["status"] = "stock_unavailable" if status == 409 else "inventory_unavailable"
            log_event("order_failed", request_id, "shop_order", order_id=order_id, status=status, stage="inventory")
            return JSONResponse(status_code=status, content=dict(order))
        charged = client.post(f"{payment_url}/payments/charges", json={
            "order_id": order_id, "amount_cents": order["total_cents"],
        }, headers=headers)
        if charged.status_code != 200:
            released = client.post(f"{inventory_url}/inventory/reservations/{order_id}/release", headers=release_headers)
            with lock:
                order["status"] = "payment_unavailable" if released.status_code == 200 else "compensation_pending"
            log_event("order_failed", request_id, "shop_order", order_id=order_id, status=503, stage=order["status"])
            return JSONResponse(status_code=503, content=dict(order))
    except httpx.RequestError:
        with lock:
            order["status"] = "pending_reconciliation"
        log_event("order_pending", request_id, "shop_order", order_id=order_id, status=503, stage="transport")
        return JSONResponse(status_code=503, content=dict(order))
    try:
        payment_body = charged.json()
        payment_id = payment_body.get("payment_id") if isinstance(payment_body, dict) else None
    except ValueError:
        payment_id = None
    if not payment_id:
        with lock:
            order["status"] = "pending_reconciliation"
        log_event("order_pending", request_id, "shop_order", order_id=order_id, status=503, stage="payment_response")
        return JSONResponse(status_code=503, content=dict(order))
    with lock:
        order["status"] = "confirmed"
        order["payment_id"] = payment_id
    log_event("order_confirmed", request_id, "shop_order", order_id=order_id, status=201)
    return JSONResponse(status_code=201, content=dict(order), headers={"X-Request-ID": request_id})


@app.get("/dependent")
def dependent(request: Request, mode: Literal["unsafe", "protected"] = "unsafe"):
    request_id, scenario = identity(request, mode)
    capacity = shared_capacity if mode == "unsafe" else dependency_bulkhead
    if not capacity.acquire(blocking=False):
        with lock:
            metrics["dependent"][mode]["rejected"] += 1
        log_event("dependency_rejected", request_id, scenario, reason="capacity_full", status=503)
        return JSONResponse(status_code=503, content={"status": "rejected", "reason": "capacity_full"})

    with lock:
        metrics["active_dependent"][mode] += 1
    log_event("dependency_started", request_id, scenario, delay_ms=1500)
    started = time.monotonic()
    try:
        try:
            upstream = client.get(
                f"{backend_url}/work",
                params={"fail": True, "delay_ms": 1500},
                headers={"X-Request-ID": request_id, "X-Scenario": scenario},
            )
            status = upstream.status_code
        except httpx.RequestError:
            status = 503
        with lock:
            metrics["dependent"][mode]["completed"] += 1
        log_event("dependency_finished", request_id, scenario, status=status, duration_ms=round((time.monotonic() - started) * 1000))
        return JSONResponse(status_code=status, content={"status": "dependency_failed" if status != 200 else "ok"})
    finally:
        with lock:
            metrics["active_dependent"][mode] -= 1
        capacity.release()


@app.get("/independent")
def independent(request: Request, mode: Literal["unsafe", "protected"] = "unsafe"):
    request_id, scenario = identity(request, mode)
    if not shared_capacity.acquire(timeout=0.05):
        with lock:
            metrics["independent"][mode]["rejected"] += 1
        log_event("independent_rejected", request_id, scenario, reason="shared_capacity_full", status=503)
        return JSONResponse(status_code=503, content={"status": "rejected", "reason": "shared_capacity_full"})
    try:
        time.sleep(0.005)
        with lock:
            metrics["independent"][mode]["success"] += 1
        log_event("independent_succeeded", request_id, scenario, status=200)
        return {"status": "ok"}
    finally:
        shared_capacity.release()


@app.get("/stats")
def stats():
    with lock:
        return json.loads(json.dumps(metrics))


@app.get("/events")
def events(limit: int = Query(default=100, ge=1, le=500), request_id: str | None = None, after: int = Query(default=0, ge=0)):
    with lock:
        selected = [entry for entry in events_buffer if (request_id is None or entry["request_id"] == request_id) and entry["sequence"] > after]
    return selected[:limit] if after else selected[-limit:]

@app.get("/health")
def health():
    return {
        "status": "healthy"
    }
