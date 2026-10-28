"""Coleta, diagnostico local e remediacao auditavel."""

from datetime import datetime, timezone
import json
import os
import threading
import time
from uuid import uuid4

import httpx

from model import LocalIncidentModel
from repository import LogRepository
from strategies import choose_action, most_common_error


class AgentService:
    def __init__(self):
        self.sources = {
            "normal": os.getenv("NORMAL_URL", "http://normal:8000"),
            "instavel": os.getenv("UNSTABLE_URL", "http://instavel:8000"),
            "carga": os.getenv("ERROR_URL", "http://carga:8000"),
        }
        self.gateway_url = os.getenv("GATEWAY_URL", "http://protecao:8000")
        self.token = os.getenv("AGENT_CONTROL_TOKEN", "")
        self.repository = LogRepository(os.getenv("LOG_DB_PATH", "/data/observabilidade.db"))
        self.model = LocalIncidentModel()
        self.strategy = os.getenv("AGENT_STRATEGY", "rf")
        if self.strategy not in ("rf", "memory", "qwen_memory"):
            raise ValueError("AGENT_STRATEGY invalida")
        self.client = httpx.Client(timeout=3.0, trust_env=False)
        self.cursors = {name: 0 for name in self.sources}
        self.new_status_count = {name: 0 for name in self.sources}
        self.predictions = {}
        self.last_action = {}
        self.episode_started_at = datetime.now(timezone.utc).isoformat()
        self.stop_event = threading.Event()
        self.thread = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self.run_loop, daemon=True, name="agent-monitor")
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=4)

    def run_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.poll_once()
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
                print(json.dumps({"service": "agente", "event": "poll_error", "error": type(error).__name__}), flush=True)
            self.stop_event.wait(1.0)

    def poll_once(self) -> None:
        for service, base_url in self.sources.items():
            try:
                response = self.client.get(f"{base_url}/events", params={"after": self.cursors[service], "limit": 500})
                response.raise_for_status()
                events = response.json()
                if not isinstance(events, list):
                    continue
                if not events and self.cursors[service]:
                    latest = self.client.get(f"{base_url}/events", params={"limit": 1}).json()
                    if latest and latest[-1]["sequence"] < self.cursors[service]:
                        self.cursors[service] = 0
                    continue
                if events:
                    self.repository.insert_events(service, events)
                    self.cursors[service] = max(int(event["sequence"]) for event in events)
                    self.new_status_count[service] += sum(
                        isinstance(event.get("status"), int) and event.get("event") in ("service_response", "backend_response")
                        and str(event.get("timestamp", "")) >= self.episode_started_at
                        for event in events
                    )
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                continue

        for target in ("normal", "instavel", "carga"):
            if self.new_status_count[target] < 6:
                continue
            if target != "normal":
                try:
                    fault = self.client.get(f"{self.sources[target]}/fault").json()
                    if not fault.get("enabled"):
                        continue
                except (httpx.HTTPError, ValueError):
                    continue
            statuses = self.repository.recent_statuses(target, 12, self.episode_started_at)
            if len(statuses) < 6:
                continue
            label, confidence = self.model.predict(statuses)
            self.predictions[target] = {"type": label, "confidence": round(confidence, 3), "sample_size": len(statuses)}
            if target == "normal":
                continue
            if label == "normal" or confidence < 0.7 or sum(status >= 500 for status in statuses) < 3:
                continue
            if time.monotonic() - self.last_action.get(target, 0) < 30:
                continue
            self.last_action[target] = time.monotonic()
            error_name = most_common_error(self.repository.recent_error_names(target, self.episode_started_at))
            self.mitigate(target, label, confidence, error_name, statuses)

    def mitigate(self, target: str, label: str, confidence: float, error_name: str, statuses: list[int]) -> None:
        verified = False
        action, decision_source = choose_action(self.strategy, target, error_name, statuses, self.repository)
        evidence = self.repository.recent_evidence(target, self.episode_started_at)
        if action:
            try:
                endpoint = "/repair"
                payload = {"action": action, "reason": f"{decision_source}:{label}"}
                response = self.client.post(
                    f"{self.sources[target]}{endpoint}", json=payload,
                    headers={"Authorization": f"Bearer {self.token}"},
                )
                response.raise_for_status()
                probe = self.client.get(
                    f"{self.gateway_url}/route/{target}",
                    headers={"X-Request-ID": str(uuid4())},
                )
                probe_body = probe.json()
                fault_state = self.client.get(f"{self.sources[target]}/fault").json()
                verified = (
                    probe.status_code == 200
                    and probe_body.get("served_by") == target
                    and probe_body.get("fault_active") is False
                    and fault_state.get("enabled") is False
                )
            except (httpx.HTTPError, ValueError):
                pass
        if verified:
            self.repository.remember_success(target, error_name, action)
        timestamp = datetime.now(timezone.utc).isoformat()
        self.repository.add_decision(timestamp, target, label, confidence, action or "no_action", verified, evidence,
                                     error_name, decision_source)
        print(json.dumps({
            "timestamp": timestamp,
            "service": "agente",
            "event": "decision",
            "target": target,
            "predicted_type": label,
            "confidence": round(confidence, 3),
            "action": action or "no_action",
            "strategy": decision_source,
            "error_name": error_name,
            "verified": verified,
            "evidence_request_id": evidence,
        }), flush=True)

    def snapshot(self) -> dict:
        return {
            "model": "local_random_forest_v1",
            "strategy": self.strategy,
            "memory": self.repository.memory_summary(),
            "episode_started_at": self.episode_started_at,
            "predictions": dict(self.predictions),
            "ingested_events": self.repository.counts(),
            "decisions": self.repository.recent_decisions(30),
        }

    def reset_episode(self, strategy: str | None = None) -> None:
        if strategy is not None and strategy not in ("rf", "memory", "qwen_memory"):
            raise ValueError("Estrategia desconhecida")
        for target in ("instavel", "carga"):
            response = self.client.post(
                f"{self.gateway_url}/control/{target}",
                json={"fallback": False, "reason": "new_demo_episode"},
                headers={"Authorization": f"Bearer {self.token}"},
            )
            response.raise_for_status()
            response = self.client.post(
                f"{self.sources[target]}/fault",
                json={"enabled": True, "reason": "new_demo_episode", **({"mode": "overload"} if target == "carga" else {})},
                headers={"Authorization": f"Bearer {self.token}"},
            )
            response.raise_for_status()
        self.episode_started_at = datetime.now(timezone.utc).isoformat()
        self.new_status_count = {name: 0 for name in self.sources}
        self.predictions = {}
        self.last_action = {}
        if strategy is not None:
            self.strategy = strategy
