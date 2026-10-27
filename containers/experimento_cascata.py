"""Mede a propagacao de uma dependencia lenta para uma rota independente."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import time
from uuid import uuid4

from experimento import get_json


def read_stats(base_url: str) -> dict:
    status, body = get_json(f"{base_url}/stats")
    if status != 200:
        raise RuntimeError("Nao foi possivel ler as metricas do servico normal")
    return body


def wait_for_active(base_url: str, mode: str, expected: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if read_stats(base_url)["active_dependent"][mode] >= expected:
            return
        time.sleep(0.01)
    raise RuntimeError(f"As chamadas dependentes nao iniciaram no modo {mode}")


def run_scenario(base_url: str, mode: str, independent_requests: int) -> dict:
    before = read_stats(base_url)
    dependency_ids = [str(uuid4()) for _ in range(4)]
    independent_ids = [str(uuid4()) for _ in range(independent_requests)]

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(
            get_json,
            f"{base_url}/dependent?mode={mode}",
            {"X-Request-ID": request_id},
        ) for request_id in dependency_ids]
        wait_for_active(base_url, mode, 4 if mode == "unsafe" else 2)
        independent_statuses = [
            get_json(f"{base_url}/independent?mode={mode}", {"X-Request-ID": request_id})[0]
            for request_id in independent_ids
        ]
        dependency_statuses = [future.result(timeout=5)[0] for future in futures]

    after = read_stats(base_url)
    return {
        "mode": mode,
        "dependent_requests": len(dependency_ids),
        "dependent_rejected": after["dependent"][mode]["rejected"] - before["dependent"][mode]["rejected"],
        "dependent_failed": sum(status != 200 for status in dependency_statuses),
        "independent_requests": independent_requests,
        "independent_success": sum(status == 200 for status in independent_statuses),
        "independent_rejected": after["independent"][mode]["rejected"] - before["independent"][mode]["rejected"],
        "sample_dependency_request_id": dependency_ids[0],
        "sample_independent_request_id": independent_ids[0],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=20, help="Chamadas a rota independente por cenario")
    parser.add_argument("--normal", default="http://127.0.0.1:8001")
    args = parser.parse_args()
    if args.requests < 1:
        parser.error("requests deve ser positivo")

    unsafe = run_scenario(args.normal, "unsafe", args.requests)
    protected = run_scenario(args.normal, "protected", args.requests)
    print(json.dumps({
        "scenario": "dependencia lenta com erro HTTP 503",
        "unsafe": unsafe,
        "protected": protected,
        "independent_success_gain": protected["independent_success"] - unsafe["independent_success"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
