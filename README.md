# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio do MBA Engenharia de Software com IA (curso de MCP e A2A). São dois processos Python separados que conversam só por HTTP:

| Processo | Pasta | Porta | Endpoints |
|---|---|---|---|
| Servidor MCP (Streamable HTTP, stateless, revisão `2026-07-28`) | `servidor-mcp/` | `7301` | `POST /mcp` |
| Agente: host MCP por dentro, servidor A2A v1.0 (JSON-RPC) por fora | `agente/` | `7300` | `GET /.well-known/agent-card.json`, `POST /a2a` |

Stack: Python ≥ 3.10, SDK oficial `mcp==2.3.0`, `starlette==1.9.0`, `uvicorn==0.54.0`, com versões travadas nos `pyproject.toml` de cada processo. O lado A2A não usa SDK: é um JSON-RPC 2.0 escrito à mão sobre Starlette, porque o desafio só exige `SendMessage` e `GetTask`. Não há LLM em lugar nenhum: o agente interpreta um pedido em formato fixo e decide por regra.

## Como rodar

A partir de um clone limpo, na raiz do repositório:

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows (PowerShell): .venv\Scripts\Activate.ps1
pip install -e ./servidor-mcp -e ./agente
```

**Terminal 1, servidor MCP.** Ele exige a chave de integridade do `requestState` em `REQUEST_STATE_SECRET`, com pelo menos 32 bytes aleatórios. Gere a sua e exporte-a no terminal do servidor; o processo se recusa a subir sem ela.

```bash
source .venv/bin/activate
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
python -m servidor_mcp
```

```powershell
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
$env:REQUEST_STATE_SECRET = python -c "import secrets; print(secrets.token_hex(32))"
python -m servidor_mcp
```

Para testar o retry depois de reiniciar o servidor, reinicie no mesmo terminal: a variável continua exportada e o `requestState` emitido antes do restart continua válido.

**Terminal 2, agente.**

```bash
source .venv/bin/activate
python -m agente_a2a
```

**Terminal 3, validador.** Rode com os dois processos recém-iniciados, porque as reservas ficam em memória e uma execução muda o resultado da seguinte.

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Variáveis opcionais, todas com os padrões do enunciado:

| Variável | Padrão | Uso |
|---|---|---|
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `7301` | bind do servidor MCP |
| `AGENTE_HOST` / `AGENTE_PORT` | `127.0.0.1` / `7300` | bind do agente |
| `AGENTE_URL_PUBLICA` | `http://<AGENTE_HOST>:<AGENTE_PORT>` | base da URL publicada no Agent Card |
| `MCP_URL` | `http://127.0.0.1:7301/mcp` | onde o agente encontra o servidor MCP |
| `DADOS_DIR` | `dados/` do repositório | origem de salas, reservas e política |

O stderr do servidor MCP registra uma linha por request, inclusive os que ele rejeita, com método, id, nome da tool ou URI, `traceparent`, capabilities declaradas e a marca `retry=sim` quando o request traz `requestState`:

```
method=tools/list id=1 traceparent=00-26c6ef84...-eb32501e1ee64608-01 capabilities={"elicitation":{"form":{}}}
method=tools/call id=6 name=reservar_sala traceparent=00-26c6ef84...-e243372c3606d7db-01 capabilities={"elicitation":{"form":{}}}
method=tools/call id=7 name=reservar_sala traceparent=00-26c6ef84...-9a5308e49295f44d-01 capabilities={"elicitation":{"form":{}}} retry=sim
```

Os passos manuais 7 a 9 do Fluxo do avaliador (`sala-garagem` das 14h às 15h listando `sala-fusca, sala-mirante`) pressupõem os processos recém-iniciados. Depois de uma rodada do validador, as duas alternativas já estão reservadas nesse horário, e o mesmo pedido termina, corretamente, em `Sem alternativas disponiveis no intervalo`.

## Onde a ponte acontece

O `input_required` do MCP vira `TASK_STATE_INPUT_REQUIRED` em [`agente/agente_a2a/ponte.py:139`](agente/agente_a2a/ponte.py#L139), dentro de `Ponte._aplicar`. Quando o resultado do `tools/call` é um `Pausado`, o agente guarda a chave do `inputRequests`, as opções do `enum` e o `requestState` no campo privado `Tarefa.pausa` daquela Task (linha 145). Em seguida a Task transita para `INPUT_REQUIRED` com a mensagem exata `alternativas: <ids>`. O resultado chega cru porque o host MCP chama o SDK com `allow_input_required=True` e não registra callback de elicitation ([`agente/agente_a2a/host_mcp.py:131`](agente/agente_a2a/host_mcp.py#L131)), então o cliente nunca responde a pergunta sozinho.

O `requestState` volta para o servidor em [`agente/agente_a2a/ponte.py:120`](agente/agente_a2a/ponte.py#L120), em `Ponte._continuar`. O `SendMessage` de continuação traz o `taskId` e o texto `escolha=<id>` ou `escolha=recusar`. O agente refaz o mesmo `tools/call` com os argumentos originais, `inputResponses` sob a mesma chave (`accept` com `{"sala": ...}` ou `decline`) e o `requestState` ecoado byte a byte. O SDK atribui um id de JSON-RPC novo a esse request. Uma escolha fora do `enum` não chega ao servidor: a Task continua pausada e repete a linha de alternativas (linha 112).

Do lado do servidor, a pergunta nasce em `_pergunta` e o retry é reconstruído em `_retomar` ([`servidor-mcp/servidor_mcp/servidor.py:135`](servidor-mcp/servidor_mcp/servidor.py#L135) e [`:181`](servidor-mcp/servidor_mcp/servidor.py#L181)).

## Decisões técnicas

**Proteção do `requestState`.** O servidor usa o `RequestStateSecurity` do SDK, alimentado por `REQUEST_STATE_SECRET`. O handler da tool emite o estado em texto e o `RequestStateBoundary` do SDK o sela antes de ir para o fio, com AES-256-GCM e chave derivada da secret por HKDF-SHA256. É criptografia autenticada: o conteúdo fica ilegível, e qualquer byte trocado falha a verificação da tag. O envelope selado também carrega:

- a expiração;
- o método e o nome da tool;
- o digest dos argumentos originais.

Na volta, o SDK verifica tudo isso antes de chamar a tool. Um estado adulterado, expirado ou apresentado com argumentos diferentes dos selados é recusado com `-32602` (`Invalid or expired requestState`), e o handler nunca vê esse estado. A chave nunca está no código, e o processo não sobe sem ela ou com menos de 32 bytes.

**Validade.** O `requestState` vale 10 minutos (`ttl=600`, dentro da faixa de 5 a 30 minutos).

**O que viaja no estado.** Viaja o pedido inteiro (`sala`, `inicio`, `fim`, `responsavel`) e as alternativas oferecidas. O servidor não guarda nada entre o `input_required` e o retry. No retry, `_retomar` reconstrói o pedido só a partir do estado: os argumentos reenviados pelo cliente nunca são usados, e a escolha é validada contra as alternativas seladas. Como a chave vem do ambiente e não do processo, um retry apresentado depois de reiniciar o servidor conclui normalmente. O estado não é de uso único: o mesmo `requestState` pode ser respondido com `decline` depois de uma tentativa adulterada, e o validador exercita exatamente isso.

**Por que `InputRequiredResult` explícito, e não `Resolve(...)`.** O primeiro desenho usava o resolver do SDK (`Annotated[..., Resolve(fn)]` com `Elicit`). Ele passava nas 36 verificações, mas falhava no passo 12 do avaliador. O resolver roda de novo a cada rodada e só aceita a resposta se a pergunta renderizada for idêntica à que foi feita. Depois de um restart, as reservas em memória voltam ao estado inicial, as alternativas mudam e o SDK descarta a resposta. O log mostrava: `Discarding the answer for resolver ...: the question changed since it was asked`. Selar as alternativas no estado e devolver o `InputRequiredResult` pela própria tool resolve isso e segue a letra do enunciado ("reconstrói o pedido original a partir do próprio `requestState`"). A checagem de capability (`-32021` com `requiredCapabilities`, HTTP 400) fica em `_exigir_elicitation_form`, com a mesma regra do SDK.

**Capability de elicitation declarada pelo agente.** No SDK `mcp==2.3.0`, o `ClientSession` só declara elicitation quando há um callback registrado, e nesse caso declara `form` e `url` juntos (`mcp/client/session.py`, linhas 634 a 638):

```python
elicitation = (
    types.ElicitationCapability(form=types.FormElicitationCapability(), url=types.UrlElicitationCapability())
    if self._elicitation_callback is not _default_elicitation_callback
    else None
)
```

O agente não pode ter callback, porque responderia a pergunta sozinho e a Task nunca pausaria. Ele também só suporta form mode. Por isso `HostMCP.iniciar` fixa a capability declarada em `{"elicitation": {"form": {}}}` ([`agente/agente_a2a/host_mcp.py:81`](agente/agente_a2a/host_mcp.py#L81)). Todo o resto do envelope continua sendo carimbado pelo SDK: `_meta` com `protocolVersion`, `clientInfo` e `clientCapabilities`, e os headers `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name`.

**Estado das Tasks.** As Tasks ficam em memória no processo do agente, em um dicionário `id -> Tarefa` (`agente/agente_a2a/tarefas.py`). Cada Task tem um `asyncio.Lock` próprio, então duas Tasks pausadas ao mesmo tempo não compartilham nada e cada uma retoma com o seu `requestState`. A pausa fica no campo `Tarefa.pausa`, que `para_json` nunca serializa: o `requestState` não aparece no card, no artifact, no histórico nem em mensagens. Estado terminal é definitivo. `transitar` recusa sair de `COMPLETED`, `FAILED` ou `CANCELED`, e um `SendMessage` para uma Task terminal recebe `-32004` (`UnsupportedOperationError`). Uma Task inexistente recebe `-32001`.

**Fluxo de uma Task.** O fluxo é `SUBMITTED` → `WORKING`, seguido de `tools/list` (descoberta em runtime, sem lista fixa), `resources/read politica://uso` (a versão vem da primeira linha) e `tools/call reservar_sala`. Todos esses requests MCP carregam o `traceparent` com o trace-id do header A2A e um span-id novo por request. Sem header, o agente gera um trace-id para a Task. Os desfechos são:

- `isError` → `FAILED`, com a mensagem exata da tool no status e no histórico;
- reserva criada → `COMPLETED`, com o artifact `reserva`, cujo campo `politica` é a versão lida pelo agente;
- recusa → `CANCELED`.

**Domínio.** Fica inteiro no servidor MCP (`servidor-mcp/servidor_mcp/dominio.py`):

- validação na ordem sala → intervalo → janela (08:00 a 20:00 em -03:00) → duração (no máximo 2h);
- conflito por sobreposição estrita (`a.inicio < b.fim && b.inicio < a.fim`);
- alternativas: salas livres com capacidade ≥ a pedida, no máximo 3, ordenadas por capacidade e depois por `id`.

O agente não conhece nenhuma dessas regras.

## Saída do validador

Execução com os dois processos recém-iniciados a partir de um clone limpo:

```
trace-id desta execucao: 9ff44612045cd216b0f3250d8747a21a
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
