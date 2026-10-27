"""Compara a amplificacao de chamadas durante uma falha deterministica."""

import argparse
import json
import time
import urllib.error
import urllib.request
from uuid import uuid4


def get_json(url: str, headers: dict | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def stats(url: str) -> dict:
    status, body = get_json(url)
    if status != 200:
        raise RuntimeError(f"Nao foi possivel ler metricas: {url}")
    return body


def run_baseline(base_url: str, count: int, max_attempts: int, scenario: str) -> dict:
    before = stats(f"{base_url}/stats")
    started = time.monotonic()
    successes = 0
    sample_request_id = None
    for _ in range(count):
        request_id = str(uuid4())
        if sample_request_id is None:
            sample_request_id = request_id
        for attempt in range(max_attempts):
            status, _ = get_json(f"{base_url}/work?fail=true", {
                "X-Request-ID": request_id,
                "X-Scenario": scenario,
                "X-Attempt": str(attempt + 1),
            })
            if status == 200:
                successes += 1
                break
            if status not in (429, 502, 503, 504):
                break
    elapsed = time.monotonic() - started
    after = stats(f"{base_url}/stats")
    return {
        "logical_requests": count,
        "successful_requests": successes,
        "backend_calls": after["total"] - before["total"],
        "duration_seconds": round(elapsed, 3),
        "sample_request_id": sample_request_id,
    }


def run_protected(base_url: str, backend_url: str, count: int) -> dict:
    before = stats(f"{backend_url}/stats")
    before_guard = stats(f"{base_url}/stats")
    started = time.monotonic()
    successes = 0
    sample_request_id = None
    for _ in range(count):
        request_id = str(uuid4())
        if sample_request_id is None:
            sample_request_id = request_id
        status, _ = get_json(f"{base_url}/work?fail=true", {
            "X-Request-ID": request_id,
            "X-Scenario": "protected",
        })
        successes += status == 200
    elapsed = time.monotonic() - started
    after = stats(f"{backend_url}/stats")
    after_guard = stats(f"{base_url}/stats")
    return {
        "logical_requests": count,
        "successful_requests": successes,
        "backend_calls": after["total"] - before["total"],
        "short_circuited": after_guard["short_circuited"] - before_guard["short_circuited"],
        "duration_seconds": round(elapsed, 3),
        "circuit_after": after_guard["circuit"],
        "sample_request_id": sample_request_id,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--baseline-attempts", type=int, default=4)
    parser.add_argument("--bounded-attempts", type=int, default=2)
    parser.add_argument("--backend", default="http://127.0.0.1:8002")
    parser.add_argument("--guard", default="http://127.0.0.1:8004")
    args = parser.parse_args()
    if min(args.requests, args.baseline_attempts, args.bounded_attempts) < 1:
        parser.error("requests e tentativas devem ser positivos")

    baseline = run_baseline(args.backend, args.requests, args.baseline_attempts, "baseline")
    bounded = run_baseline(args.backend, args.requests, args.bounded_attempts, "bounded")
    protected = run_protected(args.guard, args.backend, args.requests)
    reduction = 100 * (1 - protected["backend_calls"] / baseline["backend_calls"])
    breaker_reduction = 100 * (1 - protected["backend_calls"] / bounded["backend_calls"])
    wait = protected["circuit_after"]["seconds_until_probe"]
    if wait:
        time.sleep(wait + 0.1)
    recovery_status, _ = get_json(f"{args.guard}/work?fail=false", {
        "X-Request-ID": str(uuid4()),
        "X-Scenario": "recovery",
    })
    print(json.dumps({
        "scenario": "100% das chamadas ao backend falham com HTTP 503",
        "baseline": baseline,
        "bounded_retries": bounded,
        "protected": protected,
        "backend_call_reduction_percent": round(reduction, 1),
        "breaker_reduction_vs_bounded_percent": round(breaker_reduction, 1),
        "recovery_status": recovery_status,
        "dashboard": f"{args.guard}/dashboard",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
