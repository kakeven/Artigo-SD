"""Mostra a memoria promovendo uma correcao apos tres incidentes verificados."""

from datetime import datetime
import json

from demo_agente import AGENT, request_json, run_demo
from executar_cenarios import ROOT, SERVICES, run_command, wait_for_services


def main() -> None:
    run_command(["python", str(ROOT / "preparar_ambiente.py")], 15)
    run_command(["docker", "compose", "up", "-d"] + list(SERVICES), 600)
    wait_for_services()
    env = dict(line.split("=", 1) for line in (ROOT / ".env").read_text(encoding="ascii").splitlines() if "=" in line)
    token = env["AGENT_CONTROL_TOKEN"]
    status, _ = request_json(f"{AGENT}/memory/reset", method="POST",
                             headers={"Authorization": f"Bearer {token}"})
    if status != 200:
        raise RuntimeError("Falha ao limpar memoria")

    learning = []
    for number in range(1, 4):
        report = run_demo("qwen_memory", "dependency_timeout")
        decision = next((item for item in report["agent_decisions"] if item["target"] == "pagamentos"), {})
        observation = {"incident": number, "error_name": decision.get("error_name"),
                       "decision_source": decision.get("strategy"), "action": decision.get("action"),
                       "verified": bool(decision.get("verified"))}
        learning.append(observation)
        print(json.dumps(observation, ensure_ascii=False), flush=True)
        if not observation["verified"]:
            raise RuntimeError("Qwen nao reparou o incidente; memoria nao pode aprender")

    _, snapshot = request_json(f"{AGENT}/status")
    memory_case = next((item for item in snapshot["memory"] if item["target"] == "pagamentos"
                        and item["error_name"] == "DependencyTimeoutError"), None)
    if memory_case is None or memory_case["verified_count"] < 3:
        raise RuntimeError("A memoria nao registrou tres reparos verificados")

    report = run_demo("memory", "dependency_timeout")
    decision = next((item for item in report["agent_decisions"] if item["target"] == "pagamentos"), {})
    result = {"learning_incidents": learning, "memory_case": memory_case,
              "reused_action": decision.get("action"), "reused_from": decision.get("strategy"),
              "reused_verified": bool(decision.get("verified"))}
    path = ROOT / "resultados" / f"aprendizado_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Relatorio: {path}")
    if result["reused_from"] != "memory" or result["reused_action"] != "restore_dependency" or not result["reused_verified"]:
        raise RuntimeError("A reutilizacao da memoria nao foi confirmada")


if __name__ == "__main__":
    main()
