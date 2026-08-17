from datetime import datetime, timedelta

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer,
    Text, func, text,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import config

engine = create_async_engine(
    config.DATABASE_URL,
    connect_args=config.CONNECT_ARGS,  # см. config.POOLED_DB
    pool_pre_ping=True,
)
Session = async_sessionmaker(engine, expire_on_commit=False)

LEAD_STATUS_KEYS = [k for k, _ in config.STATUSES]
CONTACT_TYPE_KEYS = [k for k, _ in config.CONTACT_TYPES] + ["other"]


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
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    admin_note: Mapped[str | None] = mapped_column(Text)
    draft_url: Mapped[str | None] = mapped_column(Text)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[int | None] = mapped_column(BigInteger)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(in_list("status", LEAD_STATUS_KEYS), name="ck_leads_status"),
        Index(
            "uq_leads_domain_norm_active", "domain_norm",
            unique=True,
            postgresql_where="domain_norm IS NOT NULL AND cancelled_at IS NULL AND deleted_at IS NULL",
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
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(in_list("ctype", CONTACT_TYPE_KEYS), name="ck_contacts_ctype"),
        Index(
            "uq_contacts_phone_norm_active", "value_norm",
            unique=True,
            postgresql_where="ctype = 'phone' AND value_norm IS NOT NULL "
                             "AND deleted_at IS NULL AND lead_cancelled_at IS NULL",
        ),
        Index("ix_contacts_lead_id", "lead_id"),
    )


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
