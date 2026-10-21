"""O agente como host MCP: cliente do servidor de salas, falando Streamable HTTP.

Cada request carrega o envelope completo em `_meta` (versao, clientInfo e
capabilities, carimbado pelo SDK) mais o `traceparent` da Task. Nada de sessao: o
cliente fica vivo entre chamadas, mas nenhum request depende de outro.

O agente nunca responde a elicitation por conta propria. Nao ha callback de
elicitation: o `input_required` volta cru (`allow_input_required=True`) para a
ponte decidir o que fazer com ele.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Any

import mcp_types as types
from mcp import Client

PROTOCOLO = "2026-07-28"
URL_PADRAO = "http://127.0.0.1:7301/mcp"
URI_POLITICA = "politica://uso"
TOOL_RESERVA = "reservar_sala"

# Capability declarada em todo request: elicitation apenas em form mode.
CAPABILITIES = types.ClientCapabilities(elicitation=types.ElicitationCapability(form=types.FormElicitationCapability()))


class Traceparent:
    """W3C trace context: mantem o trace-id do cliente A2A e gera um span-id por request MCP."""

    def __init__(self, cabecalho: str | None) -> None:
        partes = (cabecalho or "").strip().split("-")
        valido = len(partes) == 4 and len(partes[1]) == 32 and len(partes[2]) == 16 and partes[1] != "0" * 32
        self.trace_id = partes[1].lower() if valido else secrets.token_hex(16)
        self.flags = partes[3] if valido else "01"

    def proximo(self) -> str:
        return f"00-{self.trace_id}-{secrets.token_hex(8)}-{self.flags}"


@dataclass
class Concluido:
    """Resultado final de tools/call (`resultType` complete)."""

    erro: bool
    texto: str
    dados: dict[str, Any]


@dataclass
class Pausado:
    """`input_required`: a pergunta a devolver ao cliente A2A e o estado a guardar, opaco."""

    chave: str
    opcoes: list[str]
    request_state: str | None


class HostMCP:
    def __init__(self, url: str | None = None) -> None:
        self.url = url or os.environ.get("MCP_URL", URL_PADRAO)
        self._cliente: Client | None = None

    async def iniciar(self) -> None:
        cliente = Client(
            self.url,
            mode=PROTOCOLO,  # versao fixada: sem probe, sem handshake
            client_info=types.Implementation(name="agente-central-de-salas", version="1.0.0"),
            cache=None,
        )
        original = cliente._build_session

        async def sessao_so_form(exit_stack: Any) -> Any:
            # O SDK so declara elicitation quando ha callback, e entao declara form e
            # url juntos. O agente nao tem callback (nunca responde sozinho) e suporta
            # apenas form mode, entao fixamos a capability declarada.
            sessao = await original(exit_stack)
            sessao._build_capabilities = lambda _versao: CAPABILITIES
            return sessao

        cliente._build_session = sessao_so_form  # type: ignore[method-assign]
        self._cliente = await cliente.__aenter__()

    async def encerrar(self) -> None:
        if self._cliente is not None:
            await self._cliente.__aexit__(None, None, None)
            self._cliente = None

    @property
    def _sessao(self) -> Any:
        if self._cliente is None:
            raise RuntimeError("HostMCP nao iniciado")
        return self._cliente.session

    async def listar_tools(self, trace: Traceparent) -> list[str]:
        """tools/list em runtime: o agente nao carrega lista fixa de ferramentas."""
        nomes: list[str] = []
        cursor: str | None = None
        while True:
            params = types.PaginatedRequestParams(cursor=cursor, _meta={"traceparent": trace.proximo()})
            resultado = await self._sessao.list_tools(params=params)
            nomes.extend(t.name for t in resultado.tools)
            cursor = resultado.next_cursor
            if not cursor:
                return nomes

    async def versao_politica(self, trace: Traceparent) -> str:
        """Le o resource da politica e extrai a versao declarada na primeira linha."""
        resultado = await self._sessao.read_resource(URI_POLITICA, meta={"traceparent": trace.proximo()})
        texto = next((c.text for c in resultado.contents if getattr(c, "text", None)), "")
        primeira = texto.splitlines()[0] if texto else ""
        chave, _, valor = primeira.partition(":")
        return valor.strip() if chave.strip() == "versao" else ""

    async def chamar(
        self,
        nome: str,
        argumentos: dict[str, Any],
        trace: Traceparent,
        *,
        input_responses: dict[str, Any] | None = None,
        request_state: str | None = None,
    ) -> Concluido | Pausado:
        """tools/call; no retry leva inputResponses e o requestState ecoado sem modificacao.

        Cada chamada e um request novo, com id de JSON-RPC proprio atribuido pelo SDK.
        """
        resultado = await self._sessao.call_tool(
            nome,
            argumentos,
            input_responses=input_responses,
            request_state=request_state,
            meta={"traceparent": trace.proximo()},
            allow_input_required=True,
        )
        if isinstance(resultado, types.InputRequiredResult):
            return _pausa(resultado)
        texto = " ".join(c.text for c in resultado.content if getattr(c, "text", None))
        return Concluido(erro=bool(resultado.is_error), texto=texto, dados=resultado.structured_content or {})


def _pausa(resultado: types.InputRequiredResult) -> Pausado:
    """Extrai a unica elicitation em form mode e as opcoes do schema (enum ou const)."""
    pedidos = resultado.model_dump(by_alias=True, mode="json", exclude_none=True).get("inputRequests") or {}
    if len(pedidos) != 1:
        raise ValueError(f"esperava uma unica entrada em inputRequests, vieram {len(pedidos)}")
    chave, pedido = next(iter(pedidos.items()))
    params = pedido.get("params") or {}
    if pedido.get("method") != "elicitation/create" or params.get("mode", "form") != "form":
        raise ValueError("inputRequest nao e uma elicitation em form mode")
    campo = ((params.get("requestedSchema") or {}).get("properties") or {}).get("sala") or {}
    opcoes = list(campo.get("enum") or ([campo["const"]] if "const" in campo else []))
    return Pausado(chave=chave, opcoes=opcoes, request_state=resultado.request_state)
