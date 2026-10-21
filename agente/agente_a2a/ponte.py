"""A ponte: traduz uma Task A2A em chamadas MCP e o resultado MCP de volta em estado de Task.

O agente nao decide nada de dominio. Ele interpreta o pedido em formato fixo,
descobre as tools, le a politica e repassa ao servidor MCP; conflito, regras e
alternativas vem de la.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from mcp.shared.exceptions import MCPError

from agente_a2a.host_mcp import TOOL_RESERVA, Concluido, HostMCP, Pausado, Traceparent
from agente_a2a.tarefas import CANCELED, COMPLETED, FAILED, WORKING, Tarefa, Tarefas

PEDIDO = re.compile(r"^\s*reservar\s+sala=(\S+)\s+inicio=(\S+)\s+fim=(\S+)\s+responsavel=(.+?)\s*$")
FORMATO = "reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"
CAMPOS_ARTIFACT = ("reserva", "sala", "inicio", "fim", "responsavel")

log = logging.getLogger("agente_a2a")


class ErroA2A(Exception):
    """Erro de protocolo A2A, devolvido como `error` de JSON-RPC."""

    def __init__(self, codigo: int, mensagem: str) -> None:
        super().__init__(mensagem)
        self.codigo = codigo
        self.mensagem = mensagem


TASK_NAO_ENCONTRADA = -32001
OPERACAO_NAO_SUPORTADA = -32004


def texto_da_mensagem(mensagem: dict[str, Any]) -> str:
    return " ".join(p["text"] for p in mensagem.get("parts") or [] if isinstance(p, dict) and isinstance(p.get("text"), str))


class Ponte:
    def __init__(self, host: HostMCP, tarefas: Tarefas) -> None:
        self.host = host
        self.tarefas = tarefas

    async def enviar(self, mensagem: dict[str, Any], traceparent: str | None) -> Tarefa:
        """SendMessage: sem taskId abre uma Task nova; com taskId continua a Task pausada."""
        task_id = mensagem.get("taskId")
        if task_id:
            return await self._continuar(task_id, mensagem, traceparent)
        tarefa = self.tarefas.criar(mensagem, Traceparent(traceparent))
        async with tarefa.trava:
            tarefa.transitar(WORKING)
            await self._iniciar(tarefa, texto_da_mensagem(mensagem))
        return tarefa

    async def _iniciar(self, tarefa: Tarefa, texto: str) -> None:
        casamento = PEDIDO.match(texto)
        if not casamento:
            tarefa.transitar(FAILED, f"Pedido invalido: use o formato {FORMATO}")
            return
        sala, inicio, fim, responsavel = casamento.groups()
        tarefa.argumentos = {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}
        try:
            tools = await self.host.listar_tools(tarefa.trace)
            if TOOL_RESERVA not in tools:
                tarefa.transitar(FAILED, f"O servidor MCP nao oferece a tool {TOOL_RESERVA}")
                return
            tarefa.politica = await self.host.versao_politica(tarefa.trace)
            resultado = await self.host.chamar(TOOL_RESERVA, tarefa.argumentos, tarefa.trace)
        except MCPError as e:
            tarefa.transitar(FAILED, f"Erro do servidor MCP: {e.error.message}")
            return
        except Exception as e:  # falha de transporte, servidor fora do ar
            log.exception("task=%s falha ao falar com o servidor MCP", tarefa.id)
            tarefa.transitar(FAILED, f"Falha ao falar com o servidor MCP: {type(e).__name__}")
            return
        self._aplicar(tarefa, resultado)

    async def _continuar(self, task_id: str, mensagem: dict[str, Any], traceparent: str | None) -> Tarefa:
        tarefa = self.tarefas.obter(task_id)
        if tarefa is None:
            raise ErroA2A(TASK_NAO_ENCONTRADA, f"Task nao encontrada: {task_id}")
        async with tarefa.trava:
            if tarefa.terminal:
                raise ErroA2A(OPERACAO_NAO_SUPORTADA, f"Task {task_id} esta em estado terminal: {tarefa.estado}")
            raise ErroA2A(OPERACAO_NAO_SUPORTADA, f"Task {task_id} nao esta aguardando entrada")

    def _aplicar(self, tarefa: Tarefa, resultado: Concluido | Pausado) -> None:
        """Converte o resultado MCP no estado da Task."""
        if isinstance(resultado, Pausado):
            tarefa.transitar(FAILED, "O servidor MCP pediu uma escolha, mas a pausa ainda nao e suportada")
            return
        if resultado.erro:
            tarefa.transitar(FAILED, resultado.texto or "Erro de execucao da tool sem mensagem")
            return
        if not resultado.dados.get("reservado"):
            motivo = resultado.dados.get("motivo") or "recusado"
            tarefa.transitar(CANCELED, f"Nenhuma sala reservada: {motivo}.")
            return
        reserva = {campo: resultado.dados.get(campo) for campo in CAMPOS_ARTIFACT}
        reserva["politica"] = tarefa.politica
        tarefa.artifacts.append(
            {
                "artifactId": f"art-{tarefa.id.removeprefix('task-')}",
                "name": "reserva",
                "parts": [{"text": json.dumps(reserva, ensure_ascii=False)}],
            }
        )
        tarefa.transitar(COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")
