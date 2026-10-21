"""Regras de sala: dados do starter, politica de uso e reservas em memoria.

Toda decisao de dominio (validacao, conflito, alternativas) vive aqui, no servidor
MCP. O agente nunca reimplementa nada disto.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path


def _dados_padrao() -> Path:
    """`dados/` do repositorio (instalacao editavel) ou, na falta dele, do diretorio atual."""
    do_repositorio = Path(__file__).resolve().parents[2] / "dados"
    return do_repositorio if do_repositorio.is_dir() else Path.cwd() / "dados"


DADOS = Path(os.environ.get("DADOS_DIR") or _dados_padrao())

FUSO_SAO_PAULO = timezone(timedelta(hours=-3))
ABERTURA = time(8, 0)
FECHAMENTO = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAX_ALTERNATIVAS = 3

ERRO_SALA = "Sala inexistente: {sala}"
ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"
ERRO_DATA = "Data invalida: use ISO 8601 com fuso, por exemplo 2026-11-03T14:00:00-03:00"


class ErroDeRegra(Exception):
    """Violacao de regra de negocio; vira erro de execucao (isError) na tool."""


@dataclass(frozen=True)
class Pedido:
    sala: str
    inicio: datetime
    fim: datetime


def _ler_json(nome: str) -> list[dict]:
    return json.loads((DADOS / nome).read_text(encoding="utf-8"))


SALAS: list[dict] = _ler_json("salas.json")
SALAS_POR_ID: dict[str, dict] = {s["id"]: s for s in SALAS}
POLITICA_TEXTO: str = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
POLITICA_VERSAO: str = POLITICA_TEXTO.splitlines()[0].split(":", 1)[1].strip()

_reservas: list[dict] = _ler_json("reservas.json")
_trava = threading.Lock()


def _data(valor: str) -> datetime:
    # `Z` so e aceito por fromisoformat a partir do Python 3.11; normaliza para valer em 3.10.
    if valor.endswith(("Z", "z")):
        valor = valor[:-1] + "+00:00"
    try:
        instante = datetime.fromisoformat(valor)
    except ValueError:
        raise ErroDeRegra(ERRO_DATA) from None
    if instante.tzinfo is None:
        raise ErroDeRegra(ERRO_DATA)
    return instante


def validar(sala: str, inicio: str, fim: str) -> Pedido:
    """Aplica, nesta ordem, as regras de sala, intervalo, janela e duracao."""
    if sala not in SALAS_POR_ID:
        raise ErroDeRegra(ERRO_SALA.format(sala=sala))
    ini, fi = _data(inicio), _data(fim)
    if fi <= ini:
        raise ErroDeRegra(ERRO_INTERVALO)
    ini_local, fim_local = ini.astimezone(FUSO_SAO_PAULO), fi.astimezone(FUSO_SAO_PAULO)
    dentro = (
        ini_local.date() == fim_local.date()
        and ABERTURA <= ini_local.time() <= FECHAMENTO
        and ABERTURA <= fim_local.time() <= FECHAMENTO
    )
    if not dentro:
        raise ErroDeRegra(ERRO_JANELA)
    if fi - ini > DURACAO_MAXIMA:
        raise ErroDeRegra(ERRO_DURACAO)
    return Pedido(sala, ini, fi)


def conflitos(sala: str, inicio: datetime, fim: datetime) -> list[dict]:
    """Reservas da sala que se sobrepoem ao intervalo (encostar nao e sobrepor)."""
    return [
        r
        for r in list(_reservas)
        if r["sala"] == sala and _data(r["inicio"]) < fim and inicio < _data(r["fim"])
    ]


def alternativas(pedido: Pedido) -> list[str]:
    """Salas livres no intervalo com capacidade >= a pedida, por capacidade e depois id."""
    minima = SALAS_POR_ID[pedido.sala]["capacidade"]
    candidatas = [
        s
        for s in SALAS
        if s["id"] != pedido.sala
        and s["capacidade"] >= minima
        and not conflitos(s["id"], pedido.inicio, pedido.fim)
    ]
    candidatas.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in candidatas[:MAX_ALTERNATIVAS]]


def reservar(sala: str, inicio: str, fim: str, responsavel: str) -> dict:
    """Cria a reserva. Revalida o conflito sob a trava para nao gravar sobreposicao."""
    pedido = validar(sala, inicio, fim)
    with _trava:
        if conflitos(sala, pedido.inicio, pedido.fim):
            raise ErroDeRegra(f"Conflito de horario: a sala {sala} ja esta reservada no intervalo")
        reserva = {
            "id": f"res-{len(_reservas) + 1:04d}",
            "sala": sala,
            "inicio": inicio,
            "fim": fim,
            "responsavel": responsavel,
        }
        _reservas.append(reserva)
    return reserva
