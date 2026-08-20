"""Инфраструктура pytest: тест-режим, схема, чистка своих данных.

Тесты ходят в TEST_DATABASE_URL (тестовый Neon или одноразовый Postgres в CI)
и НИКОГДА в боевую базу: TOBISITE_TEST=1 выставляется здесь раньше импорта
config, а гейт в config.py не даст тестовой строке совпасть с боевой.

Все объекты тестов помечены: работники — tg_id от TEST_TG_BASE, cost-записи —
batch_id «pytest…», FSM-ключи — бот FSM_BOT_ID. По этим меткам чистка сносит
только своё — ручные данные в тестовой базе переживают прогон.

Фикстуры, трогающие базу, — синхронные с asyncio.run: у каждого async-теста
свой event loop, а соединения asyncpg привязаны к loop; общий пул был бы
источником «attached to a different loop» (потому же в тест-режиме NullPool).
"""
import asyncio
import itertools
import os
import pathlib

# раньше ЛЮБОГО импорта проекта: config читает окружение на импорте
os.environ["TOBISITE_TEST"] = "1"
os.environ.setdefault("BOT_TEST_TOKEN", "0:pytest")
os.environ.setdefault("ADMIN_TG_ID", "1")
os.environ.setdefault("ACCESS_CODE", "pytest")
# фиксированные списки: тесты телефонов рассчитывают на регионы UA и SK
os.environ["COUNTRIES"] = "Украина|UA,Словакия|SK"
os.environ["LANGUAGES"] = "Украинский,Словацкий"

import pytest  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent

if not (os.getenv("TEST_DATABASE_URL") or (ROOT / ".env").exists()):
    pytest.exit(
        "Нет TEST_DATABASE_URL (ни в окружении, ни в .env) — тестам нужна "
        "тестовая база. Боевую подставлять нельзя: гейт в config.py не пустит.",
        returncode=3,
    )

from sqlalchemy import delete, select  # noqa: E402

from models import (  # noqa: E402
    ClientService, Contact, CostLedger, FsmState, Lead, LeadEvent, Session,
    Worker,
)

TEST_TG_BASE = 9_900_000_000_000
FSM_BOT_ID = 9_900_012_345
_seq = itertools.count(1)


async def _cleanup():
    async with Session() as s, s.begin():
        wids = list(await s.scalars(
            select(Worker.id).where(Worker.tg_id >= TEST_TG_BASE)
        ))
        if wids:
            lids = list(await s.scalars(
                select(Lead.id).where(Lead.worker_id.in_(wids))
            ))
            if lids:
                await s.execute(delete(LeadEvent).where(LeadEvent.lead_id.in_(lids)))
                await s.execute(delete(CostLedger).where(CostLedger.lead_id.in_(lids)))
                await s.execute(delete(Contact).where(Contact.lead_id.in_(lids)))
                await s.execute(delete(ClientService)
                                .where(ClientService.lead_id.in_(lids)))
                await s.execute(delete(Lead).where(Lead.id.in_(lids)))
            await s.execute(delete(Worker).where(Worker.id.in_(wids)))
        await s.execute(delete(CostLedger).where(CostLedger.batch_id.like("pytest%")))
        await s.execute(delete(FsmState).where(FsmState.key.like(f"{FSM_BOT_ID}:%")))


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """Схема до head + чистка мусора прошлых прогонов до и своего — после."""
    from alembic import command
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(cfg, "head")
    asyncio.run(_cleanup())
    yield
    asyncio.run(_cleanup())


@pytest.fixture
def worker_id():
    """Свежий работник; удалится сессионной чисткой по диапазону tg_id."""
    tg = TEST_TG_BASE + next(_seq)

    async def _make():
        async with Session() as s, s.begin():
            w = Worker(tg_id=tg, name=f"pytest-{tg}")
            s.add(w)
            await s.flush()
            return w.id

    return asyncio.run(_make())


@pytest.fixture
def make_lead(worker_id):
    """async-фабрика лида с обязательными полями; зовётся внутри теста."""
    async def _make(s, **kw):
        lead = Lead(
            worker_id=kw.pop("worker_id", worker_id),
            name=kw.pop("name", f"pytest-lead-{next(_seq)}"),
            source_url=kw.pop("source_url", "https://maps.google.com/pytest"),
            country=kw.pop("country", "Украина"),
            city=kw.pop("city", "Тест-город"),
            language=kw.pop("language", "Украинский"),
            niche=kw.pop("niche", "Стоматология"),
            found_via=kw.pop("found_via", "Google Maps"),
            **kw,
        )
        s.add(lead)
        await s.flush()
        return lead

    return _make
