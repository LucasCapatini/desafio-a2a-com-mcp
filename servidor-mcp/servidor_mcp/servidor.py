"""Servidor MCP da Central de Salas: tools, resource e a definicao do MCPServer."""

from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.mcpserver import Context, MCPServer, RequestStateSecurity
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS, MISSING_REQUIRED_CLIENT_CAPABILITY, InputRequiredResult
from pydantic import BaseModel

from servidor_mcp import dominio

NOME = "central-de-salas"
TTL_REQUEST_STATE = 600  # segundos: 10 minutos, dentro da faixa de 5 a 30 exigida
TAMANHO_MINIMO_SEGREDO = 32


def _segredo_do_ambiente() -> bytes:
    """Chave de integridade do requestState, lida de REQUEST_STATE_SECRET (nunca do codigo).

    Aceita o formato gerado por `secrets.token_hex(32)` (hex, decodificado para bytes)
    ou qualquer texto; em ambos os casos exige pelo menos 32 bytes.
    """
    bruto = os.environ.get("REQUEST_STATE_SECRET", "").strip()
    try:
        segredo = bytes.fromhex(bruto)
    except ValueError:
        segredo = bruto.encode()
    if len(segredo) < TAMANHO_MINIMO_SEGREDO:
        raise SystemExit(
            "REQUEST_STATE_SECRET ausente ou curta: exporte pelo menos 32 bytes aleatorios, por exemplo\n"
            '  export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")'
        )
    return segredo


# O SDK sela o requestState com AES-256-GCM (chave derivada por HKDF), carimba a
# expiracao e o amarra a tool e ao digest dos argumentos originais: adulteracao,
# expiracao ou argumentos divergentes no retry viram -32602. Como a chave vem do
# ambiente e nao do processo, o estado continua valido depois de um restart.
mcp = MCPServer(
    name=NOME,
    version="1.0.0",
    request_state_security=RequestStateSecurity(keys=[_segredo_do_ambiente()], ttl=TTL_REQUEST_STATE),
)

MENSAGEM_ELICITATION = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."
CHAVE_ESCOLHA = "escolha_de_sala"
VERSAO_ESTADO = 1


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    inicio: str
    fim: str
    livre: bool
    conflitos: list[ConflitoOut]


class ResultadoReserva(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


def _validar(sala: str, inicio: str, fim: str) -> dominio.Pedido:
    try:
        return dominio.validar(sala, inicio, fim)
    except dominio.ErroDeRegra as e:
        raise ToolError(str(e)) from None


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**s) for s in dominio.SALAS])


@mcp.tool(description="Informa se a sala esta livre no intervalo e, se nao estiver, quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    pedido = _validar(sala, inicio, fim)
    conflitos = dominio.conflitos(sala, pedido.inicio, pedido.fim)
    return Disponibilidade(
        sala=sala,
        inicio=inicio,
        fim=fim,
        livre=not conflitos,
        conflitos=[ConflitoOut(**{k: c[k] for k in ("id", "inicio", "fim", "responsavel")}) for c in conflitos],
    )


def _exigir_elicitation_form(ctx: Context) -> None:
    """Capability negociada por request: sem elicitation em form mode, -32021 (HTTP 400).

    Mesma regra do SDK: `elicitation: {}` sem modos conta como form (compatibilidade com
    revisoes anteriores); declarar so `url` nao conta.
    """
    capabilities = ctx.client_capabilities
    elicitation = capabilities.elicitation if capabilities is not None else None
    if elicitation is not None and (elicitation.form is not None or elicitation.url is None):
        return
    raise MCPError(
        code=MISSING_REQUIRED_CLIENT_CAPABILITY,
        message="Client did not declare the form elicitation capability required by reservar_sala",
        data={"requiredCapabilities": {"elicitation": {"form": {}}}},
    )


def _pergunta(selado: dict[str, Any]) -> InputRequiredResult:
    """O `input_required`: uma elicitation em form mode e o pedido selado no requestState.

    O `request_state` sai daqui em texto e o SDK (RequestStateBoundary) o cifra e assina
    antes de ir para o fio; no retry o handler so recebe de volta o que ele mesmo emitiu.
    """
    schema = {
        "type": "object",
        "properties": {
            "sala": {
                "type": "string",
                "title": "Sala",
                "description": "Sala alternativa escolhida",
                "enum": selado["alternativas"],
            }
        },
        "required": ["sala"],
    }
    return InputRequiredResult.model_validate(
        {
            "inputRequests": {
                CHAVE_ESCOLHA: {
                    "method": "elicitation/create",
                    "params": {"mode": "form", "message": MENSAGEM_ELICITATION, "requestedSchema": schema},
                }
            },
            "requestState": json.dumps(selado, separators=(",", ":")),
        }
    )


def _concluir(sala: str, inicio: str, fim: str, responsavel: str) -> ResultadoReserva:
    try:
        reserva = dominio.reservar(sala, inicio, fim, responsavel)
    except dominio.ErroDeRegra as e:
        raise ToolError(str(e)) from None
    return ResultadoReserva(
        reserva=reserva["id"],
        sala=reserva["sala"],
        inicio=reserva["inicio"],
        fim=reserva["fim"],
        responsavel=reserva["responsavel"],
        politica=dominio.POLITICA_VERSAO,
    )


def _retomar(ctx: Context) -> ResultadoReserva | InputRequiredResult:
    """O retry: reconstroi o pedido a partir do requestState, nunca dos argumentos reenviados.

    O estado ja chega verificado (integridade, expiracao e vinculo com a tool e com os
    argumentos originais); falhar em qualquer um vira -32602 antes deste ponto. Nada
    foi guardado em memoria entre as rodadas, entao isto funciona apos um restart.
    """
    try:
        selado = json.loads(ctx.request_state or "")
        if selado.get("v") != VERSAO_ESTADO:
            raise ValueError("versao")
    except ValueError:
        raise MCPError(code=INVALID_PARAMS, message="Invalid or expired requestState") from None

    resposta = (ctx.input_responses or {}).get(CHAVE_ESCOLHA)
    if resposta is None:
        return _pergunta(selado)  # retry sem resposta: pergunta de novo, com o mesmo estado
    acao = getattr(resposta, "action", None)
    if acao in ("decline", "cancel"):
        return ResultadoReserva(reservado=False, motivo="recusado" if acao == "decline" else "cancelado")
    escolha = (getattr(resposta, "content", None) or {}).get("sala")
    if acao != "accept" or escolha not in selado["alternativas"]:
        raise ToolError(f"Escolha invalida: {escolha} nao esta entre as alternativas oferecidas")
    return _concluir(escolha, selado["inicio"], selado["fim"], selado["responsavel"])


@mcp.tool(description="Reserva uma sala no intervalo pedido. Em conflito, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str, inicio: str, fim: str, responsavel: str, ctx: Context
) -> ResultadoReserva | InputRequiredResult:
    if ctx.request_state is not None:
        return _retomar(ctx)

    pedido = _validar(sala, inicio, fim)
    if not dominio.conflitos(sala, pedido.inicio, pedido.fim):
        return _concluir(sala, inicio, fim, responsavel)
    opcoes = dominio.alternativas(pedido)
    if not opcoes:
        raise ToolError(dominio.ERRO_SEM_ALTERNATIVA)
    _exigir_elicitation_form(ctx)
    # Tudo que o retry precisa para reconstruir o pedido viaja selado no requestState,
    # inclusive as alternativas oferecidas: a resposta e validada contra elas.
    return _pergunta(
        {
            "v": VERSAO_ESTADO,
            "sala": sala,
            "inicio": inicio,
            "fim": fim,
            "responsavel": responsavel,
            "alternativas": opcoes,
        }
    )


@mcp.resource(
    "politica://uso",
    name="politica-de-uso",
    description="Politica de uso das salas; a primeira linha declara a versao.",
    mime_type="text/markdown",
)
def politica_de_uso() -> str:
    return dominio.POLITICA_TEXTO
