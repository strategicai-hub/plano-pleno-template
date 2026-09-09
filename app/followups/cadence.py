"""
Cadencia dos follow-ups: quando cada estagio deve sair.

Duas regras vivem aqui, usadas pelo disparo de 1o contato, pela ponte do SAI e
pela reativacao — as tres precisam concordar sobre o mesmo relogio:

1. ANCORA — a regua conta a partir do ENVIO DA 1a MENSAGEM, nao do envio
   anterior. Com `offsets_hours: [12, 24]`, o estagio 1 sai 12h depois da
   abertura e o estagio 2 sai 24h depois DELA, nao 24h depois do estagio 1.
   Sem ancora, todo atraso de um estagio empurrava o seguinte junto e a regua
   ia escorregando dia apos dia.

2. JANELA — nenhum follow-up sai de madrugada. Um horario fora da janela e
   empurrado para a proxima abertura dela, com alguns minutos de sorteio
   (`spread_minutes`) para que os leads represados durante a noite nao saiam
   todos no mesmo minuto das 08:00 — isso e rajada, e rajada bloqueia o numero
   no WhatsApp. Exemplo real: abertura as 15h -> estagio 1 cairia as 03h ->
   sai entre 08:00 e 09:00; estagio 2 continua marcado para as 15h, porque
   conta da ancora.
"""
import random
from datetime import datetime, time as dtime, timedelta

from app.client_data import load_client_data

# Janela padrao quando o cliente nao configura `send_window`.
DEFAULT_WINDOW = {"hours_start": "08:00", "hours_end": "18:00", "spread_minutes": 60}


def reactivation_cfg() -> dict:
    data = load_client_data() or {}
    return (data.get("followups") or {}).get("reactivation") or {}


def track_cfg(cfg: dict, track: str) -> dict:
    """Config de uma trilha da reativacao, com fallback para o formato antigo.

    Cliente que ainda nao separou as trilhas tem `inactive_hours`/`max_stages`
    direto no bloco `reactivation` — esses valores continuam valendo para as
    duas trilhas, e o comportamento fica igual ao de antes.
    """
    sub = cfg.get(track)
    if isinstance(sub, dict) and sub:
        return sub
    return {
        "inactive_hours": cfg.get("inactive_hours", 24),
        "max_stages": cfg.get("max_stages", 3),
        "interval_hours": cfg.get("interval_hours", 24),
    }


def _parse_hhmm(raw, fallback: dtime) -> dtime:
    try:
        return datetime.strptime(str(raw), "%H:%M").time()
    except (ValueError, TypeError):
        return fallback


def window_bounds(cfg: dict) -> tuple[dtime, dtime, int]:
    """(inicio, fim, spread_minutes) da janela de envio de um bloco de config.

    Aceita tanto `send_window: {hours_start, hours_end}` quanto as chaves
    `hours_start`/`hours_end` no proprio bloco (formato usado pelo disparo e
    pelo encaminhamento por inatividade).
    """
    janela = {**DEFAULT_WINDOW, **(cfg.get("send_window") or {})}
    for chave in ("hours_start", "hours_end", "spread_minutes"):
        if cfg.get(chave) is not None and not (cfg.get("send_window") or {}).get(chave):
            janela[chave] = cfg[chave]
    inicio = _parse_hhmm(janela.get("hours_start"), dtime(8, 0))
    fim = _parse_hhmm(janela.get("hours_end"), dtime(18, 0))
    try:
        spread = max(int(janela.get("spread_minutes") or 0), 0)
    except (TypeError, ValueError):
        spread = 60
    return inicio, fim, spread


def dentro_da_janela(momento: datetime, cfg: dict) -> bool:
    inicio, fim, _ = window_bounds(cfg)
    return inicio <= momento.time() <= fim


def ajustar_para_janela(momento: datetime, cfg: dict) -> datetime:
    """Devolve `momento` se ele cai na janela; senao, a proxima abertura dela.

    Antes da abertura -> mesma data, no horario de abertura. Depois do
    fechamento -> abertura do dia seguinte. Nos dois casos soma um sorteio de
    ate `spread_minutes` para espalhar a fila represada.
    """
    inicio, fim, spread = window_bounds(cfg)
    hora = momento.time()
    if inicio <= hora <= fim:
        return momento
    base = momento if hora < inicio else momento + timedelta(days=1)
    abertura = base.replace(
        hour=inicio.hour, minute=inicio.minute, second=0, microsecond=0
    )
    return abertura + timedelta(
        minutes=random.randint(0, spread), seconds=random.randint(0, 59)
    )


def offset_horas(track: dict, stage: int) -> float:
    """Horas entre a 1a mensagem e o follow-up de `stage`.

    `offsets_hours: [12, 24]` -> estagio 1 em +12h, estagio 2 em +24h. Sem a
    lista, cai no comportamento antigo (`interval_hours` multiplicado pelo
    estagio), que e o de todo cliente que ainda nao migrou.
    """
    offsets = track.get("offsets_hours")
    if isinstance(offsets, (list, tuple)) and offsets:
        idx = min(max(int(stage), 1), len(offsets)) - 1
        try:
            return float(offsets[idx])
        except (TypeError, ValueError):
            pass
    return float(track.get("interval_hours", 24) or 24) * max(int(stage), 1)


def proximo_envio(
    ancora: datetime,
    stage: int,
    track: dict,
    cfg: dict,
    piso: datetime | None = None,
) -> datetime:
    """Quando o follow-up de `stage` deve sair, ja dentro da janela.

    `piso` e o horario minimo aceitavel (tipicamente `ultimo envio + gap`).
    Ele existe porque a janela comprime a regua: abertura as 09h -> estagio 1
    cairia as 21h e vai para as 08h do dia seguinte; o estagio 2, ancorado em
    +24h, cairia as 09h — 1h depois do estagio 1. O piso afasta os dois.
    """
    alvo = ancora + timedelta(hours=offset_horas(track, stage))
    if piso is not None and alvo < piso:
        alvo = piso
    return ajustar_para_janela(alvo, cfg)


def piso_por_gap(referencia: datetime, track: dict) -> datetime | None:
    """`referencia + min_gap_hours` — intervalo minimo entre dois follow-ups."""
    try:
        gap = float(track.get("min_gap_hours") or 0)
    except (TypeError, ValueError):
        return None
    if gap <= 0:
        return None
    return referencia + timedelta(hours=gap)
