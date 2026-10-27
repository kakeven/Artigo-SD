"""Gera o segredo local de controle sem exibi-lo no terminal."""

from pathlib import Path
import os
import secrets


path = Path(__file__).with_name(".env")
if path.exists():
    print("containers/.env ja existe; segredo preservado.")
else:
    token = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as stream:
        stream.write(f"AGENT_CONTROL_TOKEN={token}\n")
    print("containers/.env criado com um segredo local aleatorio.")
