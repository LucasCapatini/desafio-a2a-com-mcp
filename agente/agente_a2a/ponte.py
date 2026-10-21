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
from agente_a2a.tarefas import CANCELED, COMPLETED, FAILED, INPUT_REQUIRED, WORKING, Pausa, Tarefa, Tarefas

PEDIDO = re.compile(r"^\s*reservar\s+sala=(\S+)\s+inicio=(\S+)\s+fim=(\S+)\s+responsavel=(.+?)\s*$")
ESCOLHA = re.compile(r"^\s*escolha=(\S+)\s*$")
RECUSAR = "recusar"
FORMATO = "reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"
CAMPOS_ARTIFACT = ("reserva", "sala", "inicio", "fim", "responsavel")


def linha_de_alternativas(opcoes: list[str]) -> str:
    """A mensagem da pausa, byte a byte: sem prefixo, na ordem do enum da elicitation."""
    return "alternativas: " + ", ".join(opcoes)

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
            pausa = tarefa.pausa
            if tarefa.estado != INPUT_REQUIRED or pausa is None:
                raise ErroA2A(OPERACAO_NAO_SUPORTADA, f"Task {task_id} nao esta aguardando entrada")
            tarefa.historico.append(mensagem)
            if traceparent:
                tarefa.trace = Traceparent(traceparent)

            casamento = ESCOLHA.match(texto_da_mensagem(mensagem))
            escolha = casamento.group(1) if casamento else None
            if escolha == RECUSAR:
                resposta: dict[str, Any] = {"action": "decline"}
            elif escolha in pausa.opcoes:
                resposta = {"action": "accept", "content": {"sala": escolha}}
            else:
                # Escolha fora do enum da elicitation: a Task continua pausada e repete a pergunta.
                tarefa.transitar(INPUT_REQUIRED, linha_de_alternativas(pausa.opcoes))
                return tarefa

            tarefa.transitar(WORKING)
            tarefa.pausa = None
            try:
                # O retry: mesmo tools/call com id novo, a mesma chave do inputRequests e o
                # requestState devolvido exatamente como veio, sem abrir nem reconstruir.
                resultado = await self.host.chamar(
                    TOOL_RESERVA,
                    tarefa.argumentos,
                    tarefa.trace,
                    input_responses={pausa.chave: resposta},
                    request_state=pausa.request_state,
                )
            except MCPError as e:
                tarefa.transitar(FAILED, f"Erro do servidor MCP: {e.error.message}")
                return tarefa
            except Exception as e:
                log.exception("task=%s falha ao falar com o servidor MCP", tarefa.id)
                tarefa.transitar(FAILED, f"Falha ao falar com o servidor MCP: {type(e).__name__}")
                return tarefa
            self._aplicar(tarefa, resultado)
        return tarefa

    def _aplicar(self, tarefa: Tarefa, resultado: Concluido | Pausado) -> None:
        """Converte o resultado MCP no estado da Task."""
        if isinstance(resultado, Pausado):
            # A ponte: input_required do MCP vira TASK_STATE_INPUT_REQUIRED no A2A, e o
            # requestState fica guardado nesta Task (e so nela) ate a resposta chegar.
            if not resultado.opcoes:
                tarefa.transitar(FAILED, "O servidor MCP pediu uma escolha sem oferecer alternativas")
                return
            tarefa.pausa = Pausa(chave=resultado.chave, opcoes=resultado.opcoes, request_state=resultado.request_state)
            tarefa.transitar(INPUT_REQUIRED, linha_de_alternativas(resultado.opcoes))
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
