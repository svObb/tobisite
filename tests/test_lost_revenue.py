"""Формула потерянной выручки (9.35): только цифры клиента, ни одной нашей.

Пустая строка здесь — не сбой, а единственный честный ответ: не хватает
множителя, значит выручку считать нечем. Проверяется и это, и то, что в
готовой строке нет ни одного числа, которого клиент не называл.
"""
import re
from types import SimpleNamespace

import pytest

import email_gen
from handlers_admin import numbers_cmd
from models import Lead, Session
from test_reject_reason import FakeMsg, FakeState
from test_suppression_log import cmd

CLIENT = {"missed_per_month": 20, "conversion_pct": 25,
          "avg_ticket": "800", "currency": "UAH"}


def lead_with(**changes):
    # опечатка в имени ключа иначе не ломает тест, а тихо оставляет цифру
    # прежней — и тест проверяет не то, что написано в его названии
    assert set(changes) <= set(CLIENT)
    numbers = CLIENT | changes
    return SimpleNamespace(enrichment={email_gen.LOSS_KEY: numbers})


def line(lang="uk", **changes) -> str:
    return email_gen.lost_revenue(lead_with(**changes), lang)


# --- арифметика ---------------------------------------------------------------

def test_formula_multiplies_the_clients_own_numbers():
    assert line() == ("Ваші ж цифри: 20 пропущених звернень на місяць, "
                      "конверсія 25%, середній чек 800 UAH. "
                      "Разом 4000 UAH на місяць.")


def test_line_has_no_number_the_client_did_not_name():
    # 4000 — произведение трёх его чисел; больше в строке взяться нечему
    assert set(re.findall(r"\d+", line())) == {"20", "25", "800", "4000"}


def test_loss_is_rounded_down():
    # 7 × 33% × 55.55 = 128.32; в письме цифра не имеет права быть больше
    assert "128 EUR" in line(missed_per_month=7, conversion_pct=33,
                             avg_ticket="55.55", currency="EUR")


def test_kopecks_of_the_ticket_stay_as_they_are():
    assert "55.55 EUR" in line(missed_per_month=7, conversion_pct=33,
                               avg_ticket="55.55", currency="EUR")


def test_english_letter_gets_the_english_line():
    assert line("en").startswith("Your own numbers: 20 missed enquiries")


# --- нечего считать -----------------------------------------------------------

@pytest.mark.parametrize("changes", [
    {"missed_per_month": None}, {"conversion_pct": None}, {"avg_ticket": None},
    {"currency": ""}, {"missed_per_month": 0}, {"conversion_pct": 0},
    {"conversion_pct": 101}, {"avg_ticket": "0"}, {"avg_ticket": "-800"},
    {"missed_per_month": "много"}, {"avg_ticket": "восемьсот"},
])
def test_without_a_multiplier_there_is_no_line(changes):
    assert line(**changes) == ""


def test_rounding_to_zero_leaves_no_line():
    # 1 × 1% × 50 = 0.5 → вниз это ноль, а «0 UAH на місяць» писать незачем
    assert line(missed_per_month=1, conversion_pct=1, avg_ticket="50") == ""


def test_lead_without_numbers_has_no_line():
    assert email_gen.lost_revenue(SimpleNamespace(enrichment=None), "uk") == ""
    assert email_gen.lost_revenue(SimpleNamespace(enrichment={}), "uk") == ""


def test_language_outside_the_tables_has_no_line():
    assert line("de") == ""


# --- письмо 3 -----------------------------------------------------------------

async def test_letter_3_carries_the_line_as_a_slot(gap_lead):
    lead = await gap_lead(enrichment={email_gen.LOSS_KEY: CLIENT})

    third = email_gen.build_email_3(lead)

    assert third.ok and third.slots["loss"] == line()
    assert third.slots["loss"] in third.body
    # цифра-пруф не отменяет break-up: срок хранения черновика остался
    assert str(email_gen.DRAFT_HOLD_DAYS) in third.body


async def test_letter_3_without_numbers_says_nothing_extra(gap_lead):
    lead = await gap_lead()

    third = email_gen.build_email_3(lead)

    assert third.ok and third.slots["loss"] == ""
    assert "\n\n\n" not in third.body  # пустой слот выпадает целиком


async def test_letter_2_never_counts_money(gap_lead):
    lead = await gap_lead(enrichment={email_gen.LOSS_KEY: CLIENT})
    second = email_gen.build_email_2(lead, "klinika.tobisitepreview.com")
    assert second.slots["loss"] == "" and "4000" not in second.body


# --- ввод цифр ----------------------------------------------------------------

async def _numbers_of(lead_id) -> dict:
    async with Session() as s:
        lead = await s.get(Lead, lead_id)
    return (lead.enrichment or {}).get(email_gen.LOSS_KEY) or {}


async def test_numbers_command_writes_the_clients_numbers(gap_lead):
    lead = await gap_lead()
    msg = FakeMsg()

    await numbers_cmd(msg, FakeState(), cmd(f"{lead.id} 20 25 800 UAH"))

    assert await _numbers_of(lead.id) == CLIENT
    assert "4000 UAH" in msg.sent[0]  # админ видит строку до отправки


async def test_ticket_without_a_currency_is_in_dollars(gap_lead):
    lead = await gap_lead()
    await numbers_cmd(FakeMsg(), FakeState(), cmd(f"{lead.id} 10 50 120"))
    assert (await _numbers_of(lead.id))["currency"] == "USD"


@pytest.mark.parametrize("args", ["", "42 20 25", "42 20 25 800 UAH лишнее"])
async def test_wrong_argument_count_shows_the_format(args):
    msg = FakeMsg()
    await numbers_cmd(msg, FakeState(), cmd(args))
    assert "Формат:" in msg.sent[0]


@pytest.mark.parametrize("tail", ["0 25 800", "20 0 800", "20 101 800",
                                  "20 25 0", "20 25 много"])
async def test_impossible_numbers_are_refused(gap_lead, tail):
    lead = await gap_lead()
    msg = FakeMsg()

    await numbers_cmd(msg, FakeState(), cmd(f"{lead.id} {tail}"))

    assert "конверсия — 1–100" in msg.sent[0]
    assert await _numbers_of(lead.id) == {}


async def test_numbers_for_an_unknown_lead():
    msg = FakeMsg()
    await numbers_cmd(msg, FakeState(), cmd("999999999 20 25 800"))
    assert "не найден" in msg.sent[0]
