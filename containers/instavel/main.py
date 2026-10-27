from fastapi import FastAPI, Header, HTTPException, Query, Request
import time
from threading import Lock
from collections import deque
from datetime import datetime, timezone
import json
import hmac
import os
from uuid import uuid4
from pydantic import BaseModel, Field

app = FastAPI(title="Container Instável")

inicio = time.time()
contador = {"total": 0, "falhas": 0, "sucessos": 0, "por_cenario": {}}
contador_lock = Lock()
eventos = deque(maxlen=2000)
event_sequence = 0
auto_counter = 0
auto_fault_enabled = True
control_token = os.getenv("AGENT_CONTROL_TOKEN", "")


class FaultAction(BaseModel):
    enabled: bool
    reason: str = Field(min_length=3, max_length=200)


def registrar(evento: str, request_id: str, scenario: str, **details):
    global event_sequence
    with contador_lock:
        event_sequence += 1
        entry = {
            "sequence": event_sequence,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service": "instavel",
            "event": evento,
            "request_id": request_id,
            "scenario": scenario,
            **details,
        }
        eventos.append(entry)
    print(json.dumps(entry, ensure_ascii=False), flush=True)

@app.get("/")
def root():
    return {
        "service": "instavel",
        "message": "API continua executando"
    }

@app.get("/health")
def health():
    tempo_ativo = time.time() - inicio
    with contador_lock:
        fault_active = auto_fault_enabled

    if tempo_ativo > 30 and fault_active:
        raise HTTPException(
            status_code=500,
            detail="Falha simulada"
        )

    return {
        "status": "healthy",
        "tempo_ativo": tempo_ativo,
        "fault_active": fault_active
    }


@app.get("/work")
def work(request: Request, fail: bool = False, auto: bool = False, delay_ms: int = Query(default=0, ge=0, le=5000)):
    """Falha deterministica para comparar as duas estrategias."""
    global auto_counter
    request_id = request.headers.get("x-request-id", str(uuid4()))[:128]
    scenario = request.headers.get("x-scenario", "manual")[:64]
    attempt = request.headers.get("x-attempt", "1")[:8]
    with contador_lock:
        if auto:
            auto_counter += 1
            fail = auto_fault_enabled and auto_counter % 2 == 0
        fault_active = auto_fault_enabled if auto else None
        contador["total"] += 1
        contador["falhas" if fail else "sucessos"] += 1
        by_scenario = contador["por_cenario"].setdefault(scenario, {"total": 0, "falhas": 0, "sucessos": 0})
        by_scenario["total"] += 1
        by_scenario["falhas" if fail else "sucessos"] += 1
    if delay_ms:
        registrar("backend_delay", request_id, scenario, attempt=attempt, delay_ms=delay_ms)
        time.sleep(delay_ms / 1000)
    registrar("backend_response", request_id, scenario, attempt=attempt, status=503 if fail else 200,
              error_name="IntermittentBackendError" if fail and auto else None, delay_ms=delay_ms)
    if fail:
        raise HTTPException(status_code=503, detail="Falha simulada no servico")
    return {"status": "ok", "fault_active": fault_active}


@app.get("/fault")
def fault_state():
    with contador_lock:
        return {"enabled": auto_fault_enabled}


@app.post("/fault")
def set_fault(action: FaultAction, authorization: str | None = Header(default=None)):
    global auto_fault_enabled
    if not control_token or not hmac.compare_digest(authorization or "", f"Bearer {control_token}"):
        raise HTTPException(status_code=403, detail="Acesso negado")
    with contador_lock:
        auto_fault_enabled = action.enabled
    registrar("fault_control", str(uuid4()), "agent_action", enabled=action.enabled, reason=action.reason)
    return {"enabled": action.enabled}


@app.get("/stats")
def stats():
    with contador_lock:
        return {**contador, "por_cenario": {name: dict(values) for name, values in contador["por_cenario"].items()}}


@app.get("/events")
def events(limit: int = Query(default=100, ge=1, le=500), request_id: str | None = None, after: int = Query(default=0, ge=0)):
    with contador_lock:
        selected = [entry for entry in eventos if (request_id is None or entry["request_id"] == request_id) and entry["sequence"] > after]
    return selected[:limit] if after else selected[-limit:]
