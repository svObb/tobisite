"""Инфраструктура pytest: тест-режим, схема, чистка своих данных.

Тесты ходят в TEST_DATABASE_URL (тестовый Neon или одноразовый Postgres в CI)
и НИКОГДА в боевую базу: TOBISITE_TEST=1 выставляется здесь раньше импорта
config, а гейт в config.py не даст тестовой строке совпасть с боевой.

Все объекты тестов помечены: работники — tg_id от TEST_TG_BASE, cost-записи —
batch_id «pytest…», строки suppression — source «pytest», FSM-ключи — бот
FSM_BOT_ID. По этим меткам чистка сносит
только своё — ручные данные в тестовой базе переживают прогон.

Фикстуры, трогающие базу, — синхронные с asyncio.run: у каждого async-теста
свой event loop, а соединения asyncpg привязаны к loop; общий пул был бы
источником «attached to a different loop» (потому же в тест-режиме NullPool).
"""
import asyncio
import itertools
import os
import pathlib
import time
from types import SimpleNamespace

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
    Suppression, Worker,
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
        await s.execute(delete(Suppression).where(Suppression.source.like("pytest%")))
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


# --- админ-панель ------------------------------------------------------------
#
# Сети в её тестах нет вообще: RSA-ключ генерится здесь и отдаётся verifier'у
# прямо в конструктор, поэтому за JWKS Cloudflare он не ходит, а запросы идут
# в приложение через httpx.ASGITransport — без сокетов и без сервера.
# Импорты внутри фикстур: этот conftest грузится и для тестов бота, которым
# fastapi с PyJWT ни к чему.

ACCESS_TEAM_DOMAIN = "pytest.cloudflareaccess.com"
ACCESS_ISSUER = f"https://{ACCESS_TEAM_DOMAIN}"
ACCESS_AUD = "pytest-aud"
ACCESS_EMAIL = "founder@tobisite.com"
ACCESS_KID = "pytest-kid"


@pytest.fixture(scope="session")
def access_key():
    """RSA-ключ, JWKS с ним и фабрика токенов Cloudflare Access поверх него."""
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    public |= {"kid": ACCESS_KID, "alg": "RS256", "use": "sig"}

    def token(*, email=ACCESS_EMAIL, aud=ACCESS_AUD, issuer=ACCESS_ISSUER,
              kid=ACCESS_KID, lifetime=300):
        now = int(time.time())
        return jwt.encode(
            {"aud": aud, "iss": issuer, "email": email, "sub": "pytest",
             "iat": now, "exp": now + lifetime},
            private, algorithm="RS256", headers={"kid": kid},
        )

    return SimpleNamespace(jwks={"keys": [public]}, token=token)


@pytest.fixture
def admin(access_key):
    """Панель и запрос к ней: .get(path) — с валидным токеном, token=None — без.

    База — та же тестовая: config.DATABASE_URL в тест-режиме указывает на неё,
    и гейт в config.py не даёт ей совпасть с боевой.
    """
    import config
    import httpx
    from admin.app import create_app
    from admin.auth import CF_HEADER, AccessVerifier

    verifier = AccessVerifier(team_domain=ACCESS_TEAM_DOMAIN, aud=ACCESS_AUD,
                              allowed_emails=[ACCESS_EMAIL],
                              jwks=access_key.jwks)
    app = create_app(db_url=config.DATABASE_URL, verifier=verifier)

    async def request(method, path, token=..., headers=None, **kw):
        sent = {} if token is None else {
            CF_HEADER: access_key.token() if token is ... else token
        }
        sent.update(headers or {})
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://admin") as client:
            return await client.request(method, path, headers=sent, **kw)

    async def get(path, token=..., **kw):
        return await request("GET", path, token, **kw)

    return SimpleNamespace(app=app, get=get, request=request,
                           token=access_key.token, email=ACCESS_EMAIL)
