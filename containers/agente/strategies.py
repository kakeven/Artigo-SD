"""Tres politicas comparaveis de escolha de reparo."""

from collections import Counter
from pathlib import Path
import json
import os

import httpx


ALLOWED_ACTIONS = {
    "instavel": {"disable_simulated_fault", "restore_inventory_dependency", "release_reservation_lock"},
    "carga": {"reset_overload", "restore_dependency", "reset_circuit_breaker"},
}
SOURCE_FILES = {
    "instavel": Path("/source/instavel/main.py"),
    "carga": Path("/source/carga/main.py"),
}


def most_common_error(names: list[str]) -> str:
    return Counter(names).most_common(1)[0][0] if names else ""


def choose_action(strategy: str, target: str, error_name: str, statuses: list[int], repository) -> tuple[str | None, str]:
    if strategy in ("memory", "qwen_memory") and error_name:
        remembered = repository.remembered_action(target, error_name)
        if remembered in ALLOWED_ACTIONS[target]:
            return remembered, "memory"
    if strategy == "qwen_memory":
        proposed = ask_qwen(target, error_name, statuses)
        return (proposed, "qwen") if proposed else (None, "qwen_unavailable_or_invalid")
    return ("disable_simulated_fault" if target == "instavel" else "reset_overload", "rf_fallback")


def ask_qwen(target: str, error_name: str, statuses: list[int]) -> str | None:
    """Consulta somente logs resumidos e fonte montada como leitura; nunca executa saida do LLM."""
    source_path = SOURCE_FILES[target]
    try:
        source = source_path.read_text(encoding="utf-8")[:16000]
    except OSError:
        source = "fonte indisponivel"
    actions = sorted(ALLOWED_ACTIONS[target])
    prompt = (
        "Voce analisa um incidente de laboratorio. Escolha UMA acao permitida com base no nome do erro, "
        "na sequencia HTTP e no codigo. Responda somente JSON: {\"action\": \"...\"}. "
        "Se nao houver evidencia suficiente, action deve ser null. "
        f"Servico: {target}. Erro observado: {error_name!r}. Status recentes: {statuses}. "
        f"Acoes permitidas: {actions}. Codigo-fonte em leitura:\n{source}"
    )
    base_url = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434").rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b-instruct-q4_K_M")
    try:
        response = httpx.post(
            f"{base_url}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False, "format": "json",
                  "options": {"temperature": 0, "num_ctx": 4096}},
            timeout=45.0, trust_env=False,
        )
        response.raise_for_status()
        answer = json.loads(response.json()["response"])
        action = answer.get("action") if isinstance(answer, dict) else None
        return action if action in ALLOWED_ACTIONS[target] else None
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None
