import hashlib
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer,
    Numeric, SmallInteger, String, Text, UniqueConstraint, and_, func, or_,
    select, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import NullPool

import config
import gap_validation

_engine_kwargs = dict(
    connect_args=config.CONNECT_ARGS,  # см. config.POOLED_DB
    pool_pre_ping=True,
)
if config.TEST_MODE:
    # Соединения asyncpg привязаны к event loop. В pytest каждый тест живёт
    # в своём loop, и пул отдавал бы соединение из чужого — «attached to a
    # different loop». Тестовая база и так за PgBouncer'ом Neon, свой пул
    # поверх ничего не экономит.
    _engine_kwargs["poolclass"] = NullPool

engine = create_async_engine(config.DATABASE_URL, **_engine_kwargs)
Session = async_sessionmaker(engine, expire_on_commit=False)

LEAD_STATUS_KEYS = [k for k, _ in config.STATUSES]
CONTACT_TYPE_KEYS = [k for k, _ in config.CONTACT_TYPES] + ["other"]
# Типы платных операций в cost_ledger. Новый тип = новая запись здесь + миграция
# CHECK-констрейнта, как у статусов лида.
COST_OPS = ["scout", "classify", "draft", "letter", "qa", "other"]
# Статусы подписки клиента на доп-услугу (16.13). Новый статус = запись здесь
# + миграция CHECK-констрейнта, как у статусов лида.
CLIENT_SERVICE_STATUSES = ["active", "paused", "canceled"]
GAP_TYPE_KEYS = [k for k, _ in config.GAP_TYPES]
# Причины отклонения лида (6.17). Новая причина = запись в config
# + миграция CHECK-констрейнта, как у статусов лида.
LEAD_REJECT_KEYS = [k for k, _ in config.LEAD_REJECT_REASONS]
# Что заносится в suppression (7.22): хеш почты, домен, компания «имя+город».
SUPPRESSION_KINDS = ["email_hash", "domain", "company"]
# Отчего компания попала в стоп-лист (9.34). Новое основание = запись здесь
# + миграция CHECK-констрейнта, как у статусов лида.
SUPPRESSION_EVENTS = ["unsubscribe", "complaint", "bounce", "manual"]
# Итог проверки адреса получателя (9.29). NULL — не проверяли; unknown — DNS
# не ответил, и это не «в порядке»: письмо по такому адресу не собирается.
VERIFY_STATUSES = ["valid", "invalid", "unknown"]
# Состояния карточки в очереди одобрения (Д12 §6.5). Отправки в списке нет и
# не будет до интеграции Instantly: конвейер v1 кончается на approved.
# Новый статус = запись здесь + миграция CHECK-констрейнта, как у статусов лида.
DRAFT_STATUSES = ["queued", "claimed", "approved", "rejected", "cancelled",
                  "needs_manual"]
# Кто написал версию текста: модель или человек в очереди (Д12 §6.5).
VERSION_AUTHORS = ["model", "human"]
# Состояния черновика сайта (Д13 §5). published ставится только руками после
# деплоя Worker: автоматической публикации превью в конвейере нет.
# Новый статус = запись здесь + миграция CHECK-констрейнта, как у статусов лида.
BUILD_STATUSES = ["generated", "published", "failed", "expired"]
# Комиссия работника, % от суммы сделки (7.9). Границы — решение основателя
# «15–30%»; они же стоят CHECK-констрейнтом и на workers, и на строке продажи.
COMMISSION_MIN, COMMISSION_MAX = 15, 30
DEFAULT_COMMISSION_PCT = 20
PCT_RANGE = f"BETWEEN {COMMISSION_MIN} AND {COMMISSION_MAX}"
# Черновик живёт 30 дней — ровно столько письмо 3 обещает его держать
# (email_gen.DRAFT_HOLD_DAYS). Разъедутся — письмо станет враньём.
DRAFT_TTL_DAYS = 30
# Наблюдение живёт 14 дней: сайт могли починить, и «8 секунд» превратится
# в ложное утверждение в коммерческом письме конкретному юрлицу (Д12 §2).
GAP_TTL_DAYS = 14
# Проверка почты живёт 30 дней: домены умирают, и «проверено полгода назад»
# не значит ничего (9.29).
VERIFY_TTL_DAYS = 30


def in_list(col: str, values) -> str:
    return "{} IN ({})".format(col, ", ".join(f"'{v}'" for v in values))


class Base(DeclarativeBase):
    pass


class TimesMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
        nullable=False,
    )


class Worker(Base, TimesMixin):
    __tablename__ = "workers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    daily_limit: Mapped[int | None] = mapped_column(Integer)
    # процент работника на сегодня; в продаже он не участвует — там лежит копия
    # на момент сделки (7.13), поэтому смена % задним числом ничего не переписывает
    commission_pct: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=DEFAULT_COMMISSION_PCT,
        server_default=str(DEFAULT_COMMISSION_PCT),
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(f"commission_pct {PCT_RANGE}",
                        name="ck_workers_commission_pct"),
    )


async def _load_admin_worker(tg_id: int) -> Worker | None:
    """Строка админа, если она уже есть. Заодно оживляет отключённую и удалённую."""
    async with Session() as s, s.begin():
        worker = await s.scalar(select(Worker).where(Worker.tg_id == tg_id))
        # без оживления случайное «🗑 Удалить» на своей же карточке — или строка,
        # оставшаяся с тех пор, когда админ регистрировался работником, —
        # навсегда закрыли бы админу добавление компаний
        if worker is not None and (worker.deleted_at or not worker.is_active):
            worker.deleted_at = None
            worker.is_active = True
    return worker


async def ensure_admin_worker(tg_id: int | None = None) -> Worker:
    """Строка админа в workers: у лида worker_id обязателен, а регистрацию админ не проходит.

    Заводится лениво, а не миграцией: так она одинаково появляется и в боевой
    базе, и в тестовой ветке Neon, и в одноразовом Postgres в CI. У второго
    админа (6.16) строка своя — иначе его компании числились бы за первым.
    """
    tg_id = config.ADMIN_TG_ID if tg_id is None else tg_id
    worker = await _load_admin_worker(tg_id)
    if worker is not None:
        return worker
    try:
        async with Session() as s, s.begin():
            worker = Worker(tg_id=tg_id,
                            name=config.ADMIN_NAMES.get(tg_id, config.ADMIN_NAME))
            s.add(worker)
    except IntegrityError:
        # tg_id уникален: параллельный апдейт успел вставить строку первым
        worker = await _load_admin_worker(tg_id)
    if worker is None:
        raise RuntimeError("не удалось создать строку админа в workers")
    return worker


class Lead(Base, TimesMixin):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    worker_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("workers.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    website_url: Mapped[str | None] = mapped_column(Text)
    domain_norm: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    country: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    niche: Mapped[str] = mapped_column(Text, nullable=False)
    google_rating: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    screenshot_file_id: Mapped[str | None] = mapped_column(Text)
    found_via: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="new", server_default="new"
    )
    possible_duplicate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # выставляет только скаут (канал Ads Transparency): компания уже платит
    # за клики — приоритетный сегмент для missed-call и голосовых доп-услуг
    has_ads: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    admin_note: Mapped[str | None] = mapped_column(Text)
    draft_url: Mapped[str | None] = mapped_column(Text)
    # почему лид отклонён (6.17): ключ из config.LEAD_REJECT_REASONS. Живёт
    # ровно столько, сколько сам отказ — со сменой статуса очищается
    reject_reason: Mapped[str | None] = mapped_column(String(32))
    # наблюдение (Д12 §2): один разрыв на лид, без него лид непригоден для
    # персонализации касания 1. Имена полей зафиксированы — параллельных
    # fact_line / human_observation не заводить
    gap_type: Mapped[str | None] = mapped_column(String(32))
    gap_value: Mapped[str | None] = mapped_column(String(160))
    gap_note: Mapped[str | None] = mapped_column(String(120))
    gap_screenshot: Mapped[str | None] = mapped_column(Text)
    gap_captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gap_seconds: Mapped[int | None] = mapped_column(Integer)
    gap_auto_verified: Mapped[bool | None] = mapped_column(Boolean)
    # обогащение карточки под черновик (Д13 §3 шаг 1): всё, чего нет в самих
    # полях лида — услуги, часы, адрес, число фото. Ключ есть в словаре —
    # признак известен, ключа нет — неизвестен; unknown это не false
    enrichment: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # лестница деградации не собрала страницу: работнику ушёл список того,
    # что надо дозаполнить (enrichment_request), черновика у лида нет
    needs_enrichment: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    enrichment_request: Mapped[str | None] = mapped_column(Text)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[int | None] = mapped_column(BigInteger)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(in_list("status", LEAD_STATUS_KEYS), name="ck_leads_status"),
        CheckConstraint(in_list("gap_type", GAP_TYPE_KEYS), name="ck_leads_gap_type"),
        CheckConstraint(in_list("reject_reason", LEAD_REJECT_KEYS),
                        name="ck_leads_reject_reason"),
        # «без наблюдения лид не идёт в рассылку» на уровне БД, а не
        # договорённости. В боевой миграции констрейнт NOT VALID: строки,
        # собранные до наблюдения, проверять нечем
        CheckConstraint("status <> 'verified' OR gap_type IS NOT NULL",
                        name="ck_leads_verified_needs_gap"),
        Index(
            "uq_leads_domain_norm_active", "domain_norm",
            unique=True,
            postgresql_where="domain_norm IS NOT NULL AND cancelled_at IS NULL "
                             "AND deleted_at IS NULL",
        ),
        Index(
            "ix_leads_name_city_lower",
            text("lower(btrim(name))"), text("lower(btrim(city))"),
        ),
        Index("ix_leads_worker_id", "worker_id"),
        Index("ix_leads_country", "country"),
        Index("ix_leads_niche", "niche"),
        Index("ix_leads_status", "status"),
        Index("ix_leads_created_at", "created_at"),
    )


class Contact(Base, TimesMixin):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lead_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("leads.id"), nullable=False
    )
    ctype: Mapped[str] = mapped_column(Text, nullable=False)
    ctype_other: Mapped[str | None] = mapped_column(Text)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    value_norm: Mapped[str | None] = mapped_column(Text)
    # зеркало leads.cancelled_at: предикат частичного индекса не может смотреть в другую таблицу
    lead_cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # проверка адреса перед письмом (9.29): синтаксис плюс MX домена. Пусто —
    # не проверяли; проверка живёт VERIFY_TTL_DAYS, дальше снимается заново
    verify_status: Mapped[str | None] = mapped_column(String(16))
    verify_note: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(in_list("ctype", CONTACT_TYPE_KEYS), name="ck_contacts_ctype"),
        CheckConstraint(in_list("verify_status", VERIFY_STATUSES),
                        name="ck_contacts_verify_status"),
        Index(
            "uq_contacts_phone_norm_active", "value_norm",
            unique=True,
            postgresql_where="ctype = 'phone' AND value_norm IS NOT NULL "
                             "AND deleted_at IS NULL AND lead_cancelled_at IS NULL",
        ),
        Index("ix_contacts_lead_id", "lead_id"),
    )


class FsmState(Base, TimesMixin):
    """Состояние формы (aiogram FSM) в базе, а не в памяти процесса.

    MemoryStorage терял недозаполненные 12-шаговые формы у всех работников при
    каждом деплое. Здесь строка на пользователя: state — имя шага, data — JSON
    накопленных полей. Чистится purge_stale_fsm() при старте бота.
    """
    __tablename__ = "fsm_states"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    state: Mapped[str | None] = mapped_column(Text)
    data: Mapped[str | None] = mapped_column(Text)


class CostLedger(Base, TimesMixin):
    """Журнал расходов на ИИ и платные API (раздел 20 плана).

    Пишется каждым платным вызовом через costs.log_cost(); суммы читает /costs
    и месячный кэп config.AI_MONTHLY_CAP_USD. Токены — BigInteger: месяц
    batch-скаута легко уходит за пределы int4.
    """
    __tablename__ = "cost_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    op: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    output_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    cache_read_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    api_calls: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=0, server_default="0"
    )
    lead_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("leads.id"))
    batch_id: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(in_list("op", COST_OPS), name="ck_cost_ledger_op"),
        Index("ix_cost_ledger_created_at", "created_at"),
        Index("ix_cost_ledger_op", "op"),
    )


class ClientService(Base, TimesMixin):
    """Реестр подписок клиентов на доп-услуги (16.13): кто, что, почём, статус.

    service_id — id из services.yml; FK на YAML не бывает, поэтому список
    проверяет /subs при записи. Сумма price_usd активных строк = MRR доп-услуг.
    """
    __tablename__ = "client_services"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lead_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("leads.id"), nullable=False
    )
    service_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="active", server_default="active"
    )
    price_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=0, server_default="0"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(in_list("status", CLIENT_SERVICE_STATUSES),
                        name="ck_client_services_status"),
        Index("ix_client_services_lead_id", "lead_id"),
        Index("ix_client_services_status", "status"),
    )


class Sale(Base, TimesMixin):
    """Продажа сайта и начисление работнику (7.12–7.13).

    Три момента времени, и они не совпадают: строка создана (лид переведён в
    sold) → received_at (клиент заплатил) → paid_at (работник получил своё).
    Агентский договор начисляет вознаграждение «за фактом надходження коштів»,
    поэтому действительным начисление становится только со второго момента.

    rate_pct — копия workers.commission_pct на момент сделки, а не ссылка:
    смена процента работнику не должна переписывать историю уже закрытых сделок.
    """
    __tablename__ = "sales"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lead_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("leads.id"), nullable=False
    )
    worker_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("workers.id"), nullable=False
    )
    deal_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        Text, nullable=False, default="USD", server_default="USD"
    )
    rate_pct: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    amount_due: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(f"rate_pct {PCT_RANGE}", name="ck_sales_rate_pct"),
        CheckConstraint("deal_amount > 0", name="ck_sales_deal_amount"),
        # одна продажа на лид: повторный перевод в sold не должен начислять
        # работнику второй раз за ту же сделку
        UniqueConstraint("lead_id", name="uq_sales_lead"),
        Index("ix_sales_worker_id", "worker_id"),
        Index("ix_sales_created_at", "created_at"),
    )


class CommissionChange(Base, TimesMixin):
    """История смен процента работника (7.10): кто, когда, было → стало.

    Отдельная таблица, а не lead_events: события лидов — про лидов, а процент —
    про работника, и ни к какому лиду эта запись не привязывается.
    """
    __tablename__ = "commission_changes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    worker_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("workers.id"), nullable=False
    )
    old_pct: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    new_pct: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    changed_by: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (Index("ix_commission_changes_worker_id", "worker_id"),)


def commission_due(deal_amount: Decimal, rate_pct: int) -> Decimal:
    """Начисление работнику. Считается один раз — при записи продажи."""
    return (deal_amount * rate_pct / 100).quantize(Decimal("0.01"),
                                                   rounding=ROUND_HALF_UP)


class Suppression(Base, TimesMixin):
    """Кому мы больше не пишем (7.22): отписки, жалобы, ручные запреты.

    Три пространства значений сразу: почта — только хешем (сам адрес хранить
    незачем), домен — как в domain_norm, компания — «имя|город». Проверяется
    один раз перед сборкой письма, suppression_hit().
    """
    __tablename__ = "suppression"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    value_norm: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(in_list("kind", SUPPRESSION_KINDS), name="ck_suppression_kind"),
        UniqueConstraint("kind", "value_norm", name="uq_suppression_kind_value"),
    )


class SuppressionEvent(Base, TimesMixin):
    """Журнал отписок и жалоб (9.34): что случилось, когда и по чьей просьбе.

    Стоп-лист отвечает на вопрос «писать ли этой компании сейчас», а журнал —
    на вопрос «докажите, что вы прекратили»: строка suppression одна на
    значение и молчит о том, когда и почему появилась, а повторная жалоба её
    вообще не меняет. Поэтому запись сюда идёт всегда, даже если в стоп-листе
    такое значение уже есть.

    Время исполнения тоже здесь: created_at против даты обращения — то самое
    «≤2 дней» из 9.30, и оно у нас нулевое, потому что стоп-лист закрывает
    компанию в той же транзакции.
    """
    __tablename__ = "suppression_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    value_norm: Mapped[str] = mapped_column(Text, nullable=False)
    lead_id: Mapped[int | None] = mapped_column(BigInteger,
                                                ForeignKey("leads.id"))
    # откуда пришло обращение: «ответ на письмо», «звонок», «Instantly»
    source: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    actor_tg_id: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        CheckConstraint(in_list("event", SUPPRESSION_EVENTS),
                        name="ck_suppression_events_event"),
        CheckConstraint(in_list("kind", SUPPRESSION_KINDS),
                        name="ck_suppression_events_kind"),
        Index("ix_suppression_events_created_at", "created_at"),
        Index("ix_suppression_events_lead_id", "lead_id"),
    )


def email_key(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


def company_key(name: str, city: str) -> str:
    return f"{(name or '').strip().lower()}|{(city or '').strip().lower()}"


async def suppression_keys(session, lead) -> list[tuple[str, str]]:
    """Три пространства значений лида: компания, домен, хеши его адресов.

    Один список и на проверку, и на запись — иначе стоп-лист однажды закроет
    не то, что проверяет.
    """
    pairs = [("company", company_key(lead.name, lead.city))]
    if lead.domain_norm:
        pairs.append(("domain", lead.domain_norm))
    emails = await session.scalars(
        select(Contact.value).where(
            Contact.lead_id == lead.id,
            Contact.ctype == "email",
            Contact.deleted_at.is_(None),
        )
    )
    return pairs + [("email_hash", email_key(v)) for v in emails]


async def suppression_hit(session, lead) -> bool:
    """Лид в стоп-листе хотя бы по одному из трёх пространств значений."""
    pairs = await suppression_keys(session, lead)
    return bool(await session.scalar(
        select(Suppression.id).where(or_(*[
            and_(Suppression.kind == k, Suppression.value_norm == v)
            for k, v in pairs
        ])).limit(1)
    ))


async def suppress_lead(session, lead, *, event: str, source: str,
                        note: str | None = None,
                        actor_tg_id: int | None = None) -> int:
    """Стоп-лист по всем трём пространствам сразу + запись в журнал (9.27, 9.34).

    Отписка глобальна: адрес, домен и компания закрываются одной операцией,
    иначе то же юрлицо получило бы письмо со второго адреса. Сколько ключей
    добавилось — возвращается; ноль значит «уже был закрыт», и это не ошибка:
    в журнал повторное обращение всё равно попадает.
    """
    pairs = await suppression_keys(session, lead)
    added = 0
    for kind, value in pairs:
        result = await session.execute(
            pg_insert(Suppression)
            .values(kind=kind, value_norm=value, reason=event, source=source)
            .on_conflict_do_nothing(constraint="uq_suppression_kind_value")
        )
        added += result.rowcount or 0
    # в журнале одна строка на обращение, а не на каждое значение: доказывать
    # приходится факт и дату, а не устройство нашего стоп-листа
    main_kind, main_value = pairs[-1]
    session.add(SuppressionEvent(
        event=event, kind=main_kind, value_norm=main_value, lead_id=lead.id,
        source=source, note=note, actor_tg_id=actor_tg_id,
    ))
    return added


def gap_age_days(lead) -> int | None:
    if not lead.gap_captured_at:
        return None
    now = datetime.now(lead.gap_captured_at.tzinfo)
    return (now - lead.gap_captured_at).days


def gap_stale(lead) -> bool:
    """Наблюдению больше GAP_TTL_DAYS — писать по нему уже нельзя, надо переснять."""
    age = gap_age_days(lead)
    return age is not None and age > GAP_TTL_DAYS


async def gap_repeated(session, worker_id: int, gap_type: str, value: str | None,
                       note: str | None, exclude_lead_id: int | None = None) -> bool:
    """Такое же наблюдение уже есть среди последних 30 лидов этого работника.

    Правило 4 Д12 §2: копипаста одной и той же фразы по десятку карточек —
    самый дешёвый способ халтуры и самый дорогой по последствиям (факт в письме
    оказывается чужим). Сам переснимаемый лид из сравнения исключается: сайт
    мог не измениться, и повторить прежнее значение — честный ответ.
    """
    target = gap_validation.copypaste_hash(gap_type, value, note)
    if target is None:
        return False
    conditions = [Lead.worker_id == worker_id, Lead.gap_type.isnot(None)]
    if exclude_lead_id is not None:
        conditions.append(Lead.id != exclude_lead_id)
    rows = await session.execute(
        select(Lead.gap_type, Lead.gap_value, Lead.gap_note)
        .where(*conditions).order_by(Lead.id.desc()).limit(30)
    )
    return any(gap_validation.copypaste_hash(t, v, n) == target for t, v, n in rows)


class MessageDraft(Base, TimesMixin):
    """Карточка очереди одобрения: одно касание одного лида (Д12 §6.5).

    Лиз живёт прямо в строке (claimed_by/claimed_at/expires_at), отдельного
    индекса клейма нет: карточку выдаёт один атомарный UPDATE с условием
    «queued либо лиз истёк» — двое одну и ту же взять не могут.
    """
    __tablename__ = "message_drafts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lead_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("leads.id"), nullable=False
    )
    touch_number: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(
        Text, nullable=False, default="email", server_default="email"
    )
    lang: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="queued", server_default="queued"
    )
    claimed_by: Mapped[int | None] = mapped_column(BigInteger)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    shown_version_id: Mapped[int | None] = mapped_column(BigInteger)
    # сколько лизов подряд истекло, ни разу не кончившись решением: на третьем
    # карточку эскалируем админу (Д12 §6.5). Обнуляется любым решением
    expired_leases: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    __table_args__ = (
        CheckConstraint(in_list("status", DRAFT_STATUSES),
                        name="ck_message_drafts_status"),
        # двойное нажатие не создаст второе письмо того же касания (Д12 §6.5)
        UniqueConstraint("lead_id", "touch_number",
                         name="uq_message_drafts_lead_touch"),
        Index("ix_message_drafts_status", "status"),
    )


class MessageVersion(Base, TimesMixin):
    """История текста карточки: что сгенерировано и что человек поправил.

    Это обучающий сигнал раздела 7 Д12, а не журнал: по парам «модель →
    человек» с diff_ratio от 0,3 собирается банк примеров, а prompt_version
    позволяет считать метрики по версиям промпта.
    """
    __tablename__ = "message_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    draft_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("message_drafts.id"), nullable=False
    )
    author: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    slots_json: Mapped[dict | None] = mapped_column(JSONB)
    edited_slots: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    # доля изменённого текста, 0 — не тронули, 1 — переписали заново
    diff_ratio: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    prompt_version: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(in_list("author", VERSION_AUTHORS),
                        name="ck_message_versions_author"),
        Index("ix_message_versions_draft_id", "draft_id"),
    )


class Draft(Base, TimesMixin):
    """Черновик сайта лида (Д13 §5): один активный на лид.

    recipe_json — полный след решения движка, включая отвергнутые варианты и их
    score. Без него признаковый анализ через полгода превращается в тыкву, и
    восстановить, почему этому лиду достался именно такой первый экран, будет
    нечем. checks_json — то, что нашли автопроверки; пустой словарь значит
    «чисто».

    r2_prefix и preview_host заполняются только при ручной публикации: пока
    Worker не задеплоен, черновик живёт в базе и в письме, но не в интернете.
    """
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lead_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("leads.id"), nullable=False
    )
    library_version: Mapped[str | None] = mapped_column(Text)
    seed: Mapped[int | None] = mapped_column(BigInteger)
    recipe_id: Mapped[str | None] = mapped_column(Text)
    token_preset: Mapped[str | None] = mapped_column(Text)
    section_variants: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    image_ids: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    recipe_json: Mapped[dict | None] = mapped_column(JSONB)
    r2_prefix: Mapped[str | None] = mapped_column(Text)
    preview_host: Mapped[str | None] = mapped_column(Text)
    checks_json: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="generated", server_default="generated"
    )
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(in_list("status", BUILD_STATUSES), name="ck_drafts_status"),
        # один живой черновик на лид: второй «Собрать черновик» пересобирает
        # тот же, а не плодит вторую страницу с другим дизайном
        Index("uq_drafts_lead_active", "lead_id", unique=True,
              postgresql_where="deleted_at IS NULL"),
        Index("ix_drafts_status", "status"),
    )


def draft_fresh(draft) -> bool:
    """Черновик, на который письмо ещё может ссылаться."""
    if draft is None or draft.deleted_at or draft.status not in ("generated",
                                                                 "published"):
        return False
    if draft.expires_at is None:
        return True
    return datetime.now(draft.expires_at.tzinfo) < draft.expires_at


class LeadEvent(Base, TimesMixin):
    __tablename__ = "lead_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lead_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("leads.id"), nullable=False
    )
    event: Mapped[str] = mapped_column(Text, nullable=False)
    field: Mapped[str | None] = mapped_column(Text)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    actor_tg_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (Index("ix_lead_events_lead_id", "lead_id"),)


def log_event(session, lead_id, event, actor_tg_id, field=None, old=None, new=None):
    session.add(LeadEvent(
        lead_id=lead_id, event=event, actor_tg_id=actor_tg_id,
        field=field, old_value=old, new_value=new,
    ))


def constraint_of(exc) -> str:
    orig = getattr(exc, "orig", None)
    name = getattr(orig, "constraint_name", None)
    if not name:
        name = getattr(getattr(orig, "diag", None), "constraint_name", None)
    return name or ""


def dup_message(exc) -> str:
    name = constraint_of(exc)
    if name == "uq_leads_domain_norm_active":
        return "❌ Такой сайт уже есть в базе."
    if name == "uq_contacts_phone_norm_active":
        return "❌ Такой телефон уже есть в базе."
    return "❌ Эта компания уже есть в базе."


def day_start(days_back: int = 0) -> datetime:
    now = datetime.now(config.TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start - timedelta(days=days_back)


def month_start() -> datetime:
    """Начало текущего месяца по нашему поясу — окно месячного кэпа ИИ."""
    now = datetime.now(config.TZ)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
