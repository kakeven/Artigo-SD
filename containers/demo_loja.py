"""Executa pedidos de uma loja de roupas para exercitar os microservicos."""

import argparse
from datetime import datetime
import json
from pathlib import Path
import time
from urllib.parse import quote
from uuid import uuid4

from demo_agente import AGENT, request_json
from relatorio_loja import write_report


ROOT = Path(__file__).resolve().parent
SHOP = "http://127.0.0.1:8001"
INVENTORY = "http://127.0.0.1:8002"
PAYMENT = "http://127.0.0.1:8003"
CAUSE_TARGET = {"overload": "pagamentos", "dependency_timeout": "pagamentos", "circuit_open": "pagamentos",
                "dependency_unavailable": "estoque", "reservation_locked": "estoque"}


def token_from_env() -> str:
    values = dict(line.split("=", 1) for line in (ROOT / ".env").read_text(encoding="ascii").splitlines() if "=" in line)
    return values["AGENT_CONTROL_TOKEN"]


def clothing_item(index: int) -> dict:
    return {"sku": "moletom-capuz" if index % 4 == 0 else "camiseta-basica",
            "size": ("P", "M", "G")[index % 3], "quantity": 1}


def run(strategy: str, cause: str, count: int) -> dict:
    token = token_from_env()
    auth = {"Authorization": f"Bearer {token}"}
    status, episode = request_json(f"{AGENT}/reset?strategy={quote(strategy)}", method="POST", headers=auth)
    if status != 200:
        raise RuntimeError(f"Nao foi possivel iniciar episodio: HTTP {status}")
    status, _ = request_json(f"{INVENTORY}/inventory/reset", method="POST", headers=auth)
    if status != 200:
        raise RuntimeError(f"Nao foi possivel reiniciar o estoque: HTTP {status}")
    target = CAUSE_TARGET[cause]
    for service, url, mode in (("estoque", INVENTORY, cause if target == "estoque" else "intermittent"),
                               ("pagamentos", PAYMENT, cause if target == "pagamentos" else "overload")):
        status, _ = request_json(f"{url}/fault", method="POST", headers=auth,
                                 body={"enabled": service == target, "mode": mode, "reason": "shop_demo"})
        if status != 200:
            raise RuntimeError(f"Nao foi possivel configurar {service}: HTTP {status}")

    results = []
    for index in range(count):
        status, order = request_json(f"{SHOP}/orders", method="POST",
                                     headers={"X-Request-ID": str(uuid4())},
                                     body=clothing_item(index))
        results.append({"number": index + 1, "http_status": status,
                        "timestamp": datetime.now().astimezone().isoformat(),
                        "order_id": order.get("order_id"), "request_id": order.get("request_id"),
                        "sku": order.get("sku"), "size": order.get("size"),
                        "quantity": order.get("quantity"), "status": order.get("status")})
        time.sleep(0.15)
    deadline = time.monotonic() + (90 if strategy == "qwen_memory" else 15)
    while time.monotonic() < deadline:
        _, agent = request_json(f"{AGENT}/status")
        decisions = [entry for entry in agent["decisions"] if entry["timestamp"] >= episode["episode_started_at"]]
        if any(entry["target"] == target for entry in decisions):
            break
        time.sleep(0.5)
    for index in range(3):
        status, order = request_json(f"{SHOP}/orders", method="POST",
                                     headers={"X-Request-ID": str(uuid4())},
                                     body=clothing_item(count + index))
        results.append({"number": count + index + 1, "http_status": status,
                        "timestamp": datetime.now().astimezone().isoformat(),
                        "order_id": order.get("order_id"), "request_id": order.get("request_id"),
                        "sku": order.get("sku"), "size": order.get("size"),
                        "quantity": order.get("quantity"), "status": order.get("status")})
    _, inventory = request_json(f"{INVENTORY}/inventory")
    _, agent = request_json(f"{AGENT}/status")
    return {"shop_type": "clothing", "strategy": strategy, "cause": cause, "target": target,
            "episode_started_at": episode["episode_started_at"],
            "orders": results, "confirmed": sum(item["status"] == "confirmed" for item in results),
            "inventory": inventory,
            "decisions": [entry for entry in agent["decisions"] if entry["timestamp"] >= episode["episode_started_at"]]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", choices=("rf", "memory", "qwen_memory"), default="rf")
    parser.add_argument("--cause", choices=tuple(CAUSE_TARGET), default="overload")
    parser.add_argument("--orders", type=int, default=22)
    args = parser.parse_args()
    if not 6 <= args.orders <= 40:
        parser.error("--orders deve estar entre 6 e 40")
    report = run(args.strategy, args.cause, args.orders)
    output_dir = ROOT / "resultados"
    output_dir.mkdir(exist_ok=True)
    output = output_dir / f"loja_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    visual_report = write_report(output)
    print(f"Pedidos confirmados: {report['confirmed']}/{len(report['orders'])}")
    print(f"Ultimos pedidos: {report['orders'][-3:]}")
    print(f"Decisoes do agente: {len(report['decisions'])}")
    print(f"Relatorio visual: {visual_report}")
    print(f"Dados JSON: {output}")


if __name__ == "__main__":
    main()
