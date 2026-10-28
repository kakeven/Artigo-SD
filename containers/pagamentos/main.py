from collections import deque
from datetime import datetime, timezone
from uuid import uuid4
import json
from fastapi import FastAPI, Header, HTTPException, Query, Request
import threading
import time
import hmac
import os
from pydantic import BaseModel, Field

app = FastAPI(title="Serviço de Pagamentos")
lock = threading.Lock()
events_buffer = deque(maxlen=2000)
sequence = 0
requests_total = 0
error_enabled = True
fault_mode = "overload"
control_token = os.getenv("AGENT_CONTROL_TOKEN", "")
charges = {}

ERROR_NAMES = {"overload": "OverloadError", "dependency_timeout": "DependencyTimeoutError",
               "circuit_open": "CircuitOpenError"}
REPAIR_ACTIONS = {"overload": "reset_overload", "dependency_timeout": "restore_dependency",
                  "circuit_open": "reset_circuit_breaker"}


class FaultAction(BaseModel):
    enabled: bool
    reason: str = Field(min_length=3, max_length=200)
    mode: str | None = None


class RepairAction(BaseModel):
    action: str
    reason: str = Field(min_length=3, max_length=200)


class ChargeRequest(BaseModel):
    order_id: str = Field(min_length=1, max_length=64)
    amount_cents: int = Field(ge=1)


def log_event(request_id: str, status: int, error_name: str | None = None, scenario: str = "agent_demo"):
    global sequence
    with lock:
        sequence += 1
        entry = {
            "sequence": sequence,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service": "pagamentos",
            "event": "service_response",
            "scenario": scenario,
            "request_id": request_id,
            "status": status,
            "error_name": error_name,
        }
        events_buffer.append(entry)
    print(json.dumps(entry, ensure_ascii=False), flush=True)

@app.get("/")
def root():
    return {
        "service": "pagamentos",
        "message": "Use /work para simular erro continuo ou /carga para gerar uso de CPU"
    }


@app.get("/work")
def work(request: Request):
    global requests_total
    request_id = request.headers.get("x-request-id", str(uuid4()))[:128]
    with lock:
        requests_total += 1
        status = 503 if error_enabled else 200
        error_name = ERROR_NAMES[fault_mode] if error_enabled else None
    log_event(request_id, status, error_name)
    if status == 503:
        raise HTTPException(status_code=503, detail=error_name)
    return {"service": "pagamentos", "status": "ok", "fault_active": False}


@app.post("/payments/charges")
def charge_payment(action: ChargeRequest, request: Request):
    request_id = (request.headers.get("x-request-id") or str(uuid4()))[:128]
    with lock:
        existing = charges.get(action.order_id)
        if existing:
            status = 200 if existing["amount_cents"] == action.amount_cents else 409
            error_name = None
            result = dict(existing)
        elif error_enabled:
            status = 503
            error_name = ERROR_NAMES[fault_mode]
            result = None
        else:
            status = 200
            error_name = None
            result = {"payment_id": str(uuid4()), "order_id": action.order_id,
                      "amount_cents": action.amount_cents, "status": "authorized"}
            charges[action.order_id] = result
    log_event(request_id, status, error_name, "shop_order")
    if status != 200:
        raise HTTPException(status_code=status, detail=error_name or "Valor inconsistente")
    return result


@app.get("/payments/charges/{order_id}")
def payment_status(order_id: str):
    with lock:
        charge = charges.get(order_id)
        if charge is None:
            raise HTTPException(status_code=404, detail="Pagamento nao encontrado")
        return dict(charge)


@app.get("/fault")
def fault_state():
    with lock:
        return {"enabled": error_enabled}


@app.post("/fault")
def set_fault(action: FaultAction, authorization: str | None = Header(default=None)):
    global error_enabled, fault_mode
    if not control_token or not hmac.compare_digest(authorization or "", f"Bearer {control_token}"):
        raise HTTPException(status_code=403, detail="Acesso negado")
    with lock:
        if action.mode is not None:
            if action.mode not in ERROR_NAMES:
                raise HTTPException(status_code=422, detail="Modo de falha desconhecido")
            fault_mode = action.mode
        error_enabled = action.enabled
    print(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), "service": "pagamentos", "event": "fault_control", "enabled": action.enabled, "mode": fault_mode, "reason": action.reason}), flush=True)
    return {"enabled": action.enabled}


@app.post("/repair")
def repair(action: RepairAction, authorization: str | None = Header(default=None)):
    global error_enabled
    if not control_token or not hmac.compare_digest(authorization or "", f"Bearer {control_token}"):
        raise HTTPException(status_code=403, detail="Acesso negado")
    if action.action not in REPAIR_ACTIONS.values():
        raise HTTPException(status_code=422, detail="Acao desconhecida")
    with lock:
        if action.action != REPAIR_ACTIONS[fault_mode]:
            raise HTTPException(status_code=409, detail="Acao nao corrigiu a causa ativa")
        error_enabled = False
    print(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), "service": "pagamentos", "event": "repair_control", "action": action.action, "reason": action.reason}), flush=True)
    return {"enabled": False, "action": action.action}


@app.get("/stats")
def stats():
    with lock:
        return {"total": requests_total, "fault_enabled": error_enabled, "authorized_payments": len(charges)}


@app.get("/events")
def events(limit: int = Query(default=100, ge=1, le=500), after: int = Query(default=0, ge=0), request_id: str | None = None):
    with lock:
        selected = [event for event in events_buffer if event["sequence"] > after and
                    (request_id is None or event["request_id"] == request_id)]
    return selected[:limit] if after else selected[-limit:]


def gerar_carga():
    fim = time.time() + 20

    while time.time() < fim:
        _ = 999999 ** 999


@app.post("/carga")
def carga():
    thread = threading.Thread(target=gerar_carga)
    thread.start()

    return {
        "message": "Carga de CPU iniciada por 20 segundos"
    }


@app.get("/health")
def health():
    with lock:
        fault_active = error_enabled
    if fault_active:
        raise HTTPException(status_code=503, detail="Falha simulada ativa")
    return {
        "status": "healthy",
        "fault_active": False
    }
