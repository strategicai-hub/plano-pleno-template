"""
Reativacao de leads inativos — duas trilhas.

Trigger: lead com `next_follow_up <= now` em SQLite, nao finalizado e nao em
modo_mudo. Envia a mensagem da trilha, avanca o estagio e reagenda. No ultimo
estagio da trilha, finaliza.

TRILHAS (a escolha e por lead, derivada de `last_customer_message_at`):

- `no_reply`  — o lead NUNCA respondeu. O relogio comeca no ENVIO do 1o contato
  (quem agenda o estagio 1 e o `lead_dispatch`) e TODOS os estagios contam dessa
  mesma ancora (`leads.followup_anchor_at`), nao do estagio anterior — ver
  `followups/cadence.py`.
- `stalled`   — o lead respondeu e parou. O relogio comeca no ultimo retorno
  dele. `_seed_inactive_leads` captura esses leads apos `inactive_hours`.

A transicao entre trilhas nao precisa de estado proprio: quando o lead responde,
`db.touch_last_message()` zera estagio e agendamento e preenche
`last_customer_message_at` — ele sai sozinho da trilha `no_reply` para a
`stalled`.

Cliente que nao declarar as trilhas em `followups.reactivation` continua no
comportamento antigo (trilha unica): `_track_cfg` cai nos valores chapados. O
mesmo vale para a cadencia: sem `offsets_hours`, a regua segue com
`interval_hours` como sempre foi.

JANELA: nenhum follow-up sai fora de `reactivation.send_window` (08:00-18:00 por
padrao). O que cairia de madrugada e reagendado para a abertura seguinte, com
sorteio de minutos (`spread_minutes`) para nao sair tudo as 08:00 em rajada.

TEXTO: quando ha template do cliente (`followups.templates.<trilha>_stage_N`),
ele e a fonte da mensagem e a IA so o reescreve com outras palavras
(`gemini.vary_message`) — texto identico em massa e o principal gatilho de
bloqueio do WhatsApp. Sem template, cai na geracao livre a partir do historico
(`generate_reactivation_message`).
"""
import logging
from datetime import datetime, timedelta, timezone

from zoneinfo import ZoneInfo

from app import db
from app.client_data import load_client_data
from app.config import settings
from app.followups import cadence, templates
from app.services import lead_intake, nomes, redis_service as rds, sai_sync, uazapi
from app.services.gemini import generate_reactivation_message, vary_message

logger = logging.getLogger("followup.reactivation")

# Idade maxima aceitavel da lista de pausados do SAI para que o follow-up possa
# disparar. O painel reconcilia a cada 15 min (POLL_INTERVAL_SECONDS): 45 min
# tolera duas falhas seguidas antes de calar a cobranca.
RECONCILE_MAX_AGE_SECONDS = 45 * 60

TRACK_NO_REPLY = "no_reply"
TRACK_STALLED = "stalled"

# Status que tiram o lead da regua de reativacao. 'encaminhado' entrou aqui
# porque a equipe ja assumiu a conversa: cobrar resposta depois do handoff e
# cobrar a pessoa errada — quem deve dar o proximo passo e a equipe.
STATUS_FORA_DA_REGUA = ("finalizado", "agendado", "encaminhado")


async def _variante_em_conflito(phone: str) -> bool:
    """True se alguma forma do numero (com/sem o 9o digito) ja saiu da regua.

    O mesmo celular tem duas linhas no banco e `touch_last_message` preenche
    `last_customer_message_at` nas duas — sem esta checagem as duas eram
    semeadas no mesmo ciclo e o lead recebia a MESMA cobranca duas vezes, com
    minutos de diferenca e o texto so reescrito pela IA. Basta uma variante
    estar agendada, muda ou fora da regua para o grupo inteiro ficar de fora.
    """
    for variant in lead_intake.phone_variants(phone):
        outro = await db.get_lead(variant)
        if not outro:
            continue
        if outro.get("next_follow_up"):
            return True
        if int(outro.get("modo_mudo") or 0) == 1:
            return True
        if (outro.get("status_conversa") or "") in STATUS_FORA_DA_REGUA:
            return True
    return False


def _cfg() -> dict:
    data = load_client_data() or {}
    return (data.get("followups") or {}).get("reactivation") or {}


def _track_cfg(cfg: dict, track: str) -> dict:
    """Config da trilha, com fallback para o formato antigo (trilha unica)."""
    return cadence.track_cfg(cfg, track)


def _ancora(lead: dict, stage: int, track_cfg: dict, now_tz: datetime) -> datetime:
    """Momento do 1o contato deste lead, base de toda a regua.

    Lead sem ancora gravada (disparado por uma versao anterior a esta, ou
    semeado pela trilha `stalled`, que nasce da ultima fala do lead) nao tem como
    saber quando a abertura saiu — assume-se que o estagio atual esta vencendo
    agora, o que reconstroi a regua a partir daqui sem atropelar o lead.
    """
    raw = (lead.get("followup_anchor_at") or "").strip()
    if raw:
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(ZoneInfo(settings.SCHEDULER_TZ))
        except ValueError:
            logger.warning("ancora invalida em %s: %r", lead.get("phone"), raw)
    return now_tz - timedelta(hours=cadence.offset_horas(track_cfg, stage))


def _track_for(lead: dict) -> str:
    """Trilha do lead: respondeu alguma vez -> `stalled`; nunca -> `no_reply`."""
    return TRACK_STALLED if (lead.get("last_customer_message_at") or "") else TRACK_NO_REPLY


def _now_tz() -> datetime:
    return datetime.now(ZoneInfo(settings.SCHEDULER_TZ))


async def _seed_inactive_leads(now_tz: datetime, inactive_hours: int) -> None:
    """
    Marca next_follow_up = now para leads com last_customer_message_at antigo
    (sem next_follow_up agendado) — assim o loop abaixo os captura.

    So alimenta a trilha `stalled`: o filtro exige `last_customer_message_at`
    preenchido. Quem nunca respondeu entra na trilha `no_reply` pelo agendamento
    feito no disparo do 1o contato.

    Dedup por variante: o numero tem linha com e sem o 9o digito e
    `touch_last_message` preenche `last_customer_message_at` nas duas. Sem dedup
    as duas ficavam devidas e o lead levava a MESMA cobranca em ciclos seguidos,
    com texto so reescrito pela IA — o que soa exatamente como robo. Dentro do
    grupo vence a linha que falou por ultimo: e a que tem o historico da
    conversa, que e o insumo do texto do follow-up.
    """
    now_utc = now_tz.astimezone(timezone.utc)
    cutoff = (now_utc - timedelta(hours=inactive_hours)).isoformat()
    now_iso = now_utc.isoformat()

    import aiosqlite
    async with aiosqlite.connect(settings.SQLITE_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            f"""
            SELECT l.phone, l.last_customer_message_at FROM leads l
            WHERE l.next_follow_up IS NULL
              AND l.last_customer_message_at IS NOT NULL
              AND l.last_customer_message_at <= ?
              AND COALESCE(l.status_conversa, '') NOT IN
                  ('finalizado', 'agendado', 'encaminhado')
              AND COALESCE(l.modo_mudo, 0) = 0
              AND COALESCE(l.stage_follow_up, 0) = 0
              {db.FOLLOWUP_HOLD_CLAUSE}
              AND NOT EXISTS (
                  SELECT 1 FROM appointments a
                  WHERE a.phone = l.phone
                    AND a.scheduled_at >= ?
                    AND a.status IN ('booked', 'reminded')
              )
            """,
            (cutoff, now_iso),
        )
        rows = await cur.fetchall()

    if not rows:
        return

    # Uma entrada por numero real: a variante que respondeu mais recentemente
    # (empate resolvido pela forma mais longa, a canonica do disparo).
    escolhidos: dict[str, tuple[str, str]] = {}
    for row in rows:
        phone = row["phone"]
        visto = row["last_customer_message_at"] or ""
        chave = min(lead_intake.phone_variants(phone))
        atual = escolhidos.get(chave)
        if atual is None or (visto, len(phone)) > (atual[1], len(atual[0])):
            escolhidos[chave] = (phone, visto)

    semeados = 0
    for phone, _ in escolhidos.values():
        if await _variante_em_conflito(phone):
            continue
        await db.schedule_followup(phone, next_follow_up_iso=now_iso, stage=1)
        semeados += 1
    if semeados:
        logger.info("reactivation: %d lead(s) sementeados para reativacao", semeados)


async def _sai_state_is_fresh() -> bool:
    """A lista de conversas em atendimento humano esta atualizada?

    O SAI e a fonte da verdade sobre quem tem gente de verdade na conversa, e
    quem a reconcilia e a API (polling de 15 min) — este job roda em OUTRO
    processo (scheduler) e enxerga o resultado pelo Redis/SQLite compartilhados.
    Se a API esta fora, se o SAI esta fora ou se o Redis foi limpo, o scheduler
    seguiria disparando com uma foto velha de quem esta pausado e cobraria lead
    em pleno atendimento — foi essa a janela que sobrou depois de o bloqueio e o
    `modo_mudo` cobrirem o caminho normal.

    Sem vinculo com o SAI o gate nao se aplica (bot autonomo).
    """
    if not await sai_sync.sai_integration_active():
        return True
    idade = await sai_sync.seconds_since_reconcile()
    if idade is None:
        logger.warning(
            "reactivation: lista de pausados do SAI ainda nao conferida neste Redis — "
            "nenhum follow-up sai ate a primeira reconciliacao"
        )
        return False
    if idade > RECONCILE_MAX_AGE_SECONDS:
        logger.warning(
            "reactivation: lista de pausados do SAI conferida ha %.0f min (limite %.0f) — "
            "follow-up suspenso ate a sincronizacao voltar",
            idade / 60, RECONCILE_MAX_AGE_SECONDS / 60,
        )
        return False
    return True


async def _build_message(
    phone: str,
    nome: str,
    track: str,
    stage: int,
    now_tz: datetime,
) -> str:
    """Mensagem do estagio: template do cliente (variado pela IA) ou geracao livre."""
    base = templates.get_override_first(
        [f"{track}_stage_{stage}", f"reactivation_stage_{stage}"],
        nome=nome,
        saudacao=templates.saudacao(now_tz),
    )
    if base:
        variacao = await vary_message(phone, base, nome=nome, kind="REACTIVATION")
        return variacao or base

    now_str = now_tz.strftime("%A, %d/%m/%Y %H:%M")
    return await generate_reactivation_message(phone, nome, stage, now_str)


async def run() -> None:
    cfg = _cfg()
    if not cfg.get("enabled", False):
        return

    if not await _sai_state_is_fresh():
        return

    # Teto de envios por execucao. Ao ligar a reativacao num bot com base
    # acumulada, TODOS os leads inativos ficam devidos no mesmo ciclo — sem
    # teto isso vira centenas de mensagens em minutos (risco de ban no
    # WhatsApp). Com teto, o backlog escoa aos poucos. 0 = sem limite.
    max_per_run = int(cfg.get("max_per_run", 20))

    now_tz = _now_tz()
    now_utc_iso = now_tz.astimezone(timezone.utc).isoformat()

    stalled_cfg = _track_cfg(cfg, TRACK_STALLED)
    await _seed_inactive_leads(now_tz, int(stalled_cfg.get("inactive_hours", 24)))

    due = await db.get_followups_due(now_utc_iso)
    if not due:
        return

    total_due = len(due)
    if max_per_run > 0 and total_due > max_per_run:
        due = due[:max_per_run]
        logger.info(
            "reactivation: %d lead(s) devido(s), processando %d neste ciclo (teto max_per_run)",
            total_due, max_per_run,
        )
    else:
        logger.info("reactivation: %d lead(s) devido(s)", total_due)

    for lead in due:
        phone = lead["phone"]
        # Nunca ler `lead["nome"]` cru: a reativacao ja saiu chamando lead pelo
        # nome do perfil do WhatsApp ("Oi Tutu"). nomes.nome_do_lead aplica a
        # precedencia confirmado > cadastro e devolve "" quando nao ha nome
        # confiavel — nesse caso a mensagem sai sem vocativo.
        nome = nomes.nome_do_lead(lead)
        stage = int(lead.get("stage_follow_up") or 1)

        track = _track_for(lead)
        track_cfg = _track_cfg(cfg, track)
        max_stages = int(track_cfg.get("max_stages", 3))

        if stage > max_stages:
            await db.mark_finalizado(phone)
            continue

        # Bloqueio ativo: ou o atendente humano assumiu (expira amanha 08:00 SP),
        # ou o operador clicou "Desativar assistente" no SAI (permanente, so sai
        # com "Ativar assistente"). Em nenhum dos dois o bot pode enviar nada —
        # sai sem avancar o estagio e retoma quando o bloqueio cair.
        # `is_blocked` varre as duas formas do numero (com/sem o 9o digito): o
        # SAI bloqueia pelo telefone canonico e aqui o phone vem do SQLite, que
        # o indexa pelo JID da UAZAPI. Casar so a string exata ja deixou
        # follow-up sair depois do "Desativar assistente".
        if await rds.is_blocked(phone):
            motivo = "assistente desativado no SAI" if await rds.is_permanently_blocked(phone) \
                else "humano assumiu"
            logger.info("[%s] bloqueado (%s) — reativacao adiada", phone, motivo)
            continue

        # Janela de envio: follow-up nao sai de madrugada. Fora dela o lead e
        # empurrado para a proxima abertura (com sorteio de minutos) em vez de
        # receber a mensagem agora. Pega tambem os leads que ficaram agendados
        # pela regra antiga, de horas corridas a partir do disparo.
        if not cadence.dentro_da_janela(now_tz, cfg):
            quando = cadence.ajustar_para_janela(now_tz, cfg)
            await db.schedule_followup(
                phone,
                next_follow_up_iso=quando.astimezone(timezone.utc).isoformat(),
                stage=stage,
            )
            logger.info("[%s] fora da janela de envio — follow-up adiado para %s",
                        phone, quando.strftime("%d/%m %H:%M"))
            continue

        # Trava distribuida: impede que duas execucoes concorrentes do
        # scheduler (rolling update / overlap) gerem dois envios pro mesmo
        # lead. TTL=3600s cobre o pior cenario de Gemini lento + falha.
        if not await rds.acquire_followup_lock(phone, ttl=3600):
            logger.info("[%s] follow-up ja em andamento, pulando", phone)
            continue

        try:
            msg = await _build_message(phone, nome, track, stage, now_tz)
        except Exception:
            logger.exception("[%s] falha ao gerar reativacao (trilha=%s)", phone, track)
            # Adia 1h para nao reentrar a cada 15min enquanto Gemini esta fora.
            retry_iso = (now_tz + timedelta(hours=1)).astimezone(timezone.utc).isoformat()
            await db.schedule_followup(phone, next_follow_up_iso=retry_iso, stage=stage)
            await rds.release_followup_lock(phone)
            continue

        if not msg:
            logger.info("[%s] mensagem vazia (trilha=%s stage=%d), pulando", phone, track, stage)
            retry_iso = (now_tz + timedelta(hours=1)).astimezone(timezone.utc).isoformat()
            await db.schedule_followup(phone, next_follow_up_iso=retry_iso, stage=stage)
            await rds.release_followup_lock(phone)
            continue

        if settings.FOLLOWUP_DRY_RUN:
            logger.info("[DRY_RUN][%s] trilha=%s stage=%d -> %s", phone, track, stage, msg)
        else:
            try:
                await uazapi.send_paragraphs(phone, msg)
            except Exception:
                logger.exception("[%s] falha ao enviar reativacao", phone)
                # Adia 1h para nao tentar reenviar a cada 15min (ex.: UAZAPI fora
                # ou token stale ficava em loop infinito).
                retry_iso = (now_tz + timedelta(hours=1)).astimezone(timezone.utc).isoformat()
                await db.schedule_followup(phone, next_follow_up_iso=retry_iso, stage=stage)
                await rds.release_followup_lock(phone)
                continue

        finalize = stage >= max_stages
        new_stage = stage + 1 if not finalize else max_stages
        next_iso = None
        if not finalize:
            # Proximo estagio ancorado no 1o contato (+12h, +24h... conforme
            # `offsets_hours`), dentro da janela e nunca colado no envio de agora
            # (`min_gap_hours`). Sem `offsets_hours` a conta cai em
            # `interval_hours * estagio`, que e o comportamento de sempre.
            ancora = _ancora(lead, stage, track_cfg, now_tz)
            proximo = cadence.proximo_envio(
                ancora, stage + 1, track_cfg, cfg,
                piso=cadence.piso_por_gap(now_tz, track_cfg),
            )
            next_iso = proximo.astimezone(timezone.utc).isoformat()

        # Avanca tambem as variantes do numero: se as duas formas (com/sem o 9o
        # digito) chegaram a ficar agendadas, a irma sai da fila junto e nao
        # repete a cobranca no ciclo seguinte.
        await db.advance_followup_stage_variants(
            lead_intake.phone_variants(phone), phone, new_stage, next_iso, finalize,
        )
        await rds.release_followup_lock(phone)
        # Texto no log de proposito: a mensagem e gerada na hora e nao fica
        # gravada em lugar nenhum — sem isso nao da para auditar o que o bot
        # mandou em cada follow-up.
        logger.info(
            "[%s] ENVIADO trilha=%s stage=%d nome=%r texto=%r (proximo=%s finalize=%s)",
            phone, track, stage, nome, msg, next_iso or "-", finalize,
        )
