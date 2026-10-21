"""O agente como servidor A2A v1.0: Agent Card no well-known e JSON-RPC 2.0 em /a2a."""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agente_a2a.host_mcp import HostMCP
from agente_a2a.ponte import TASK_NAO_ENCONTRADA, ErroA2A, Ponte
from agente_a2a.tarefas import Tarefas

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

log = logging.getLogger("agente_a2a")


def agent_card(url_a2a: str) -> dict[str, Any]:
    return {
        "name": "Central de Salas",
        "description": "Reserva salas de reuniao da Hill Valley Tech.",
        "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
        "version": "1.0.0",
        "supportedInterfaces": [{"url": url_a2a, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "reservar-sala",
                "name": "Reservar sala",
                "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                "tags": ["salas", "agenda"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
                "examples": [
                    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                    "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
                ],
            }
        ],
    }


def _erro(id_: Any, codigo: int, mensagem: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": codigo, "message": mensagem}})


def criar_app(url_a2a: str, host: HostMCP) -> Starlette:
    tarefas = Tarefas()
    ponte = Ponte(host, tarefas)
    card = agent_card(url_a2a)

    async def get_card(_: Request) -> JSONResponse:
        return JSONResponse(card)

    async def send_message(params: dict[str, Any], request: Request) -> dict[str, Any]:
        mensagem = params.get("message")
        if not isinstance(mensagem, dict) or not isinstance(mensagem.get("parts"), list):
            raise ErroA2A(INVALID_PARAMS, "params.message com parts e obrigatorio")
        tarefa = await ponte.enviar(mensagem, request.headers.get("traceparent"))
        return {"task": tarefa.para_json()}

    async def get_task(params: dict[str, Any], _: Request) -> dict[str, Any]:
        task_id = params.get("id")
        if not isinstance(task_id, str):
            raise ErroA2A(INVALID_PARAMS, "params.id e obrigatorio")
        tarefa = tarefas.obter(task_id)
        if tarefa is None:
            raise ErroA2A(TASK_NAO_ENCONTRADA, f"Task nao encontrada: {task_id}")
        limite = params.get("historyLength")
        return {"task": tarefa.para_json(limite if isinstance(limite, int) and limite >= 0 else None)}

    metodos = {"SendMessage": send_message, "GetTask": get_task}

    async def rpc(request: Request) -> JSONResponse:
        try:
            corpo = json.loads(await request.body())
        except ValueError:
            return _erro(None, PARSE_ERROR, "Parse error")
        if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0" or not isinstance(corpo.get("method"), str):
            return _erro(corpo.get("id") if isinstance(corpo, dict) else None, INVALID_REQUEST, "Invalid Request")
        id_ = corpo.get("id")
        metodo = metodos.get(corpo["method"])
        log.info("a2a method=%s id=%s traceparent=%s", corpo["method"], json.dumps(id_), request.headers.get("traceparent", "-"))
        if metodo is None:
            return _erro(id_, METHOD_NOT_FOUND, f"Method not found: {corpo['method']}")
        params = corpo.get("params")
        if not isinstance(params, dict):
            return _erro(id_, INVALID_PARAMS, "params deve ser um objeto")
        try:
            resultado = await metodo(params, request)
        except ErroA2A as e:
            return _erro(id_, e.codigo, e.mensagem)
        except Exception:
            log.exception("erro interno em %s", corpo["method"])
            return _erro(id_, INTERNAL_ERROR, "Internal error")
        return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": resultado})

    @contextlib.asynccontextmanager
    async def ciclo_de_vida(_: Starlette) -> AsyncIterator[None]:
        await host.iniciar()
        try:
            yield
        finally:
            await host.encerrar()

    return Starlette(
        routes=[
            Route("/.well-known/agent-card.json", get_card, methods=["GET"]),
            Route("/a2a", rpc, methods=["POST"]),
        ],
        lifespan=ciclo_de_vida,
    )
