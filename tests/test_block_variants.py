"""Bloqueio do assistente tem que valer nas DUAS formas do celular.

O SAI Comercial guarda a conversa com o numero canonicalizado (COM o 9o digito)
e e essa forma que chega no POST /sai/block. O bot indexa Redis e SQLite pelo
JID da UAZAPI, que em muitos numeros vem SEM o 9. Enquanto as duas pontas nao
se falavam, o "Desativar assistente" gravava bloqueio num telefone e os jobs
proativos (reativacao, lembrete) consultavam o outro — e mandavam follow-up por
cima do assistente desativado. Caso real: lead 5561983644341 / 556183644341 no
tenant corretor walisson.
"""
import pytest


COM_NOVE = "5561983644341"
SEM_NOVE = "556183644341"


class FakeRedis:
    """Fake com nocao de TTL (-1 = sem expiracao, -2 = inexistente)."""

    def __init__(self):
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key: str):
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False):
        if nx and key in self.values:
            return None
        self.values[key] = value
        self.ttls[key] = ex if ex is not None else -1
        return True

    async def exists(self, key: str) -> int:
        return 1 if key in self.values else 0

    async def ttl(self, key: str) -> int:
        if key not in self.values:
            return -2
        return self.ttls.get(key, -1)

    async def delete(self, *keys: str) -> int:
        count = 0
        for key in keys:
            if key in self.values:
                del self.values[key]
                self.ttls.pop(key, None)
                count += 1
        return count

    async def scan_iter(self, match: str = "*", count: int = 100):
        import fnmatch
        for key in list(self.values.keys()):
            if fnmatch.fnmatch(key, match):
                yield key


@pytest.fixture
def fake(monkeypatch):
    from app.services import redis_service as rds

    f = FakeRedis()

    async def fake_get_redis():
        return f

    monkeypatch.setattr(rds, "get_redis", fake_get_redis)
    return f


def test_block_variants_cobre_as_duas_formas():
    from app.services.phone_utils import block_variants

    assert set(block_variants(COM_NOVE)) == {COM_NOVE, SEM_NOVE}
    assert set(block_variants(SEM_NOVE)) == {COM_NOVE, SEM_NOVE}
    # aceita lixo de formatacao vindo do SAI / do JID
    assert set(block_variants("+55 (61) 98364-4341")) == {COM_NOVE, SEM_NOVE}
    assert block_variants("") == []


@pytest.mark.asyncio
async def test_bloqueio_do_sai_vale_para_a_forma_sem_nove(fake):
    """O bug do walisson: SAI bloqueia com 9, follow-up consulta sem 9."""
    from app.services import redis_service as rds

    await rds.set_permanent_block(COM_NOVE, reason="manual")

    assert await rds.is_blocked(SEM_NOVE) is True
    assert await rds.is_permanently_blocked(SEM_NOVE) is True


@pytest.mark.asyncio
async def test_desbloqueio_limpa_as_duas_formas(fake):
    from app.services import redis_service as rds

    await rds.set_permanent_block(SEM_NOVE)
    assert await rds.clear_block(COM_NOVE) is True

    assert await rds.is_blocked(COM_NOVE) is False
    assert await rds.is_blocked(SEM_NOVE) is False


@pytest.mark.asyncio
async def test_eco_da_atendente_nao_rebaixa_bloqueio_permanente_da_outra_forma(fake):
    """set_block (humano assumiu, com prazo) nao pode derrubar o bloqueio do
    botao — nem quando o eco chega na variante oposta do numero."""
    from app.services import redis_service as rds

    await rds.set_permanent_block(COM_NOVE, reason="manual")
    await rds.set_block(SEM_NOVE)  # eco fromMe chega pelo JID sem o 9

    assert await rds.is_permanently_blocked(COM_NOVE) is True
    assert await rds.is_permanently_blocked(SEM_NOVE) is True


@pytest.mark.asyncio
async def test_bloqueio_automatico_continua_com_prazo(fake):
    from app.services import redis_service as rds

    await rds.set_block(SEM_NOVE)

    assert await rds.is_blocked(COM_NOVE) is True
    assert await rds.is_permanently_blocked(COM_NOVE) is False


@pytest.mark.asyncio
async def test_reconciliacao_aplica_pausa_que_o_bot_perdeu(fake):
    """Redis limpo num redeploy / bot fora do ar no clique: o snapshot do SAI
    reintroduz o bloqueio sem ninguem clicar de novo."""
    from app.services import redis_service as rds

    applied, removed = await rds.apply_paused_phones([COM_NOVE])

    assert (applied, removed) == (1, 0)
    assert await rds.is_permanently_blocked(SEM_NOVE) is True


@pytest.mark.asyncio
async def test_reconciliacao_derruba_pausa_que_o_sai_nao_lista_mais(fake):
    from app.services import redis_service as rds

    await rds.set_permanent_block(COM_NOVE)
    applied, removed = await rds.apply_paused_phones([])

    assert applied == 0 and removed >= 1
    assert await rds.is_blocked(COM_NOVE) is False


@pytest.mark.asyncio
async def test_reconciliacao_nao_toca_bloqueio_de_humano_assumiu(fake):
    """Pausa automatica tem prazo proprio e nao aparece no snapshot — a
    reconciliacao nao pode derruba-la."""
    from app.services import redis_service as rds

    await rds.set_block(SEM_NOVE)  # humano assumiu
    applied, removed = await rds.apply_paused_phones([])

    assert (applied, removed) == (0, 0)
    assert await rds.is_blocked(SEM_NOVE) is True


@pytest.mark.asyncio
async def test_reconciliacao_idempotente(fake):
    from app.services import redis_service as rds

    await rds.apply_paused_phones([COM_NOVE])
    applied, removed = await rds.apply_paused_phones([COM_NOVE])

    assert (applied, removed) == (0, 0)
    assert await rds.is_permanently_blocked(SEM_NOVE) is True


@pytest.mark.asyncio
async def test_rota_block_corta_redis_e_sqlite_nas_duas_formas(fake, monkeypatch):
    """O clique em "Desativar assistente" tem que bloquear o Redis e tirar o
    lead da fila de follow-up — nas duas formas do numero."""
    import os
    import tempfile

    from app import db as db_mod
    from app import sai_router
    from app.config import settings

    monkeypatch.setattr(settings, "SAI_INGEST_SECRET", "segredo")
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(settings, "SQLITE_PATH", os.path.join(tmp, "t.db"))
        db_mod.init_db_sync()

        # follow-up agendado sob a forma SEM o 9 (como o JID da UAZAPI entrega)
        await db_mod.schedule_followup(SEM_NOVE, next_follow_up_iso="2020-01-01T00:00:00+00:00", stage=2)

        # o SAI manda a forma COM o 9
        res = await sai_router.block_phone(
            sai_router.BlockBody(phone=COM_NOVE, blocked=True, manual=True),
            x_ingest_secret="segredo",
        )

        assert res["ok"] is True
        assert set(res["variants"]) == {COM_NOVE, SEM_NOVE}
        assert await rds_is_permanently_blocked(SEM_NOVE) is True
        lead = await db_mod.get_lead(SEM_NOVE)
        assert lead["modo_mudo"] == 1 and lead["next_follow_up"] is None

        # e o religar pelo botao desfaz os dois lados
        await sai_router.block_phone(
            sai_router.BlockBody(phone=COM_NOVE, blocked=False, manual=True),
            x_ingest_secret="segredo",
        )
        assert await rds_is_blocked(SEM_NOVE) is False
        assert (await db_mod.get_lead(SEM_NOVE))["modo_mudo"] == 0


@pytest.mark.asyncio
async def test_rota_block_com_resumeAt_nao_vira_permanente(fake, monkeypatch):
    """Pausa de "humano assumiu" chega com prazo — nao pode ser gravada como
    bloqueio definitivo, senao a reconciliacao com o SAI a derrubaria por nao
    constar em pausedPhones."""
    import os
    import tempfile
    from datetime import datetime, timedelta, timezone

    from app import db as db_mod
    from app import sai_router
    from app.config import settings

    monkeypatch.setattr(settings, "SAI_INGEST_SECRET", "segredo")
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(settings, "SQLITE_PATH", os.path.join(tmp, "t.db"))
        db_mod.init_db_sync()

        resume = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
        await sai_router.block_phone(
            sai_router.BlockBody(phone=COM_NOVE, blocked=True, resumeAt=resume),
            x_ingest_secret="segredo",
        )

        assert await rds_is_blocked(SEM_NOVE) is True
        assert await rds_is_permanently_blocked(SEM_NOVE) is False

        # o fim automatico do prazo NAO devolve o lead ao follow-up
        await db_mod.upsert_lead(SEM_NOVE, modo_mudo=1)
        await sai_router.block_phone(
            sai_router.BlockBody(phone=COM_NOVE, blocked=False),
            x_ingest_secret="segredo",
        )
        assert (await db_mod.get_lead(SEM_NOVE))["modo_mudo"] == 1


@pytest.mark.asyncio
async def test_rota_block_resumeAt_nao_rebaixa_desativacao(fake, monkeypatch):
    """Atendente responde depois do "Desativar assistente": o prazo do takeover
    nao pode substituir o bloqueio definitivo."""
    import os
    import tempfile
    from datetime import datetime, timedelta, timezone

    from app import db as db_mod
    from app import sai_router
    from app.config import settings

    monkeypatch.setattr(settings, "SAI_INGEST_SECRET", "segredo")
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(settings, "SQLITE_PATH", os.path.join(tmp, "t.db"))
        db_mod.init_db_sync()

        await sai_router.block_phone(
            sai_router.BlockBody(phone=COM_NOVE, blocked=True, manual=True),
            x_ingest_secret="segredo",
        )
        resume = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
        await sai_router.block_phone(
            sai_router.BlockBody(phone=SEM_NOVE, blocked=True, resumeAt=resume),
            x_ingest_secret="segredo",
        )

        assert await rds_is_permanently_blocked(COM_NOVE) is True


async def rds_is_blocked(phone: str) -> bool:
    from app.services import redis_service as rds
    return await rds.is_blocked(phone)


async def rds_is_permanently_blocked(phone: str) -> bool:
    from app.services import redis_service as rds
    return await rds.is_permanently_blocked(phone)


@pytest.mark.asyncio
async def test_reativacao_pula_lead_com_assistente_desativado(fake, monkeypatch, tmp_path):
    """Teste de ponta: o job de reativacao NAO envia quando o SAI desativou o
    assistente pela outra forma do numero (regressao do walisson)."""
    from app.followups import reactivation
    from app.services import redis_service as rds

    await rds.set_permanent_block(COM_NOVE, reason="manual")

    enviados: list[str] = []

    monkeypatch.setattr(
        reactivation, "_cfg",
        lambda: {"enabled": True, "inactive_hours": 24, "max_stages": 3, "max_per_run": 20},
    )

    async def fake_seed(*_a, **_k):
        return None

    async def fake_due(_now_iso):
        return [{"phone": SEM_NOVE, "nome": "Igor", "stage_follow_up": 1}]

    async def fake_send(phone, msg, **_k):
        enviados.append(phone)

    async def boom(*_a, **_k):
        raise AssertionError("nao pode gerar mensagem para lead bloqueado")

    monkeypatch.setattr(reactivation, "_seed_inactive_leads", fake_seed)
    monkeypatch.setattr(reactivation.db, "get_followups_due", fake_due)
    monkeypatch.setattr(reactivation.uazapi, "send_text", fake_send)
    monkeypatch.setattr(reactivation, "generate_reactivation_message", boom)

    await reactivation.run()

    assert enviados == []
