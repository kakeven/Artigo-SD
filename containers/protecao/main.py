from pathlib import Path
import os
import hmac
from uuid import uuid4

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from service import ProtectedCaller
from routing import RoutingService

app = FastAPI(title="Protecao contra tempestade de retentativas")
caller = ProtectedCaller()
normal_url = os.getenv("NORMAL_URL", "http://normal:8000")
error_url = os.getenv("ERROR_URL", "http://carga:8000")
agent_url = os.getenv("AGENT_URL", "http://agente:8000")
router_service = RoutingService(caller.log_event)
control_token = os.getenv("AGENT_CONTROL_TOKEN", "")


class ControlAction(BaseModel):
    fallback: bool
    reason: str = Field(min_length=3, max_length=200)


@app.get("/work")
def work(request: Request, response: Response, fail: bool = False):
    request_id = request.headers.get("x-request-id") or str(uuid4())
    scenario = request.headers.get("x-scenario", "manual")
    status, body = caller.call(fail, request_id, scenario)
    response.status_code = status
    response.headers["X-Request-ID"] = request_id[:128]
    return body


@app.get("/stats")
def stats():
    return caller.snapshot()


@app.get("/events")
def events(limit: int = Query(default=100, ge=1, le=500), request_id: str | None = None):
    return caller.recent_events(limit, request_id)


@app.get("/observability")
def observability(limit: int = Query(default=100, ge=1, le=500), request_id: str | None = None):
    result = {"protection": {"stats": caller.snapshot(), "events": caller.recent_events(limit, request_id)}}
    event_params = {"limit": limit}
    if request_id:
        event_params["request_id"] = request_id
    try:
        backend_stats = caller.client.get(f"{caller.backend_url}/stats").json()
        backend_events = caller.client.get(
            f"{caller.backend_url}/events", params=event_params
        ).json()
        result["backend"] = {"reachable": True, "stats": backend_stats, "events": backend_events}
    except (httpx.HTTPError, ValueError) as error:
        result["backend"] = {"reachable": False, "error": type(error).__name__}
    try:
        normal_stats = caller.client.get(f"{normal_url}/stats").json()
        normal_events = caller.client.get(
            f"{normal_url}/events", params=event_params
        ).json()
        result["normal"] = {"reachable": True, "stats": normal_stats, "events": normal_events}
    except (httpx.HTTPError, ValueError) as error:
        result["normal"] = {"reachable": False, "error": type(error).__name__}
    try:
        payment_stats = caller.client.get(f"{error_url}/stats").json()
        payment_events = caller.client.get(f"{error_url}/events", params=event_params).json()
        result["payment"] = {"reachable": True, "stats": payment_stats, "events": payment_events}
    except (httpx.HTTPError, ValueError) as error:
        result["payment"] = {"reachable": False, "error": type(error).__name__}
    try:
        result["shop"] = {"reachable": True, "stats": caller.client.get(f"{normal_url}/shop/stats").json()}
    except (httpx.HTTPError, ValueError) as error:
        result["shop"] = {"reachable": False, "error": type(error).__name__}
    try:
        result["agent"] = {"reachable": True, "status": caller.client.get(f"{agent_url}/status").json()}
    except (httpx.HTTPError, ValueError) as error:
        result["agent"] = {"reachable": False, "error": type(error).__name__}
    return result


@app.get("/dashboard", include_in_schema=False)
def dashboard():
    return FileResponse(Path(__file__).with_name("dashboard.html"), headers={
        "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
    })


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/route/{target}")
def route(target: str, request: Request, response: Response):
    if target not in router_service.targets:
        raise HTTPException(status_code=404, detail="Alvo desconhecido")
    request_id = (request.headers.get("x-request-id") or str(uuid4()))[:128]
    status, body = router_service.route(target, request_id)
    response.status_code = status
    response.headers["X-Request-ID"] = request_id
    return body


@app.get("/routing")
def routing():
    return router_service.state()


@app.post("/control/{target}")
def control(target: str, action: ControlAction, authorization: str | None = Header(default=None)):
    if not control_token:
        raise HTTPException(status_code=503, detail="Controle indisponivel")
    supplied = authorization or ""
    if not hmac.compare_digest(supplied, f"Bearer {control_token}"):
        raise HTTPException(status_code=403, detail="Acesso negado")
    try:
        state = router_service.set_fallback(target, action.fallback)
    except ValueError:
        raise HTTPException(status_code=404, detail="Alvo desconhecido")
    caller.log_event("agent_action", str(uuid4()), "agent_demo", target=target, fallback=action.fallback, reason=action.reason)
    return {"routing": state}
