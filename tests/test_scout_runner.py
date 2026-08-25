"""Прогон скаута целиком: скоринг → гейт → ingest → дайджест (15.15, 15.18).

Сети нет: probe и гейт подменяются, PageSpeed не зовётся (у кандидата нет
сайта). Проверяется строка, которая решает судьбу спорной карточки, — фильтр
перед ingest, и то, что дайджест не врёт про частично упавший гейт.
"""
import itertools

from sqlalchemy import select

import config
from models import Lead, Session, Worker
from scout import runner
from scout.gate import GateResult
from scout.types import RawBiz

_seq = itertools.count(1)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)


def _gray(gate_write=None) -> RawBiz:
    """Карточка, которую скоринг посчитает спорной: сайт есть, но не открылся."""
    n = next(_seq)
    return RawBiz(name=f"pytest-runner-{n}", city="Тест-город",
                  website=f"pytest-runner-{n}.example", gate_write=gate_write)


def _candidate() -> RawBiz:
    """Сайта нет, телефон есть — 80 баллов, гейт такую не видит."""
    n = next(_seq)
    return RawBiz(name=f"pytest-runner-{n}", city="Тест-город",
                  phone=f"+38050{5000000 + n}")


async def _lead_names(cards) -> set[str]:
    async with Session() as s:
        rows = await s.scalars(
            select(Lead.name).where(Lead.name.in_([c.name for c in cards]))
        )
    return set(rows)


async def _run(monkeypatch, worker_id, cards, verdict: GateResult):
    async def _admin():
        async with Session() as s:
            return await s.get(Worker, worker_id)

    async def _gate(gray, **kw):
        return verdict

    async def _no_probe(urls):
        return {}

    monkeypatch.setattr(config, "SCOUT_DAILY_RAW_LIMIT", 10_000)
    monkeypatch.setattr(runner.site_probe, "probe_many", _no_probe)
    monkeypatch.setattr(runner, "ensure_admin_worker", _admin)
    monkeypatch.setattr(runner.gate, "run", _gate)
    bot = FakeBot()
    await runner._run_cards(
        bot, chat_id=1, header="pytest", cards=cards, country="Украина",
        niche="Стоматология", city="Тест-город", batch_id="pytest-runner",
    )
    return bot


async def test_cards_dropped_by_the_gate_do_not_reach_the_base(monkeypatch,
                                                               worker_id):
    dropped = _gray(gate_write=False)
    kept = _gray(gate_write=True)
    untouched = _gray()                 # до неё гейт не дошёл
    candidate = _candidate()            # спорной не была вовсе
    cards = [dropped, kept, untouched, candidate]

    bot = await _run(monkeypatch, worker_id, cards,
                     GateResult(True, kept=1, dropped=1, unseen=1))

    assert await _lead_names(cards) == {kept.name, untouched.name,
                                        candidate.name}
    digest, = bot.sent
    assert "Спорных: 3 (75%) — гейт оставил 1, отсеял 1, не смотрел 1" in digest
    assert "кандидатов 1, сырых 2" in digest


async def test_half_failed_gate_keeps_its_decisions_and_says_so(monkeypatch,
                                                                worker_id):
    dropped = _gray(gate_write=False)
    lost = _gray()  # чанк с ней упал, карточка едет на модерацию сырой
    cards = [dropped, lost]

    bot = await _run(monkeypatch, worker_id, cards,
                     GateResult(False, kept=0, dropped=1, unseen=1,
                                reason="чанк 2: модель недоступна"))

    assert await _lead_names(cards) == {lost.name}
    digest, = bot.sent
    assert "чанк 2: модель недоступна" in digest
    assert "успел оставить 0, отсеять 1, не видел 1" in digest
