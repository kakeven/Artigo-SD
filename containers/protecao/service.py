"""Politica de protecao das chamadas ao servico de estoque."""

import os
import random
import threading
import time
import json
from collections import deque
from datetime import datetime, timezone
from uuid import uuid4

import httpx


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, cooldown_seconds: float = 5.0):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.consecutive_failures = 0
        self.open_until = 0.0
        self.probe_in_progress = False
        self.lock = threading.Lock()

    def allow(self) -> bool:
        with self.lock:
            now = time.monotonic()
            if self.open_until > now:
                return False
            if self.open_until and not self.probe_in_progress:
                self.probe_in_progress = True
                return True
            if self.probe_in_progress:
                return False
            return True

    def record_success(self) -> None:
        with self.lock:
            self.consecutive_failures = 0
            self.open_until = 0.0
            self.probe_in_progress = False

    def record_failure(self) -> bool:
        with self.lock:
            self.consecutive_failures += 1
            self.probe_in_progress = False
            if self.consecutive_failures >= self.failure_threshold:
                self.open_until = time.monotonic() + self.cooldown_seconds
                return True
            return False

    def snapshot(self) -> dict:
        with self.lock:
            remaining = max(0.0, self.open_until - time.monotonic())
            state = "open" if remaining else "half_open" if self.open_until else "closed"
            return {
                "state": state,
                "consecutive_failures": self.consecutive_failures,
                "seconds_until_probe": round(remaining, 3),
            }


class ProtectedCaller:
    def __init__(self):
        self.backend_url = os.getenv("ESTOQUE_URL", "http://estoque:8000")
        self.timeout = float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "1"))
        self.max_attempts = int(os.getenv("MAX_ATTEMPTS", "2"))
        self.breaker = CircuitBreaker(
            failure_threshold=int(os.getenv("FAILURE_THRESHOLD", "5")),
            cooldown_seconds=float(os.getenv("COOLDOWN_SECONDS", "5")),
        )
        self.client = httpx.Client(timeout=self.timeout, trust_env=False)
        self.lock = threading.Lock()
        self.metrics = {"logical_requests": 0, "backend_attempts": 0, "short_circuited": 0}
        self.events = deque(maxlen=2000)
        self.event_lock = threading.Lock()
        self.event_sequence = 0

    def log_event(self, event: str, request_id: str, scenario: str, **details) -> None:
        with self.event_lock:
            self.event_sequence += 1
            entry = {
                "sequence": self.event_sequence,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "service": "protecao",
                "event": event,
                "request_id": request_id,
                "scenario": scenario,
                **details,
            }
            self.events.append(entry)
        print(json.dumps(entry, ensure_ascii=False), flush=True)

    def call(self, fail: bool, request_id: str | None = None, scenario: str = "manual") -> tuple[int, dict]:
        request_id = (request_id or str(uuid4()))[:128]
        scenario = scenario[:64]
        with self.lock:
            self.metrics["logical_requests"] += 1
        self.log_event("request_received", request_id, scenario, failure_simulated=fail)
        if not self.breaker.allow():
            with self.lock:
                self.metrics["short_circuited"] += 1
            self.log_event("short_circuited", request_id, scenario, status=503)
            return 503, {"status": "blocked", "reason": "circuit_open"}

        for attempt in range(self.max_attempts):
            with self.lock:
                self.metrics["backend_attempts"] += 1
            try:
                response = self.client.get(
                    f"{self.backend_url}/work",
                    params={"fail": fail},
                    headers={
                        "X-Request-ID": request_id,
                        "X-Scenario": scenario,
                        "X-Attempt": str(attempt + 1),
                    },
                )
                self.log_event("backend_attempt", request_id, scenario, attempt=attempt + 1, status=response.status_code)
                if response.status_code == 200:
                    self.breaker.record_success()
                    self.log_event("request_succeeded", request_id, scenario, status=200)
                    return 200, response.json()
                # Apenas falhas transitorias sao elegiveis para retentativa.
                retryable = response.status_code in (429, 502, 503, 504)
            except (httpx.TimeoutException, httpx.TransportError) as error:
                self.log_event("backend_attempt", request_id, scenario, attempt=attempt + 1, error=type(error).__name__)
                retryable = True
            if not retryable or attempt + 1 == self.max_attempts:
                break
            time.sleep(min(0.05 * 2**attempt + random.uniform(0, 0.02), 0.2))

        opened = self.breaker.record_failure()
        self.log_event("request_failed", request_id, scenario, status=503, circuit_opened=opened)
        return 503, {"status": "failed", "reason": "backend_unavailable"}

    def snapshot(self) -> dict:
        with self.lock:
            metrics = dict(self.metrics)
        return {**metrics, "circuit": self.breaker.snapshot()}

    def recent_events(self, limit: int = 100, request_id: str | None = None) -> list[dict]:
        with self.event_lock:
            selected = [entry for entry in self.events if request_id is None or entry["request_id"] == request_id]
        return selected[-limit:]
