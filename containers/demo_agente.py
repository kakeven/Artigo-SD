"""Executa um episodio local com verdade-terreno separada do agente."""

from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4
import json
import time


BASE = "http://127.0.0.1:8004"
AGENT = "http://127.0.0.1:8005"


def request_json(url: str, method: str = "GET", headers: dict | None = None,
                 body: dict | None = None) -> tuple[int, dict]:
    request = Request(url, method=method, headers=headers or {},
                      data=json.dumps(body).encode("utf-8") if body is not None else None)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def run_target(target: str, count: int) -> dict:
    started_at = datetime.now(timezone.utc).isoformat()
    responses = []
    for _ in range(count):
        status, body = request_json(f"{BASE}/route/{target}", headers={"X-Request-ID": str(uuid4())})
        responses.append({"status": status, "served_by": body.get("served_by"), "fault_active": body.get("fault_active")})
        time.sleep(0.15)
    return {
        "started_at": started_at,
        "requests": count,
        "direct_errors": sum(item["status"] >= 500 and item["served_by"] == target for item in responses),
        "direct_successes": sum(item["status"] == 200 and item["served_by"] == target for item in responses),
        "repaired_successes": sum(item["status"] == 200 and item["served_by"] == target and item["fault_active"] is False for item in responses),
        "last_response": responses[-1],
    }


def run_demo(strategy: str = "rf", carga_mode: str = "overload") -> dict:
    if strategy not in ("rf", "memory", "qwen_memory") or carga_mode not in ("overload", "dependency_timeout"):
        raise ValueError("Estrategia ou causa desconhecida")
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        raise SystemExit("Execute python preparar_ambiente.py antes do demo")
    values = dict(line.strip().split("=", 1) for line in env_path.read_text(encoding="ascii").splitlines() if "=" in line)
    token = values["AGENT_CONTROL_TOKEN"]
    status, episode = request_json(f"{AGENT}/reset?strategy={strategy}", method="POST", headers={"Authorization": f"Bearer {token}"})
    if status != 200:
        raise RuntimeError(f"Nao foi possivel reiniciar episodio: HTTP {status}")
    status, _ = request_json("http://127.0.0.1:8003/fault", method="POST",
                             headers={"Authorization": f"Bearer {token}"},
                             body={"enabled": True, "mode": carga_mode, "reason": "experiment_setup"})
    if status != 200:
        raise RuntimeError(f"Nao foi possivel configurar causa: HTTP {status}")

    truth = {
        "episode_started_at": episode["episode_started_at"],
        "faults": [
            {"root_service": "estoque", "fault_type": "intermittent", "error_name": "IntermittentBackendError"},
            {"root_service": "pagamentos", "fault_type": "continuous",
             "error_name": "OverloadError" if carga_mode == "overload" else "DependencyTimeoutError"},
        ],
    }
    results = {
        "pedidos": run_target("pedidos", 8),
        "estoque": run_target("estoque", 22),
        "pagamentos": run_target("pagamentos", 22),
    }
    deadline = time.monotonic() + (90 if strategy == "qwen_memory" else 8)
    while time.monotonic() < deadline:
        _, unstable_fault = request_json("http://127.0.0.1:8002/fault")
        _, direct_error_fault = request_json("http://127.0.0.1:8003/fault")
        if not unstable_fault["enabled"] and not direct_error_fault["enabled"]:
            break
        time.sleep(0.25)
    for target in ("estoque", "pagamentos"):
        confirmation = run_target(target, 5)
        result = results[target]
        result["requests"] += confirmation["requests"]
        result["direct_errors"] += confirmation["direct_errors"]
        result["direct_successes"] += confirmation["direct_successes"]
        result["repaired_successes"] += confirmation["repaired_successes"]
        result["last_response"] = confirmation["last_response"]
    _, agent_status = request_json(f"{AGENT}/status")
    decisions = [
        decision for decision in agent_status["decisions"]
        if decision["timestamp"] >= episode["episode_started_at"]
    ]
    latest = {decision["target"]: decision for decision in decisions if decision["target"] in ("estoque", "pagamentos")}
    expected_actions = {"estoque": "disable_simulated_fault",
                        "pagamentos": "reset_overload" if carga_mode == "overload" else "restore_dependency"}
    correct = sum(
        latest.get(item["root_service"], {}).get("predicted_type") == item["fault_type"]
        and latest.get(item["root_service"], {}).get("action") == expected_actions[item["root_service"]]
        for item in truth["faults"]
    )
    truth["finished_at"] = datetime.now(timezone.utc).isoformat()
    output_dir = Path(__file__).with_name("resultados")
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / f"ground_truth_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    output_path.write_text(json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "results": results,
        "agent_decisions": decisions,
        "root_cause_correct": correct,
        "root_cause_total": len(truth["faults"]),
        "strategy": strategy,
        "carga_mode": carga_mode,
        "ground_truth_file": str(output_path),
    }


def main() -> None:
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
