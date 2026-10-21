"""Servidor MCP da Central de Salas: tools, resource e a definicao do MCPServer."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

from servidor_mcp import dominio

mcp = MCPServer(name="central-de-salas", version="1.0.0")


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


@mcp.tool(description="Reserva uma sala no intervalo pedido, dentro da politica de uso.")
def reservar_sala(sala: str, inicio: str, fim: str, responsavel: str) -> ResultadoReserva:
    _validar(sala, inicio, fim)
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


@mcp.resource(
    "politica://uso",
    name="politica-de-uso",
    description="Politica de uso das salas; a primeira linha declara a versao.",
    mime_type="text/markdown",
)
def politica_de_uso() -> str:
    return dominio.POLITICA_TEXTO
