"""cost_ledger: запись расходов, месячная сумма, кэп и алерты (раздел 20)."""
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

import config
import costs


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


async def test_log_cost_adds_up():
    before = await costs.month_spent()
    after = await costs.log_cost(
        op="scout", cost_usd="1.25", model="claude-haiku-4-5",
        input_tokens=1000, output_tokens=50, cache_read_tokens=4000,
        batch_id="pytest-sum",
    )
    assert after == before + Decimal("1.25")
    assert await costs.month_spent() == after


async def test_cap_stops_and_alerts(monkeypatch):
    spent = await costs.month_spent()
    monkeypatch.setattr(config, "AI_MONTHLY_CAP_USD", float(spent) + 5)
    assert not await costs.cap_reached()

    bot = FakeBot()
    await costs.log_cost(op="other", cost_usd="5.5", batch_id="pytest-cap", bot=bot)

    assert await costs.cap_reached()
    assert any("исчерпан" in text for _, text in bot.sent)
    assert all(chat == config.ADMIN_TG_ID for chat, _ in bot.sent)


async def test_cap_zero_disables(monkeypatch):
    monkeypatch.setattr(config, "AI_MONTHLY_CAP_USD", 0.0)
    assert not await costs.cap_reached()


async def test_unknown_op_rejected_by_check():
    with pytest.raises(IntegrityError):
        await costs.log_cost(op="взлом", cost_usd="0", batch_id="pytest-bad")
