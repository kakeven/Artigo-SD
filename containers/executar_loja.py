"""Prepara a loja de roupas, executa pedidos e gera o relatorio visual."""

import argparse
from pathlib import Path
import subprocess
import sys

from executar_cenarios import SERVICES, wait_for_services


ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sem-build", action="store_true", help="Reutiliza as imagens Docker existentes")
    parser.add_argument("--strategy", choices=("rf", "memory", "qwen_memory"), default="rf")
    parser.add_argument("--cause", choices=("overload", "dependency_timeout"), default="overload")
    parser.add_argument("--orders", type=int, default=22, help="Pedidos durante o incidente; outros 3 verificam o resultado")
    args = parser.parse_args()
    if not 6 <= args.orders <= 40:
        parser.error("--orders deve estar entre 6 e 40")

    print("Preparando a loja de roupas...", flush=True)
    subprocess.run([sys.executable, str(ROOT / "preparar_ambiente.py")], cwd=ROOT, check=True)
    command = ["docker", "compose", "up", "-d"]
    if not args.sem_build:
        command.append("--build")
    subprocess.run(command + list(SERVICES), cwd=ROOT, check=True)
    wait_for_services()
    print("Executando pedidos automaticos e aguardando o agente...", flush=True)
    subprocess.run([
        sys.executable, str(ROOT / "demo_loja.py"),
        "--strategy", args.strategy, "--cause", args.cause, "--orders", str(args.orders),
    ], cwd=ROOT, check=True)


if __name__ == "__main__":
    try:
        main()
    except FileNotFoundError as error:
        raise SystemExit(f"Comando nao encontrado: {error.filename}") from error
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"Execucao interrompida: {error.cmd[0]} retornou {error.returncode}") from error
