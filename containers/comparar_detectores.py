"""Compara RF, memoria de incidentes e Qwen+memoria em duas causas HTTP 503."""

import argparse
from datetime import datetime
import json
from pathlib import Path

from demo_agente import AGENT, request_json, run_demo
from executar_cenarios import run_command, wait_for_services, ROOT, SERVICES


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sem-build", action="store_true")
    parser.add_argument("--sem-qwen", action="store_true", help="Executa apenas RF e memoria")
    args = parser.parse_args()
    run_command(["python", str(ROOT / "preparar_ambiente.py")], 15)
    command = ["docker", "compose", "up", "-d"]
    if not args.sem_build:
        command.append("--build")
    run_command(command + list(SERVICES), 600)
    wait_for_services()
    env = dict(line.split("=", 1) for line in (ROOT / ".env").read_text(encoding="ascii").splitlines() if "=" in line)
    token = env["AGENT_CONTROL_TOKEN"]
    status, _ = request_json(f"{AGENT}/memory/reset", method="POST",
                             headers={"Authorization": f"Bearer {token}"})
    if status != 200:
        raise RuntimeError("Nao foi possivel limpar a memoria para o experimento")

    print("Preparando memoria com tres reparos verificados de OverloadError...")
    training = [run_demo("rf", "overload") for _ in range(3)]
    _, trained_state = request_json(f"{AGENT}/status")
    print("Memoria:", json.dumps(trained_state["memory"], ensure_ascii=False))

    strategies = ("rf", "memory") if args.sem_qwen else ("rf", "memory", "qwen_memory")
    cases = []
    for strategy in strategies:
        for cause in ("overload", "dependency_timeout"):
            print(f"Executando {strategy} / {cause}...")
            result = run_demo(strategy, cause)
            decisions = {item["target"]: item for item in result["agent_decisions"]}
            choice = decisions.get("pagamentos", {})
            case = {
                "strategy": strategy,
                "cause": cause,
                "error_name": choice.get("error_name"),
                "decision_source": choice.get("strategy"),
                "chosen_action": choice.get("action"),
                "correct_root_cause": result["root_cause_correct"] == 2,
                "repair_verified": bool(choice.get("verified")),
                "pagamentos_503": result["results"]["pagamentos"]["direct_errors"],
                "pagamentos_200_after_repair": result["results"]["pagamentos"]["repaired_successes"],
            }
            cases.append(case)
            print(f"  erro={case['error_name']} acao={case['chosen_action']} fonte={case['decision_source']} reparado={case['repair_verified']}")

    summary = {strategy: {
        "cases": len([case for case in cases if case["strategy"] == strategy]),
        "root_cause_accuracy": sum(case["correct_root_cause"] for case in cases if case["strategy"] == strategy) / 2,
        "repair_success_rate": sum(case["repair_verified"] for case in cases if case["strategy"] == strategy) / 2,
    } for strategy in strategies}
    output = ROOT / "resultados" / f"comparacao_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps({"training": training, "trained_memory": trained_state["memory"],
                                  "cases": cases, "summary": summary}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Relatorio: {output}")


if __name__ == "__main__":
    main()
