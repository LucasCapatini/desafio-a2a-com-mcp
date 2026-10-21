"""Sobe o servidor MCP em Streamable HTTP stateless: python -m servidor_mcp"""

from __future__ import annotations

import json
import logging
import os
import sys

import uvicorn
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from servidor_mcp.servidor import mcp

log = logging.getLogger("servidor_mcp")


class RegistroDeRequests:
    """Middleware ASGI: registra no stderr metodo, id e traceparent de todo POST JSON-RPC.

    Le o corpo antes do SDK para registrar inclusive os requests que ele rejeita
    (por exemplo, sem os campos obrigatorios de _meta), e devolve o corpo intacto.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return

        partes: list[bytes] = []
        mensagens: list[Message] = []
        while True:
            mensagem = await receive()
            mensagens.append(mensagem)
            partes.append(mensagem.get("body", b""))
            if mensagem["type"] != "http.request" or not mensagem.get("more_body", False):
                break
        self._registrar(b"".join(partes))

        async def reenviar() -> Message:
            return mensagens.pop(0) if mensagens else await receive()

        await self.app(scope, reenviar, send)

    @staticmethod
    def _registrar(corpo: bytes) -> None:
        try:
            dados = json.loads(corpo)
        except ValueError:
            log.info("request ilegivel (%d bytes)", len(corpo))
            return
        for item in dados if isinstance(dados, list) else [dados]:
            if not isinstance(item, dict):
                continue
            params = item.get("params") if isinstance(item.get("params"), dict) else {}
            meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
            alvo = params.get("name") or params.get("uri")
            log.info(
                "method=%s id=%s%s traceparent=%s",
                item.get("method"),
                json.dumps(item.get("id")),
                f" name={alvo}" if alvo else "",
                meta.get("traceparent", "-"),
            )


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    log.setLevel(logging.INFO)

    host = os.environ.get("MCP_HOST", "127.0.0.1")
    porta = int(os.environ.get("MCP_PORT", "7301"))
    app = RegistroDeRequests(mcp.streamable_http_app(stateless_http=True, json_response=True, host=host))
    log.info("servidor MCP em http://%s:%d/mcp", host, porta)
    uvicorn.run(app, host=host, port=porta, log_level="warning")


if __name__ == "__main__":
    main()
