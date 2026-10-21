"""Servidor MCP da Central de Salas: tools, resource e a definicao do MCPServer."""

from __future__ import annotations

import os
from typing import Annotated, Literal

from mcp.server.mcpserver import (
    AcceptedElicitation,
    Elicit,
    ElicitationResult,
    MCPServer,
    RequestStateSecurity,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field, create_model

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


class EscolhaDeSala(BaseModel):
    """Forma da resposta da elicitation; o schema real restringe `sala` as alternativas."""

    sala: str


def escolha_de_sala(sala: str, inicio: str, fim: str) -> Elicit[EscolhaDeSala] | None:
    """Resolver do MRTR: decide se a reserva precisa perguntar algo ao cliente.

    Sala livre: nao pergunta (None). Ocupada com alternativas: devolve um Elicit, que o
    SDK transforma em `input_required` com o `requestState` selado. Ocupada sem
    alternativas: erro de execucao. Roda de novo a cada rodada, entao o retry reavalia
    o conflito com os argumentos selados.
    """
    pedido = _validar(sala, inicio, fim)
    if not dominio.conflitos(sala, pedido.inicio, pedido.fim):
        return None
    opcoes = dominio.alternativas(pedido)
    if not opcoes:
        raise ToolError(dominio.ERRO_SEM_ALTERNATIVA)
    schema = create_model(
        "EscolhaDeSalaAlternativa",
        __base__=EscolhaDeSala,
        sala=(Literal[tuple(opcoes)], Field(title="Sala", description="Sala alternativa escolhida")),
    )
    return Elicit(MENSAGEM_ELICITATION, schema)


@mcp.tool(description="Reserva uma sala no intervalo pedido. Em conflito, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[EscolhaDeSala], Resolve(escolha_de_sala)],
) -> ResultadoReserva:
    if not isinstance(escolha, AcceptedElicitation):
        motivo = "recusado" if escolha.action == "decline" else "cancelado"
        return ResultadoReserva(reservado=False, motivo=motivo)
    destino = escolha.data.sala if escolha.data is not None else sala
    try:
        reserva = dominio.reservar(destino, inicio, fim, responsavel)
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


@mcp.resource(
    "politica://uso",
    name="politica-de-uso",
    description="Politica de uso das salas; a primeira linha declara a versao.",
    mime_type="text/markdown",
)
def politica_de_uso() -> str:
    return dominio.POLITICA_TEXTO
