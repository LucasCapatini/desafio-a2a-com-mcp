# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Contexto

Entrega de um desafio do MBA (curso de MCP e A2A): um servidor MCP e um agente A2A, que é host MCP por dentro. O enunciado original está no commit inicial (`git show 263f17e:README.md`) e é a fonte de verdade para regras, mensagens de erro exatas e critérios de aceite. O `README.md` atual é a documentação da entrega e precisa manter as quatro seções exigidas: Como rodar, Onde a ponte acontece, Decisões técnicas e Saída do validador.

**Proibido alterar** `dados/`, `validador/` e `exemplos/`, porque o avaliador roda o validador do repositório original. `exemplos/wire/` traz o formato exato de cada request e response que o validador envia e espera.

## Comandos

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -e ./servidor-mcp -e ./agente

export REQUEST_STATE_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
python -m servidor_mcp      # porta 7301, POST /mcp; loga cada request no stderr
python -m agente_a2a        # porta 7300, POST /a2a e GET /.well-known/agent-card.json

python validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

- O validador (36 verificações; 1 a 20 contra o MCP, 21 a 36 contra o A2A) só é confiável com **os dois processos recém-iniciados**: as reservas ficam em memória e uma execução altera a seguinte. Antes de rodar, confirme que nenhum processo antigo segura as portas 7300/7301; senão os novos falham no bind e o validador testa código velho.
- Não há suíte de testes própria. A verificação é o validador mais o Fluxo do avaliador (passos 7 a 13 do enunciado), que inclui reiniciar o servidor MCP entre o `input_required` e o retry.
- O servidor não sobe sem `REQUEST_STATE_SECRET` (mínimo de 32 bytes). Para o retry sobreviver a um restart, use a mesma secret.

## Arquitetura

Dois processos que só conversam por HTTP. O agente nunca importa código do servidor.

**`servidor-mcp/servidor_mcp/`**: `MCPServer` do SDK `mcp==2.3.0` em `stateless_http` + `json_response`.
- `dominio.py` concentra toda regra de negócio: validação, conflito e alternativas.
- `servidor.py` declara as tools e o resource. A `reservar_sala` faz o MRTR **à mão**: devolve `InputRequiredResult` com o pedido e as alternativas em `request_state`, e no retry (`ctx.request_state`, `ctx.input_responses`) reconstrói tudo a partir do estado. O `RequestStateBoundary` do SDK cifra e assina o estado (AES-GCM, TTL de 600 s, vinculado à tool e aos argumentos). **Não volte a usar `Resolve(...)`/`Elicit`**: o resolver refaz a pergunta a cada rodada e descarta a resposta quando as alternativas mudam, o que quebra o retry depois de um restart (passo 12 do avaliador).
- `__main__.py` contém um middleware ASGI que lê o corpo antes do SDK para logar método, id, `traceparent` e capabilities inclusive de requests rejeitados.
- O SDK já cuida de: `-32602`/HTTP 400 para `_meta` incompleto, `-32020` para header divergente do corpo, `-32602` para tool ou resource inexistente e para `requestState` inválido.

**`agente/agente_a2a/`**:
- `host_mcp.py` é o cliente MCP. Usa `client.session.call_tool(..., allow_input_required=True)` para receber o `input_required` cru, **sem callback de elicitation**, e sobrescreve `_build_capabilities` para declarar só `{"elicitation": {"form": {}}}`, já que o SDK declararia `form` e `url` juntos. `Traceparent` preserva o trace-id e gera um span-id por request.
- `ponte.py` é o coração do desafio. `_iniciar` faz, por Task, `tools/list`, `resources/read` e `tools/call`. `_aplicar` converte `Pausado` em `TASK_STATE_INPUT_REQUIRED` com a linha exata `alternativas: a, b` e guarda o `requestState` na Task. `_continuar` trata `escolha=<id>|recusar` e refaz o `tools/call` com a mesma chave e o estado ecoado.
- `tarefas.py` guarda as Tasks em memória, com um lock por Task. `Tarefa.pausa` nunca é serializado: o `requestState` não pode vazar em nenhuma resposta A2A, e a verificação 34 confere isso.
- `a2a.py` publica o Agent Card (idêntico a `exemplos/wire/07`) e o JSON-RPC (`SendMessage`, `GetTask`, com respostas no formato `{"task": ...}`). Erros: `-32001` para Task inexistente e `-32004` para Task em estado terminal.

O agente não implementa regra de sala, só traduz protocolo. A única validação local é conferir se a escolha está no `enum` da elicitation (verificação 29).
