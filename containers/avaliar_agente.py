"""Avalia episodios repetidos sem expor a verdade-terreno ao agente."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import time
from uuid import uuid4

from demo_agente import AGENT, request_json, run_target


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def wait_for_actions(timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, unstable_fault = request_json("http://127.0.0.1:8002/fault")
        _, direct_error_fault = request_json("http://127.0.0.1:8003/fault")
        if not unstable_fault["enabled"] and not direct_error_fault["enabled"]:
            return
        time.sleep(0.25)


def run_episode(token: str, index: int) -> dict:
    status, episode = request_json(
        f"{AGENT}/reset", method="POST", headers={"Authorization": f"Bearer {token}"}
    )
    if status != 200:
        raise RuntimeError(f"Falha ao reiniciar episodio {index}: HTTP {status}")
    episode_start = episode["episode_started_at"]

    normal = run_target("pedidos", 8)
    for _ in range(12):
        request_json("http://127.0.0.1:8002/work?fail=false", headers={"X-Request-ID": str(uuid4())})
    time.sleep(1.2)
    _, healthy_status = request_json(f"{AGENT}/status")
    healthy_decisions = [
        decision for decision in healthy_status["decisions"] if decision["timestamp"] >= episode_start
    ]
    false_alarm = bool(healthy_decisions)

    unstable = run_target("estoque", 22)
    error = run_target("pagamentos", 22)
    wait_for_actions()
    _, final_status = request_json(f"{AGENT}/status")
    decisions = [
        decision for decision in final_status["decisions"] if decision["timestamp"] >= episode_start
    ]
    by_target = {decision["target"]: decision for decision in decisions}
    truth = [
        {"root_service": "estoque", "fault_type": "intermittent", "started_at": unstable["started_at"]},
        {"root_service": "pagamentos", "fault_type": "continuous", "started_at": error["started_at"]},
    ]
    delays = []
    for item in truth:
        decision = by_target.get(item["root_service"])
        if decision:
            delta = datetime.fromisoformat(decision["timestamp"]) - datetime.fromisoformat(item["started_at"])
            delays.append(round(delta.total_seconds(), 3))
    return {
        "index": index,
        "episode_started_at": episode_start,
        "healthy_control": {
            "pedidos_requests": normal["requests"],
            "unstable_healthy_requests": 12,
            "false_alarm": false_alarm,
            "predictions": healthy_status["predictions"],
        },
        "faults": truth,
        "results": {"estoque": unstable, "pagamentos": error},
        "decisions": decisions,
        "detected": sum(item["root_service"] in by_target for item in truth),
        "root_cause_correct": sum(
            by_target.get(item["root_service"], {}).get("predicted_type") == item["fault_type"]
            and by_target.get(item["root_service"], {}).get("action") == (
                "disable_simulated_fault" if item["root_service"] == "estoque" else "reset_overload"
            ) for item in truth
        ),
        "remediation_verified": sum(
            bool(by_target.get(item["root_service"], {}).get("verified")) for item in truth
        ),
        "detection_delays_seconds": delays,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episodes", type=int, default=3)
    args = parser.parse_args()
    if args.episodes < 1:
        parser.error("episodes deve ser positivo")
    env = Path(__file__).with_name(".env")
    if not env.exists():
        raise SystemExit("Execute python preparar_ambiente.py antes da avaliacao")
    token = dict(line.split("=", 1) for line in env.read_text(encoding="ascii").splitlines() if "=" in line)["AGENT_CONTROL_TOKEN"]

    episodes = [run_episode(token, index + 1) for index in range(args.episodes)]
    total_faults = 2 * args.episodes
    delays = [delay for episode in episodes for delay in episode["detection_delays_seconds"]]
    summary = {
        "episodes": args.episodes,
        "fault_cases": total_faults,
        "detection_recall": sum(item["detected"] for item in episodes) / total_faults,
        "root_cause_accuracy": sum(item["root_cause_correct"] for item in episodes) / total_faults,
        "repair_success_rate": sum(item["remediation_verified"] for item in episodes) / total_faults,
        "healthy_control_false_alarm_rate": sum(item["healthy_control"]["false_alarm"] for item in episodes) / args.episodes,
        "mean_detection_delay_seconds": round(statistics.mean(delays), 3) if delays else None,
    }
    report = {"summary": summary, "episodes": episodes, "generated_at": utc_now()}
    directory = Path(__file__).with_name("resultados")
    directory.mkdir(exist_ok=True)
    path = directory / f"avaliacao_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": summary, "report_file": str(path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
