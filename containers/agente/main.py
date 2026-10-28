from contextlib import asynccontextmanager
import hmac
import os

from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from service import AgentService
from strategies import ALLOWED_ACTIONS

agent = AgentService()
control_token = os.getenv("AGENT_CONTROL_TOKEN", "")


class MemoryCase(BaseModel):
    target: str
    error_name: str = Field(min_length=1, max_length=80)
    action: str
    verified_count: int = Field(ge=1, le=1000)


class MemorySnapshot(BaseModel):
    cases: list[MemoryCase] = Field(max_length=100)


@asynccontextmanager
async def lifespan(app: FastAPI):
    agent.start()
    yield
    agent.stop()


app = FastAPI(title="Agente local de observabilidade", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/status")
def status():
    return agent.snapshot()


@app.get("/logs")
def logs(limit: int = Query(default=100, ge=1, le=500)):
    return agent.repository.recent_events(limit)


@app.post("/reset")
def reset(strategy: str | None = None, authorization: str | None = Header(default=None)):
    if not control_token or not hmac.compare_digest(authorization or "", f"Bearer {control_token}"):
        raise HTTPException(status_code=403, detail="Acesso negado")
    try:
        agent.reset_episode(strategy)
    except Exception:
        raise HTTPException(status_code=503, detail="Falha ao reiniciar episodio")
    return {"episode_started_at": agent.episode_started_at}


@app.post("/memory/reset")
def reset_memory(authorization: str | None = Header(default=None)):
    if not control_token or not hmac.compare_digest(authorization or "", f"Bearer {control_token}"):
        raise HTTPException(status_code=403, detail="Acesso negado")
    agent.repository.clear_memory()
    return {"memory": []}


@app.post("/memory/restore")
def restore_memory(snapshot: MemorySnapshot, authorization: str | None = Header(default=None)):
    if not control_token or not hmac.compare_digest(authorization or "", f"Bearer {control_token}"):
        raise HTTPException(status_code=403, detail="Acesso negado")
    cases = [case.model_dump() for case in snapshot.cases]
    if any(case["action"] not in ALLOWED_ACTIONS.get(case["target"], set()) for case in cases):
        raise HTTPException(status_code=422, detail="Caso de memoria invalido")
    agent.repository.restore_memory(cases)
    return {"memory": agent.repository.memory_summary()}
