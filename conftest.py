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
import json
import os
import pathlib
import time
from datetime import datetime
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

from sqlalchemy import delete, select, tuple_  # noqa: E402

import outbound  # noqa: E402
from models import (  # noqa: E402
    ClientService, CommissionChange, Contact, CostLedger, Draft, FsmState,
    Invoice, Lead, LeadEvent, MessageDraft, MessageVersion, PreviewHit, Sale,
    Session, Setting, Suppression, SuppressionEvent, Worker, suppression_keys,
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
                # ключи стоп-листа считаем до удаления контактов: у строк,
                # оставленных командой /stop, боевой source, и по нему их от
                # настоящих не отличить — только по значению ключа
                pairs = []
                for lead in await s.scalars(select(Lead).where(Lead.id.in_(lids))):
                    pairs += await suppression_keys(s, lead)
                await s.execute(delete(Suppression).where(
                    tuple_(Suppression.kind, Suppression.value_norm).in_(pairs)
                ))
                # версии → карточки → лид: FK держит порядок удаления
                dids = list(await s.scalars(
                    select(MessageDraft.id).where(MessageDraft.lead_id.in_(lids))
                ))
                if dids:
                    await s.execute(delete(MessageVersion)
                                    .where(MessageVersion.draft_id.in_(dids)))
                    await s.execute(delete(MessageDraft)
                                    .where(MessageDraft.id.in_(dids)))
                await s.execute(delete(Draft).where(Draft.lead_id.in_(lids)))
                await s.execute(delete(PreviewHit)
                                .where(PreviewHit.lead_id.in_(lids)))
                # счета — раньше продаж: invoices.sale_id держит строку продажи
                await s.execute(delete(Invoice).where(Invoice.lead_id.in_(lids)))
                await s.execute(delete(Sale).where(Sale.lead_id.in_(lids)))
                await s.execute(delete(LeadEvent).where(LeadEvent.lead_id.in_(lids)))
                await s.execute(delete(CostLedger).where(CostLedger.lead_id.in_(lids)))
                await s.execute(delete(Contact).where(Contact.lead_id.in_(lids)))
                await s.execute(delete(ClientService)
                                .where(ClientService.lead_id.in_(lids)))
                await s.execute(delete(SuppressionEvent)
                                .where(SuppressionEvent.lead_id.in_(lids)))
                await s.execute(delete(Lead).where(Lead.id.in_(lids)))
            await s.execute(delete(CommissionChange)
                            .where(CommissionChange.worker_id.in_(wids)))
            await s.execute(delete(Worker).where(Worker.id.in_(wids)))
        await s.execute(delete(CostLedger).where(CostLedger.batch_id.like("pytest%")))
        await s.execute(delete(SuppressionEvent)
                        .where(SuppressionEvent.source.like("pytest%")))
        await s.execute(delete(Suppression).where(Suppression.source.like("pytest%")))
        await s.execute(delete(FsmState).where(FsmState.key.like(f"{FSM_BOT_ID}:%")))
        # экстренный стоп — единственный на всю базу: прогон, упавший с
        # включённым флагом, иначе закрыл бы очередь и следующему
        await s.execute(delete(Setting).where(Setting.key == outbound.KEY))


async def wipe_cards():
    """Убрать из очереди карточки тестовых лидов.

    Очередь общая на всю базу, а claim_next выдаёт самую старую карточку какая
    есть: без чистки тест получал бы соседскую. Зовётся из тестов очереди
    фикстурой вокруг каждого из них.
    """
    async with Session() as s, s.begin():
        leads = select(Lead.id).where(Lead.worker_id.in_(
            select(Worker.id).where(Worker.tg_id >= TEST_TG_BASE)
        ))
        drafts = list(await s.scalars(
            select(MessageDraft.id).where(MessageDraft.lead_id.in_(leads))
        ))
        if drafts:
            await s.execute(delete(MessageVersion)
                            .where(MessageVersion.draft_id.in_(drafts)))
            await s.execute(delete(MessageDraft)
                            .where(MessageDraft.id.in_(drafts)))


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


# --- генерация писем ----------------------------------------------------------
#
# Сети в тестах писем нет: клиент модели подменяется фальшивкой, которая отдаёт
# заранее заданные ответы и запоминает, с чем её позвали. Реальный API не
# дёргается ни разу — ни ключа, ни денег, ни писем наружу.


class FakeMessages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        text = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=120, output_tokens=60,
                                  cache_read_input_tokens=1800,
                                  cache_creation_input_tokens=0),
        )


class FakeR2:
    """Бакет в памяти вместо R2. fail — исключение, которым отвечает вызов.

    refuse — ключи, которые бакет отказывается удалять: delete_objects отвечает
    на них 200 с Errors внутри, ровно как настоящий R2.

    ops — журнал операций по порядку (put, copy, delete): по нему тесты
    проверяют, что картинки легли в бакет РАНЬШЕ index.html, а прежняя папка
    подметается ПОЗЖЕ копий, а не просто что в бакете нужный набор ключей.
    """

    def __init__(self):
        self.objects = {}
        self.puts = []
        self.copies = []
        self.ops = []
        self.fail = None
        self.refuse = set()
        self.refuse_copy = set()

    def put_object(self, **kw):
        self._check()
        self.puts.append(kw)
        self.ops.append(("put", kw["Key"]))
        self.objects[kw["Key"]] = kw["Body"]
        return {}

    def copy_object(self, **kw):
        """Копия на стороне бакета: байты через бота не проходят."""
        self._check()
        source = kw["CopySource"]["Key"]
        if source in self.refuse_copy:
            raise RuntimeError(f"pytest не даёт скопировать {source}")
        if source not in self.objects:
            raise RuntimeError(f"нет объекта {source}")
        self.copies.append(kw)
        self.ops.append(("copy", kw["Key"]))
        self.objects[kw["Key"]] = self.objects[source]
        return {}

    def list_objects_v2(self, **kw):
        self._check()
        keys = sorted(k for k in self.objects if k.startswith(kw["Prefix"]))
        return {"Contents": [{"Key": k} for k in keys[:kw.get("MaxKeys", 1000)]]}

    def delete_objects(self, **kw):
        self._check()
        errors = []
        for obj in kw["Delete"]["Objects"]:
            if obj["Key"] in self.refuse:
                errors.append({"Key": obj["Key"], "Code": "AccessDenied",
                               "Message": "pytest не даёт удалить"})
                continue
            self.objects.pop(obj["Key"], None)
            self.ops.append(("delete", obj["Key"]))
        return {"Errors": errors} if errors else {}

    def _check(self):
        if self.fail:
            raise self.fail


@pytest.fixture
def r2(monkeypatch):
    """Ключи R2 в окружении и подменённый клиент: публикация без сети."""
    import draft_service

    fake = FakeR2()
    for name in draft_service.R2_ENV:
        monkeypatch.setenv(name, "pytest")
    monkeypatch.setattr(draft_service, "_s3", fake)
    return fake


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    """Проверка почты (9.29) в тестах в интернет не ходит.

    По умолчанию домен принимает почту: иначе письма зависели бы от того, что
    видит резолвер машины, на которой идёт прогон. Тесту, которому нужен
    мёртвый домен или молчащий DNS, подменять эту же функцию своей.
    """
    import email_verify

    async def _accepts(domain):
        return True

    email_verify._domain_cache.clear()
    monkeypatch.setattr(email_verify, "domain_accepts_mail", _accepts)


@pytest.fixture
def model(monkeypatch):
    """model(ответ, ...) — подменяет клиента и заполняет подпись как на бою."""
    import config
    import email_gen

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "pytest-key")
    monkeypatch.setattr(config, "SIGNATURE_NAME", "Микола Тобі")
    monkeypatch.setattr(config, "SIGNATURE_COMPANY", "tobisite")
    monkeypatch.setattr(config, "POSTAL_ADDRESS", "вулиця Соборна 12, Київ")

    def _install(*replies):
        fake = SimpleNamespace(messages=FakeMessages(replies))
        monkeypatch.setattr(email_gen, "_client", fake)
        return fake

    return _install


@pytest.fixture
def slot_model(monkeypatch):
    """slot_model(ответ, ...) — тот же приём для слот-генерации черновиков."""
    import config
    import slot_gen

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "pytest-key")

    def _install(*replies):
        fake = SimpleNamespace(messages=FakeMessages(replies))
        monkeypatch.setattr(slot_gen, "_client", fake)
        return fake

    return _install


@pytest.fixture
def gate_model(monkeypatch):
    """gate_model(ответ, ...) — тот же приём для ИИ-гейта спорных карточек."""
    import config
    from scout import gate

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "pytest-key")

    def _install(*replies):
        fake = SimpleNamespace(messages=FakeMessages(replies))
        monkeypatch.setattr(gate, "_client", fake)
        return fake

    return _install


@pytest.fixture
def enrich_model(monkeypatch):
    """enrich_model(ответ, ...) — тот же приём для ИИ-ветки обогащения.

    Заодно поднимает ENRICH_AI: на бою ветка выключена, и без флага фальшивую
    модель просто не спросили бы.
    """
    import config
    import enrich_service

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "pytest-key")
    monkeypatch.setattr(config, "ENRICH_AI", True)

    def _install(*replies):
        fake = SimpleNamespace(messages=FakeMessages(replies))
        monkeypatch.setattr(enrich_service, "_client", fake)
        return fake

    return _install


# Обогащение карточки под черновик (Д13 §3 шаг 1). Компания выдумана целиком,
# телефон несуществующий; цифры — входные данные теста, а не текст страницы.
DRAFT_ENRICHMENT = {
    "services": ["Лікування карієсу", "Професійна гігієна", "Імплантація",
                 "Протезування"],
    "hours": ["Пн–Пт: 09:00–18:00"],
    "address": "вул. Тестова, 1, Тест-город",
    "address_parts": {"street": "вул. Тестова, 1", "locality": "Тест-город",
                      "country": "UA"},
    "photo_count": 0,
    "review_count": 24,
    "google_rating": 4.7,
    "has_prices": False,
    "text_volume": "medium",
    "old_site_state": "outdated",
    "images": {},
}


@pytest.fixture
def draft_lead(make_lead):
    """Лид, из которого черновик собирается: контакты плюс обогащение."""
    import config
    from models import Session

    async def _make(**kw):
        phone = kw.pop("phone", "+380 00 000 00 05")
        email = kw.pop("email", "office@example.com")
        async with Session() as s, s.begin():
            lead = await make_lead(
                s,
                domain_norm=kw.pop("domain_norm",
                                   f"draft-{next(_seq)}.example"),
                enrichment=kw.pop("enrichment", dict(DRAFT_ENRICHMENT)),
                gap_type=kw.pop("gap_type", "slow"),
                gap_value=kw.pop("gap_value", "8"),
                gap_captured_at=kw.pop("gap_captured_at",
                                       datetime.now(config.TZ)),
                **kw,
            )
            for ctype, value in (("phone", phone), ("email", email)):
                if value:
                    s.add(Contact(lead_id=lead.id, ctype=ctype, value=value))
        return lead

    return _make


# Строки, которыми фальшивая модель закрывает free-слоты: короче любого
# max_chars библиотеки, без цифр и без стоп-слов — чтобы автопроверки ловили
# ошибки движка, а не заготовку теста.
SLOT_LINES = {
    "eyebrow": "Стоматологія",
    "headline": "Запис на прийом",
    "lede": "Зателефонуйте нам.",
    "call_label": "Зателефонувати",
    "secondary_label": "Написати",
    "reassurance": "Відповідаємо вдень",
    "section_title": "Наші послуги",
    "address_label": "Адреса",
    "hours_label": "Години",
    "map_alt": "Карта розташування",
    "portrait_alt": "Фото компанії",
    "name_label": "Імʼя",
    "phone_label": "Телефон",
    "message_label": "Про завдання",
    "submit_label": "Надіслати",
    "honeypot_label": "Не заповнюйте",
    "privacy_note": "Пишемо у відповідь.",
    "contacts_title": "Контакти",
    "hours_title": "Години",
    "company_label": "Компанія",
    "about_text": "Приймаємо замовлення телефоном і у формі на цій сторінці.",
    "legal_line": "Чернетка сторінки.",
}


def _slot_reply(specs, overrides=None) -> str:
    texts = {spec["slot"]: SLOT_LINES.get(spec["kind"], "Рядок") for spec in specs}
    texts.update(overrides or {})
    return json.dumps(texts, ensure_ascii=False)


@pytest.fixture
def slot_plan():
    """slot_plan(лид) — профиль и композиция ровно те, что увидит сборка."""
    import draft_service
    import slot_gen
    from models import Session

    async def _plan(lead):
        async with Session() as s:
            profile = await draft_service.build_profile(s, lead)
        composition = draft_service.compose_for(profile)
        return SimpleNamespace(profile=profile, sections=composition.sections,
                               specs=slot_gen.slot_specs(composition.sections))

    return _plan


@pytest.fixture
def slot_answer(slot_model, slot_plan):
    """slot_answer(лид, правка, ...) — модель, отвечающая на реальную композицию.

    Ответ собирается по free-слотам композиции этого лида, поэтому тест не
    зависит от того, какие варианты секций выиграли скоринг. Правка — свой
    ответ на каждый вызов по порядку: slot_answer(lead, {слот: длинно}, {})
    отдаёт сначала нарушение лимита, потом чистый ответ.
    """
    async def _install(lead, *overrides):
        plan = await slot_plan(lead)
        plan.fake = slot_model(*[_slot_reply(plan.specs, patch)
                                 for patch in overrides or ({},)])
        return plan

    return _install


@pytest.fixture
def gap_lead(make_lead):
    """Лид со свежим наблюдением: то, из чего письмо вообще можно собрать."""
    import config
    from models import Session

    async def _make(**kw):
        # имени контакта в схеме пока нет — оно придёт с обогащением карточки
        contact_name = kw.pop("contact_name", "Олена")
        async with Session() as s, s.begin():
            lead = await make_lead(
                s, gap_type=kw.pop("gap_type", "slow"),
                gap_value=kw.pop("gap_value", "8"),
                gap_captured_at=kw.pop("gap_captured_at",
                                       datetime.now(config.TZ)),
                **kw,
            )
        lead.contact_name = contact_name
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
