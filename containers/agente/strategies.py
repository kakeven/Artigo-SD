"""Tres politicas comparaveis de escolha de reparo."""

from collections import Counter
from pathlib import Path
import ast
import json
import os

import httpx


ALLOWED_ACTIONS = {
    "estoque": {"disable_simulated_fault", "restore_inventory_dependency", "release_reservation_lock"},
    "pagamentos": {"reset_overload", "restore_dependency", "reset_circuit_breaker"},
}
KNOWN_ERROR_ACTIONS = {
    "estoque": {
        "IntermittentBackendError": "disable_simulated_fault",
        "InventoryDependencyError": "restore_inventory_dependency",
        "ReservationLockError": "release_reservation_lock",
    },
    "pagamentos": {
        "OverloadError": "reset_overload",
        "DependencyTimeoutError": "restore_dependency",
        "CircuitOpenError": "reset_circuit_breaker",
    },
}
SOURCE_FILES = {
    "estoque": Path("/source/estoque/main.py"),
    "pagamentos": Path("/source/pagamentos/main.py"),
}


def relevant_source(target: str) -> str:
    """Extrai as regras de erro e reparo do codigo montado em leitura."""
    try:
        source = SOURCE_FILES[target].read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return "fonte indisponivel"
    excerpts = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(item, ast.Name) and item.id in ("ERROR_NAMES", "REPAIR_ACTIONS")
            for item in node.targets
        ):
            excerpts.append(ast.get_source_segment(source, node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "repair":
            excerpts.append(ast.get_source_segment(source, node))
    return "\n\n".join(part for part in excerpts if part) or "regras de reparo indisponiveis"


def most_common_error(names: list[str]) -> str:
    return Counter(names).most_common(1)[0][0] if names else ""


def choose_action(strategy: str, target: str, error_name: str, statuses: list[int], repository) -> tuple[str | None, str]:
    if strategy in ("memory", "qwen_memory") and error_name:
        remembered = repository.remembered_action(target, error_name)
        if remembered in ALLOWED_ACTIONS[target]:
            return remembered, "memory"
    if strategy == "qwen_memory":
        proposed, failure = ask_qwen(target, error_name, statuses)
        if proposed:
            return proposed, "qwen"
        fallback = KNOWN_ERROR_ACTIONS.get(target, {}).get(error_name)
        if fallback in ALLOWED_ACTIONS[target]:
            return fallback, f"code_fallback_{failure}"
        return None, f"qwen_failed_{failure}"
    return ("disable_simulated_fault" if target == "estoque" else "reset_overload", "rf_fallback")


def ask_qwen(target: str, error_name: str, statuses: list[int]) -> tuple[str | None, str]:
    """Consulta somente logs resumidos e fonte montada como leitura; nunca executa saida do LLM."""
    source = relevant_source(target)
    actions = sorted(ALLOWED_ACTIONS[target])
    prompt = (
        "Voce analisa um incidente simulado. Encontre em ERROR_NAMES o modo correspondente ao erro observado. "
        "Depois, use esse mesmo modo como chave de REPAIR_ACTIONS para escolher a acao. "
        "A funcao repair confirma que outra acao nao corrige a causa. "
        "Nao escolha pela sequencia HTTP quando houver nome de erro, pois todas as causas podem retornar 503. "
        "Responda somente JSON: {\"action\": \"...\"}. Se a relacao nao existir, use null. "
        f"Servico: {target}. Erro observado: {error_name!r}. Status recentes: {statuses}. "
        f"Acoes permitidas: {actions}. Codigo-fonte em leitura:\n{source}"
    )
    configured_url = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434")
    urls = list(dict.fromkeys(url.strip().rstrip("/") for url in (
        [configured_url] + os.getenv("OLLAMA_EXTRA_URLS", "").split(",")
        + ["http://host.docker.internal:11434", "http://gateway.docker.internal:11434"]
    ) if url.strip()))
    configured_model = os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b-instruct-q4_K_M")
    timeout = httpx.Timeout(45.0, connect=3.0)
    failures = set()
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        for base_url in urls:
            try:
                tags = client.get(f"{base_url}/api/tags", timeout=4.0)
                tags.raise_for_status()
                installed = [item["name"] for item in tags.json().get("models", [])
                             if isinstance(item, dict) and isinstance(item.get("name"), str)]
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
                failures.add("connection")
                print(json.dumps({"service": "agente", "event": "ollama_attempt_failed",
                                  "url": base_url, "stage": "connection", "error": type(error).__name__,
                                  "status": getattr(getattr(error, "response", None), "status_code", None)}), flush=True)
                continue
            models = [configured_model] if configured_model in installed else []
            models += [name for name in installed if name.lower().startswith("qwen2.5:") and name not in models]
            if not models:
                failures.add("model_missing")
                print(json.dumps({"service": "agente", "event": "ollama_attempt_failed",
                                  "url": base_url, "stage": "model_missing", "configured_model": configured_model,
                                  "installed_models": installed}), flush=True)
                continue
            for model in models:
                try:
                    response = client.post(
                        f"{base_url}/api/generate",
                        json={"model": model, "prompt": prompt, "stream": False, "format": "json",
                              "options": {"temperature": 0, "num_ctx": 4096}},
                    )
                    response.raise_for_status()
                    answer = json.loads(response.json()["response"])
                    action = answer.get("action") if isinstance(answer, dict) else None
                    if action in ALLOWED_ACTIONS[target]:
                        print(json.dumps({"service": "agente", "event": "ollama_selected",
                                          "url": base_url, "model": model}), flush=True)
                        return action, ""
                    failures.add("invalid_response")
                    print(json.dumps({"service": "agente", "event": "ollama_attempt_failed",
                                      "url": base_url, "model": model, "stage": "invalid_action"}), flush=True)
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
                    failures.add("timeout" if isinstance(error, httpx.TimeoutException) else "generation_error")
                    print(json.dumps({"service": "agente", "event": "ollama_attempt_failed",
                                      "url": base_url, "model": model, "stage": "generation",
                                      "error": type(error).__name__,
                                      "status": getattr(getattr(error, "response", None), "status_code", None)}), flush=True)
    for reason in ("timeout", "generation_error", "invalid_response", "model_missing", "connection"):
        if reason in failures:
            return None, reason
    return None, "unknown"
