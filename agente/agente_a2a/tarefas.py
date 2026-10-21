"""Tasks A2A em memoria: identidade, estado, historico e produto.

O estado pausado da ponte (o requestState do MCP) fica num campo privado da Task e
nunca entra na serializacao devolvida ao cliente A2A.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any

from agente_a2a.host_mcp import Traceparent

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
FAILED = "TASK_STATE_FAILED"
CANCELED = "TASK_STATE_CANCELED"
TERMINAIS = frozenset({COMPLETED, FAILED, CANCELED})

log = logging.getLogger("agente_a2a")


def novo_id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


@dataclass
class Pausa:
    """O que a ponte guarda entre o input_required e o retry. Opaco para o agente."""

    chave: str
    opcoes: list[str]
    request_state: str | None = field(repr=False)


@dataclass
class Tarefa:
    id: str
    context_id: str
    trace: Traceparent
    estado: str = SUBMITTED
    mensagem: dict[str, Any] | None = None
    historico: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    argumentos: dict[str, str] = field(default_factory=dict)
    politica: str = ""
    pausa: Pausa | None = field(default=None, repr=False)
    trava: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def terminal(self) -> bool:
        return self.estado in TERMINAIS

    def transitar(self, estado: str, texto: str | None = None) -> None:
        """Muda o estado; estado terminal e definitivo e nao volta a WORKING."""
        if self.terminal:
            raise RuntimeError(f"Task {self.id} ja esta em estado terminal {self.estado}")
        log.info("task=%s %s -> %s", self.id, self.estado, estado)
        self.estado = estado
        self.mensagem = None
        if texto is not None:
            self.mensagem = {
                "messageId": novo_id("msg"),
                "role": "ROLE_AGENT",
                "parts": [{"text": texto}],
                "taskId": self.id,
                "contextId": self.context_id,
            }
            self.historico.append(self.mensagem)
        if estado in TERMINAIS:
            self.pausa = None

    def para_json(self, limite_historico: int | None = None) -> dict[str, Any]:
        status: dict[str, Any] = {"state": self.estado}
        if self.mensagem is not None:
            status["message"] = self.mensagem
        historico = self.historico if limite_historico is None else self.historico[-limite_historico:] if limite_historico else []
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": historico,
            "artifacts": self.artifacts,
        }


class Tarefas:
    def __init__(self) -> None:
        self._por_id: dict[str, Tarefa] = {}

    def criar(self, mensagem_usuario: dict[str, Any], trace: Traceparent) -> Tarefa:
        tarefa = Tarefa(
            id=novo_id("task"),
            context_id=mensagem_usuario.get("contextId") or novo_id("ctx"),
            trace=trace,
        )
        tarefa.historico.append(mensagem_usuario)
        self._por_id[tarefa.id] = tarefa
        log.info("task=%s criada em %s", tarefa.id, SUBMITTED)
        return tarefa

    def obter(self, task_id: str) -> Tarefa | None:
        return self._por_id.get(task_id)
