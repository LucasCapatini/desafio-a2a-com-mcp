"""Sobe o agente A2A: python -m agente_a2a"""

from __future__ import annotations

import logging
import os
import sys

import uvicorn

from agente_a2a.a2a import criar_app
from agente_a2a.host_mcp import HostMCP


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("agente_a2a").setLevel(logging.INFO)

    host = os.environ.get("AGENTE_HOST", "127.0.0.1")
    porta = int(os.environ.get("AGENTE_PORT", "7300"))
    url_a2a = os.environ.get("AGENTE_URL_PUBLICA", f"http://{host}:{porta}") + "/a2a"
    app = criar_app(url_a2a, HostMCP())
    logging.getLogger("agente_a2a").info("agente A2A em %s (card em /.well-known/agent-card.json)", url_a2a)
    uvicorn.run(app, host=host, port=porta, log_level="warning")


if __name__ == "__main__":
    main()
