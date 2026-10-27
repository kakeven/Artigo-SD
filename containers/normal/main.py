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

app = FastAPI(title="Container Normal")
backend_url = os.getenv("BACKEND_URL", "http://instavel:8000")
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


def log_event(event: str, request_id: str, scenario: str, **details):
    global sequence
    with lock:
        sequence += 1
        entry = {
            "sequence": sequence,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service": "normal",
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
        "service": "normal",
        "message": "Funcionando normalmente"
    }


@app.get("/work")
def work(request: Request):
    request_id = request.headers.get("x-request-id", str(uuid4()))[:128]
    with lock:
        metrics["work"]["success"] += 1
    log_event("service_response", request_id, "agent_demo", status=200)
    return {"service": "normal", "status": "ok"}


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
