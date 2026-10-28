"""Executa cinco causas para cada estrategia e gera um relatorio comparativo."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from demo_agente import AGENT, request_json
from demo_loja import CAUSE_TARGET, run, token_from_env
from relatorio_loja import write_report


ROOT = Path(__file__).resolve().parent
STRATEGIES = ("rf", "memory", "qwen_memory")
CAUSES = tuple(CAUSE_TARGET)


def require_ok(url: str, token: str, body: dict | None = None) -> dict:
    status, response = request_json(url, method="POST", headers={"Authorization": f"Bearer {token}"}, body=body)
    if status != 200:
        raise RuntimeError(f"Falha ao preparar comparacao: {url} retornou HTTP {status}")
    return response


def run_comparison(order_count: int) -> dict:
    token = token_from_env()
    require_ok(f"{AGENT}/memory/reset", token)
    print("Preparando memoria com tres reparos verificados de sobrecarga...", flush=True)
    training = []
    for trial in range(3):
        result = run("rf", "overload", order_count)
        verified = any(decision["target"] == "pagamentos" and decision["verified"] for decision in result["decisions"])
        if not verified:
            raise RuntimeError(f"Preparacao da memoria falhou na repeticao {trial + 1}")
        training.append({"cause": "overload", "verified": True})
    _, state = request_json(f"{AGENT}/status")
    baseline = state["memory"]
    if not any(case["target"] == "pagamentos" and case["error_name"] == "OverloadError"
               and case["verified_count"] >= 3 for case in baseline):
        raise RuntimeError("A memoria nao registrou os tres reparos verificados")

    episodes = []
    for strategy in STRATEGIES:
        for cause in CAUSES:
            require_ok(f"{AGENT}/memory/restore", token, {"cases": baseline})
            print(f"Executando {strategy}: {cause} ({len(episodes) + 1}/15)...", flush=True)
            result = run(strategy, cause, order_count)
            episodes.append(result)
    return {"kind": "shop_comparison", "generated_at": datetime.now(timezone.utc).isoformat(),
            "training": training, "baseline_memory": baseline, "order_count": order_count,
            "strategies": list(STRATEGIES), "causes": list(CAUSES), "episodes": episodes}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orders", type=int, default=22, help="Pedidos durante cada incidente; mais tres verificam o reparo")
    args = parser.parse_args()
    if not 6 <= args.orders <= 40:
        parser.error("--orders deve estar entre 6 e 40")
    comparison = run_comparison(args.orders)
    output_dir = ROOT / "resultados"
    output_dir.mkdir(exist_ok=True)
    output = output_dir / f"loja_comparacao_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    output.write_text(json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")
    visual = write_report(output)
    print(f"Relatorio comparativo: {visual}")
    print(f"Dados: {output}")


if __name__ == "__main__":
    main()
