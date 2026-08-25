"""Открытия превью: разбор ключей из R2, уведомление, отметка лида (10.20–10.22).

Сети нет: бакет подменяет фикстура r2 из conftest, Telegram — FakeBot. Хиты
кладутся в фальшивый бакет ровно теми ключами, которые пишет воркер.
"""
import itertools
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import config
import draft_service
import preview_hits
from models import Draft, Lead, LeadEvent, PreviewHit, Session, Worker
from test_notifications import FakeBot

_slugs = itertools.count(1)


def key(slug: str, event: str = "view", at: datetime | None = None,
        nonce: str = "a1b2c3d4") -> str:
    """Ключ ровно того вида, который кладёт воркер."""
    at = at or datetime.now(config.TZ)
    return (f"{preview_hits.PREFIX}{slug}/{event}/"
            f"{int(at.timestamp() * 1000)}-{nonce}")


def moment(minutes_ago: int = 0) -> datetime:
    # миллисекунды: точнее ключ воркера всё равно не помнит
    return (datetime.now(config.TZ) - timedelta(minutes=minutes_ago)).replace(
        microsecond=0
    )


@pytest.fixture
def published(make_lead):
    """Лид с опубликованным превью; слаг у каждого свой, как на бою."""
    async def _make(**kw):
        slug = f"pytest-preview-{next(_slugs)}"
        async with Session() as s, s.begin():
            lead = await make_lead(s, **kw)
            s.add(Draft(lead_id=lead.id, status="published", r2_prefix=slug,
                        preview_host=f"{slug}.tobisitepreview.com",
                        published_at=datetime.now(config.TZ)))
            return SimpleNamespace(id=lead.id, slug=slug,
                                   worker_id=lead.worker_id)

    return _make


async def _hits(lead_id: int) -> list[PreviewHit]:
    async with Session() as s:
        return list(await s.scalars(
            select(PreviewHit).where(PreviewHit.lead_id == lead_id)
            .order_by(PreviewHit.happened_at)
        ))


async def _lead(lead_id: int) -> Lead:
    async with Session() as s:
        return await s.get(Lead, lead_id)


async def _worker_tg(worker_id: int) -> int:
    async with Session() as s:
        return (await s.get(Worker, worker_id)).tg_id


# --- разбор ключа -------------------------------------------------------------

def test_key_of_the_worker_is_understood():
    at = datetime(2026, 8, 25, 12, 30, tzinfo=config.TZ)
    parsed = preview_hits.parse_key(key("zubna-feia", "cta_click", at))
    assert parsed == ("zubna-feia", "cta_click", at)


@pytest.mark.parametrize("bad", [
    "_hits/slug/view",                       # без хвоста
    "_hits/slug/view/nope-1",                # время не число
    "_hits/ПЛОХОЙ/view/1756000000000-a1",    # слаг не слаг
    "_hits/slug/hover/1756000000000-a1",     # события такого нет
    "other/slug/view/1756000000000-a1",      # чужой префикс
])
def test_broken_keys_are_not_guessed(bad):
    assert preview_hits.parse_key(bad) is None


# --- заход поллера ------------------------------------------------------------

async def test_first_open_lands_in_the_base_and_reaches_people(published, r2,
                                                               monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [config.ADMIN_TG_ID])
    lead = await published()
    first, second = moment(5), moment(4)
    r2.objects[key(lead.slug, "view", first, "one")] = ""
    r2.objects[key(lead.slug, "scroll50", second, "two")] = ""
    bot = FakeBot()

    assert await preview_hits.poll_once(bot) == 2

    rows = await _hits(lead.id)
    assert [(r.event, r.happened_at) for r in rows] == [("view", first),
                                                        ("scroll50", second)]
    assert all(r.slug == lead.slug for r in rows)
    assert (await _lead(lead.id)).preview_opened_at == first
    # разобранное из бакета уходит: следующий заход не увидит того же самого
    assert not r2.objects
    async with Session() as s:
        events = list(await s.scalars(
            select(LeadEvent).where(LeadEvent.lead_id == lead.id,
                                    LeadEvent.event == "preview_opened")
        ))
    assert len(events) == 1
    # админ и работник, который лид нашёл
    assert {chat for chat, _ in bot.sent} == {config.ADMIN_TG_ID,
                                             await _worker_tg(lead.worker_id)}
    assert all("открыл превью" in text for _, text in bot.sent)


async def test_second_visit_is_recorded_but_stays_quiet(published, r2):
    lead = await published()
    first = moment(120)
    r2.objects[key(lead.slug, "view", first, "one")] = ""
    await preview_hits.poll_once(FakeBot())

    r2.objects[key(lead.slug, "view", moment(), "two")] = ""
    bot = FakeBot()
    assert await preview_hits.poll_once(bot) == 1

    assert len(await _hits(lead.id)) == 2
    assert bot.sent == []
    # отметка первого открытия остаётся первой
    assert (await _lead(lead.id)).preview_opened_at == first


async def test_same_object_twice_does_not_double_the_row(published, r2):
    lead = await published()
    same = key(lead.slug, nonce="repeat")
    r2.objects[same] = ""
    await preview_hits.poll_once(FakeBot())
    r2.objects[same] = ""                  # удаление из бакета не прошло

    assert await preview_hits.poll_once(FakeBot()) == 1
    assert len(await _hits(lead.id)) == 1


async def test_junk_key_is_swept_and_does_not_stop_the_rest(published, r2):
    lead = await published()
    r2.objects[f"{preview_hits.PREFIX}кто-то-положил-это-руками"] = ""
    r2.objects[key(lead.slug)] = ""

    assert await preview_hits.poll_once(FakeBot()) == 1

    assert len(await _hits(lead.id)) == 1
    assert not r2.objects


async def test_hit_of_an_unknown_slug_is_dropped(published, r2):
    lead = await published()
    r2.objects[key("never-published")] = ""

    bot = FakeBot()
    assert await preview_hits.poll_once(bot) == 0

    assert await _hits(lead.id) == [] and bot.sent == []
    assert not r2.objects                  # висеть в бакете такому незачем


async def test_hits_of_a_removed_preview_are_not_lost(published, r2):
    """Превью снято (черновик expired), но слаг и строка у лида остались."""
    lead = await published()
    async with Session() as s, s.begin():
        draft = await s.scalar(select(Draft).where(Draft.lead_id == lead.id))
        draft.status = "expired"
    r2.objects[key(lead.slug)] = ""

    assert await preview_hits.poll_once(FakeBot()) == 1
    assert len(await _hits(lead.id)) == 1


async def test_disabled_worker_is_not_written_to(published, r2, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [config.ADMIN_TG_ID])
    lead = await published()
    async with Session() as s, s.begin():
        (await s.get(Worker, lead.worker_id)).is_active = False
    r2.objects[key(lead.slug)] = ""

    bot = FakeBot()
    await preview_hits.poll_once(bot)

    assert [chat for chat, _ in bot.sent] == [config.ADMIN_TG_ID]


async def test_blocked_chat_does_not_break_the_run(published, r2, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [config.ADMIN_TG_ID])
    lead = await published()
    r2.objects[key(lead.slug)] = ""

    await preview_hits.poll_once(FakeBot(blocked=[config.ADMIN_TG_ID]))

    assert len(await _hits(lead.id)) == 1
    assert (await _lead(lead.id)).preview_opened_at is not None


async def test_unwritten_hits_stay_in_the_bucket(published, r2, monkeypatch):
    """База отказала — объекты ждут следующего захода, а не пропадают."""
    lead = await published()
    r2.objects[key(lead.slug)] = ""

    async def _boom(*a, **kw):
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(preview_hits, "_save", _boom)
    assert await preview_hits.poll_once(FakeBot()) == 0

    assert len(r2.objects) == 1
    assert await _hits(lead.id) == []


async def test_without_r2_keys_the_poller_sleeps(monkeypatch):
    for name in draft_service.R2_ENV:
        monkeypatch.delenv(name, raising=False)
    bot = FakeBot()

    assert await preview_hits.poll_once(bot) == 0
    assert bot.sent == []
