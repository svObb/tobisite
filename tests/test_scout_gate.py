"""ИИ-гейт спорных карточек скаута (15.15, 15.18–15.20).

Сети нет: клиента подменяет фикстура gate_model. Проверяется и обратное —
без ключа и при исчерпанном кэпе гейт не зовёт модель вообще, а карточки
уезжают на модерацию такими же, какими пришли.
"""
import json
from types import SimpleNamespace

import anthropic
import pytest
from sqlalchemy import select

import config
import costs
from models import CostLedger, Session
from scout import gate
from scout.scoring import Split, split
from scout.types import RawBiz

BATCH = "pytest-gate"


def _card(name="Кав'ярня", **kw) -> RawBiz:
    kw.setdefault("city", "Вінниця")
    kw.setdefault("score", 45)
    kw.setdefault("verdict", "review")
    kw.setdefault("reasons", ["без HTTPS", "есть телефон"])
    return RawBiz(name=name, **kw)


def _answer(*rows) -> str:
    return json.dumps({"cards": [
        {"i": i, "write": write, "hook": hook}
        for i, (write, hook) in enumerate(rows, 1)
    ]}, ensure_ascii=False)


class ScriptedMessages:
    """Ответы по очереди на каждый чанк; Exception — падение этого вызова."""

    def __init__(self, script, stop_reason="end_turn"):
        self.script = list(script)
        self.stop_reason = stop_reason
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        step = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(step, Exception):
            raise step
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=step)],
            stop_reason=self.stop_reason,
            usage=SimpleNamespace(input_tokens=120, output_tokens=60,
                                  cache_read_input_tokens=1800,
                                  cache_creation_input_tokens=0),
        )


def _scripted(monkeypatch, gate_model, script, stop_reason="end_turn"):
    fake = gate_model("{}")
    messages = ScriptedMessages(script, stop_reason)
    monkeypatch.setattr(fake, "messages", messages)
    return messages


async def _run(cards, batch=BATCH, **kw):
    return await gate.run(cards, batch_id=batch, niche="Кафе/ресторан",
                          city="Вінниця", **kw)


async def _ledger_rows(op: str, batch: str) -> list[CostLedger]:
    async with Session() as s:
        return list(await s.scalars(
            select(CostLedger).where(CostLedger.batch_id == batch,
                                     CostLedger.op == op)
        ))


# --- 15.15: три исхода --------------------------------------------------------

def test_split_sorts_cards_by_verdict():
    cards = [_card(verdict="candidate"), _card(verdict="review"),
             _card(verdict="reject"), _card(verdict="candidate")]
    parts = split(cards)
    assert (len(parts.candidates), len(parts.gray), len(parts.rejected)) \
        == (2, 1, 1)
    assert parts.total == 4 and parts.gray_share == 0.25


def test_split_of_nothing_has_no_share():
    assert Split().gray_share == 0.0


def test_unscored_card_does_not_slip_through():
    with pytest.raises(KeyError):
        split([_card(verdict="")])


# --- 15.19: промпт ------------------------------------------------------------

def test_prompt_prefix_is_long_enough_to_cache():
    # короче минимума модели префикс не кэшируется молча — см. gate.CACHE_MIN_CHARS
    assert len(gate.SYSTEM_PROMPT) >= gate.CACHE_MIN_CHARS


def test_payload_shows_only_card_facts():
    card = _card(phone="+380501234567", website="https://cafe.example/menu",
                 address="вул. Соборна, 1", source_url="https://osm.example/1")
    row, = gate.payload([card])
    assert row == {"i": 1, "name": card.name, "city": "Вінниця", "score": 45,
                   "reasons": ["без HTTPS", "есть телефон"],
                   "site": "cafe.example", "ads": False}


def test_user_prompt_carries_niche_and_cards():
    prompt = gate.user_prompt([_card()], "Кафе/ресторан")
    assert "<niche>Кафе/ресторан</niche>" in prompt
    assert "Кав'ярня" in prompt


async def test_static_part_goes_first_and_marked_cacheable(gate_model):
    fake = gate_model(_answer((True, "Кофейня без сайта.")))
    await _run([_card()])

    system, = fake.messages.calls[0]["system"]
    assert system["text"] == gate.SYSTEM_PROMPT
    assert system["cache_control"] == {"type": "ephemeral"}
    assert fake.messages.calls[0]["model"] == gate.MODEL


# --- 15.18: решения -----------------------------------------------------------

async def test_gate_keeps_and_drops(gate_model):
    keep, drop = _card("Кав'ярня"), _card("McDonald's")
    gate_model(_answer((True, "Кофейня без сайта."), (False, "")))

    result = await _run([keep, drop])

    assert result.ok and (result.kept, result.dropped) == (1, 1)
    assert keep.gate_write is True and keep.gate_hook == "Кофейня без сайта."
    assert drop.gate_write is False


async def test_long_hook_is_cut_not_dropped(gate_model):
    card = _card()
    gate_model(_answer((True, "я" * (gate.HOOK_MAX + 50))))
    await _run([card])
    assert len(card.gate_hook) == gate.HOOK_MAX


async def test_card_the_model_skipped_stays(gate_model):
    first, second = _card("Перша"), _card("Друга")
    gate_model(_answer((False, "")))  # решение только по первой

    result = await _run([first, second])

    assert first.gate_write is False
    assert second.gate_write is None and result.unseen == 1


async def test_answer_not_json_drops_nobody(gate_model):
    card = _card()
    gate_model("конечно, вот мои решения")

    result = await _run([card])

    assert not result.ok and "не JSON" in result.reason
    assert card.gate_write is None


async def test_answer_in_wrong_shape_drops_nobody(gate_model):
    card = _card()
    gate_model(json.dumps({"решения": [{"i": 1, "write": False}]}))

    result = await _run([card])

    assert not result.ok and "не по формату" in result.reason
    assert card.gate_write is None


async def test_api_error_is_not_a_verdict(gate_model, monkeypatch):
    card = _card()
    fake = gate_model(_answer((False, "")))

    async def _boom(**kw):
        raise anthropic.APIError("сломалось", request=None, body=None)

    monkeypatch.setattr(fake.messages, "create", _boom)
    result = await _run([card])

    assert not result.ok and "модель недоступна" in result.reason
    assert card.gate_write is None


async def test_cards_beyond_the_run_limit_are_left_alone(gate_model,
                                                         monkeypatch):
    monkeypatch.setattr(config, "SCOUT_GATE_MAX", 1)
    seen, unseen = _card("Перша"), _card("Друга")
    fake = gate_model(_answer((False, "")))

    result = await _run([seen, unseen])

    assert "Друга" not in fake.messages.calls[0]["messages"][0]["content"]
    assert seen.gate_write is False and unseen.gate_write is None
    assert result.unseen == 1


# --- прогон в несколько чанков ------------------------------------------------

async def test_failed_chunk_does_not_cancel_the_others(monkeypatch, gate_model):
    monkeypatch.setattr(gate, "CHUNK", 2)
    monkeypatch.setattr(config, "SCOUT_GATE_MAX", 6)
    cards = [_card(f"Картка {n}") for n in range(6)]
    messages = _scripted(monkeypatch, gate_model, [
        _answer((True, "перша"), (False, "")),
        anthropic.APIError("сломалось", request=None, body=None),
        _answer((True, "пʼята"), (False, "")),
    ])

    result = await _run(cards)

    assert len(messages.calls) == 3  # сбойный чанк не отменил третий
    assert [c.gate_write for c in cards] == [True, False, None, None, True, False]
    assert not result.ok and "чанк 2" in result.reason
    # цифры честные: решения удавшихся чанков уже применены к карточкам
    assert (result.kept, result.dropped, result.unseen) == (2, 2, 2)


async def test_cap_reached_mid_run_silences_the_rest(monkeypatch, gate_model):
    monkeypatch.setattr(gate, "CHUNK", 2)
    monkeypatch.setattr(config, "SCOUT_GATE_MAX", 6)
    cards = [_card(f"Картка {n}") for n in range(6)]
    messages = _scripted(monkeypatch, gate_model, [_answer((True, ""), (False, ""))])
    checks = []

    async def _reached():
        checks.append(1)
        return len(checks) > 1  # кэп кончился на первом же чанке

    monkeypatch.setattr(costs, "cap_reached", _reached)
    result = await _run(cards)

    assert len(messages.calls) == 1
    assert not result.ok and "кэп" in result.reason
    assert (result.kept, result.dropped, result.unseen) == (1, 1, 4)


async def test_truncated_answer_is_named_as_such(monkeypatch, gate_model):
    card = _card()
    _scripted(monkeypatch, gate_model, [_answer((True, "зацепка"))],
              stop_reason="max_tokens")

    result = await _run([card])

    assert not result.ok and "обрезан" in result.reason
    assert card.gate_write is None


# --- деградация без ключа и по кэпу -------------------------------------------

async def test_without_a_key_the_gate_does_not_call_the_model(monkeypatch,
                                                              gate_model):
    fake = gate_model(_answer((False, "")))
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    card = _card()

    result = await _run([card])

    assert not result.ok and "ANTHROPIC_API_KEY" in result.reason
    assert fake.messages.calls == []
    # карточка едет на модерацию сырой — ровно как до появления гейта
    assert card.gate_write is None and result.unseen == 1


async def test_spent_cap_stops_the_gate(monkeypatch, gate_model):
    fake = gate_model(_answer((False, "")))

    async def _reached():
        return True

    monkeypatch.setattr(costs, "cap_reached", _reached)
    result = await _run([_card()])

    assert not result.ok and "кэп" in result.reason
    assert fake.messages.calls == []


async def test_switch_off_disables_the_gate(monkeypatch, gate_model):
    fake = gate_model(_answer((False, "")))
    monkeypatch.setattr(config, "SCOUT_GATE_MAX", 0)

    result = await _run([_card()])

    assert not result.ok and "SCOUT_GATE_MAX" in result.reason
    assert fake.messages.calls == []


async def test_empty_batch_costs_nothing(gate_model):
    fake = gate_model(_answer((False, "")))
    result = await _run([])
    assert result.ok and fake.messages.calls == []


# --- 15.20: расходы -----------------------------------------------------------

async def test_gate_call_lands_in_the_ledger(gate_model):
    batch = f"{BATCH}-ledger"  # свой прогон: соседние тесты тоже платят
    gate_model(_answer((True, "Кофейня без сайта.")))

    await _run([_card()], batch)

    row, = await _ledger_rows(gate.COST_OP, batch)
    assert row.model == gate.MODEL and row.cost_usd > 0
    assert (row.input_tokens, row.output_tokens) == (120, 60)
    assert row.cache_read_tokens == 1800
    assert row.note == "гейт Кафе/ресторан Вінниця, карточек: 1"
    # своей строкой: бесплатные вызовы скаута считаются отдельно
    assert await _ledger_rows("scout", batch) == []
