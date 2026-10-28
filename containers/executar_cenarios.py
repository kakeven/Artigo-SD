"""Um comando para subir o laboratorio e mostrar os tres cenarios no terminal."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error

from demo_agente import AGENT, BASE, request_json, run_demo


ROOT = Path(__file__).resolve().parent
SERVICES = ("pedidos", "estoque", "pagamentos", "protecao", "agente")


def run_command(args: list[str], timeout: int) -> None:
    try:
        result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as error:
        raise RuntimeError(f"Comando nao encontrado: {args[0]}") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"Tempo esgotado ao executar: {' '.join(args[:3])}") from error
    if result.returncode:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "falha desconhecida"
        raise RuntimeError(f"Falha ao iniciar os servicos: {detail}. Verifique se o Docker Desktop esta aberto.")


def wait_for_services() -> None:
    endpoints = (
        "http://127.0.0.1:8001/stats",
        "http://127.0.0.1:8002/stats",
        "http://127.0.0.1:8003/stats",
        f"{BASE}/routing",
        f"{AGENT}/status",
    )
    deadline = time.monotonic() + 90
    pending = set(endpoints)
    while pending and time.monotonic() < deadline:
        for url in list(pending):
            try:
                status, _ = request_json(url)
                if status == 200:
                    pending.remove(url)
            except (OSError, urllib.error.URLError, ValueError):
                pass
        if pending:
            time.sleep(0.5)
    if pending:
        raise RuntimeError("Servicos nao responderam: " + ", ".join(sorted(pending)))


def print_table(report: dict, predictions: dict) -> None:
    decisions = {item["target"]: item for item in report["agent_decisions"]}
    specs = (
        ("pedidos", "Pedidos"),
        ("estoque", "Estoque"),
        ("pagamentos", "Pagamentos"),
    )
    headers = ("Cenario", "Chamadas", "200 antes", "503 antes", "200 reparo", "Diagnostico", "Verificado")
    widths = (12, 8, 10, 10, 10, 14, 10)
    divider = "+" + "+".join("-" * (width + 2) for width in widths) + "+"

    def row(values):
        return "| " + " | ".join(str(value).ljust(width)[:width] for value, width in zip(values, widths)) + " |"

    print(divider)
    print(row(headers))
    print(divider)
    for target, label in specs:
        result = report["results"][target]
        decision = decisions.get(target)
        diagnosis = decision["predicted_type"] if decision else predictions.get(target, {}).get("type", "-")
        verified = "sim" if decision and decision["verified"] else "nao" if decision else "n/a"
        print(row((label, result["requests"], result["direct_successes"] - result["repaired_successes"],
                   result["direct_errors"], result["repaired_successes"], diagnosis, verified)))
    print(divider)


def print_decisions(report: dict) -> None:
    print("\nAcoes do agente")
    if not report["agent_decisions"]:
        print("  Nenhuma acao registrada neste episodio.")
        return
    for decision in sorted(report["agent_decisions"], key=lambda item: item["timestamp"]):
        moment = datetime.fromisoformat(decision["timestamp"]).astimezone().strftime("%H:%M:%S")
        confidence = f"{decision['confidence']:.0%}"
        verified = "confirmada" if decision["verified"] else "nao confirmada"
        print(f"  {moment}  {decision['target']}: {decision['predicted_type']} ({confidence})")
        print(f"            erro: {decision.get('error_name') or '-'}; decisor: {decision.get('strategy') or '-'}")
        print(f"            {decision['action']} -> {verified}")
        print(f"            evidencia: {decision['evidence_request_id']}")


def print_evidence(report: dict, logs: list[dict]) -> None:
    ids = {item["evidence_request_id"] for item in report["agent_decisions"] if item["evidence_request_id"]}
    selected = [entry for entry in reversed(logs) if entry.get("request_id") in ids]
    print("\nLogs usados como evidencia")
    if not selected:
        print("  Nenhum log de evidencia retornado pela consulta recente.")
        return
    for entry in selected:
        moment = datetime.fromisoformat(entry["timestamp"]).astimezone().strftime("%H:%M:%S")
        print(f"  {moment}  {entry['service']:<9} {entry['event']:<18} HTTP {entry['status']}  {entry.get('error_name') or '-'}  {entry['request_id']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sem-build", action="store_true", help="Reutiliza as imagens Docker existentes")
    parser.add_argument("--strategy", choices=("rf", "memory", "qwen_memory"), default="rf")
    parser.add_argument("--cause", choices=("overload", "dependency_timeout"), default="overload")
    args = parser.parse_args()
    print("Preparando o laboratorio...")
    run_command([sys.executable, str(ROOT / "preparar_ambiente.py")], timeout=15)
    command = ["docker", "compose", "up", "-d"]
    if not args.sem_build:
        command.append("--build")
    run_command(command + list(SERVICES), timeout=600)
    wait_for_services()
    print(f"Executando pedidos, estoque e pagamentos; estrategia={args.strategy}, causa={args.cause}...\n")

    report = run_demo(args.strategy, args.cause)
    _, agent_status = request_json(f"{AGENT}/status")
    _, logs = request_json(f"{AGENT}/logs?limit=500")
    print_table(report, agent_status["predictions"])
    print_decisions(report)
    print_evidence(report, logs)
    print(f"\nCausa raiz identificada: {report['root_cause_correct']}/{report['root_cause_total']}")

    output_dir = ROOT / "resultados"
    output_dir.mkdir(exist_ok=True)
    output = output_dir / f"execucao_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    output.write_text(json.dumps({"report": report, "predictions": agent_status["predictions"],
                                  "evidence_logs": [entry for entry in logs if entry.get("request_id") in {
                                      item["evidence_request_id"] for item in report["agent_decisions"]
                                  }]}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Relatorio completo: {output}")
    print(f"Verdade-terreno separada: {report['ground_truth_file']}")
    print("Painel: http://127.0.0.1:8004/dashboard")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, KeyError, urllib.error.URLError) as error:
        raise SystemExit(str(error)) from error
