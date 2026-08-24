"""Наблюдение в карточке и на экране подтверждения — чистые функции формы."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import config
import gap_validation as gv
from handlers_worker import fmt_lead, fmt_summary, gap_value_prompt
from models import GAP_TTL_DAYS


def form_data(**kw):
    base = dict(
        name="Клініка Здоров'я", website_url="https://example.com",
        source_url="https://maps.google.com/x", country="Украина", city="Львов",
        language="Украинский", niche="Стоматология", found_via="Google Maps",
        contacts=[], gap_type="slow", gap_value="8", gap_note=None,
    )
    return base | kw


def card(**kw):
    base = dict(
        id=7, name="Клініка Здоров'я", status="new", website_url=None,
        source_url="https://maps.google.com/x", country="Украина", city="Львов",
        language="Украинский", niche="Стоматология", google_rating=None,
        found_via="Google Maps", note=None, gap_type="slow", gap_value="8",
        gap_note=None, gap_captured_at=None, created_at=datetime.now(config.TZ),
        possible_duplicate=False, has_ads=False, cancelled_at=None,
        draft_url=None, admin_note=None, reject_reason=None,
    )
    return SimpleNamespace(**(base | kw))


def test_summary_shows_the_observation():
    assert "Наблюдение: Вантажився довго — 8" in fmt_summary(form_data())
    assert "Наблюдение: не снято" in fmt_summary(form_data(gap_type=None,
                                                           gap_value=None))


def test_summary_warns_about_missing_phone():
    # связку contact_mismatch с телефоном раньше проверить негде: контакты
    # в форме идут после наблюдения
    pair = form_data(gap_type="contact_mismatch",
                     gap_value="+380501112233, +380671114455")
    assert "⚠️ Наблюдение о расхождении телефонов" in fmt_summary(pair)
    with_phone = pair | {"contacts": [
        {"ctype": "phone", "ctype_other": None, "value": "+380501112233"}
    ]}
    assert "⚠️ Наблюдение о расхождении телефонов" not in fmt_summary(with_phone)


def test_card_marks_stale_observation():
    now = datetime.now(config.TZ)
    fresh = fmt_lead(card(gap_captured_at=now - timedelta(days=2)), [])
    assert "⚠️ наблюдению" not in fresh
    old = fmt_lead(card(gap_captured_at=now - timedelta(days=GAP_TTL_DAYS + 5)), [])
    assert f"⚠️ наблюдению {GAP_TTL_DAYS + 5} дней" in old


def test_value_prompt_shows_examples_and_buttons():
    text, markup = gap_value_prompt("slow")
    assert text.splitlines() == [gv.QUESTIONS["slow"]] + gv.EXAMPLES["slow"]
    # у slow клавиатуры вариантов нет — только «Отмена»
    assert len(markup.inline_keyboard) == 1
    _, choice_markup = gap_value_prompt("no_booking")
    labels = [b.text for row in choice_markup.inline_keyboard for b in row]
    assert gv.CHOICE_OPTIONS["no_booking"] == labels[:3]
