"""Письмо 2 (9.5): ссылка на живое превью и два слота инлайн-снимков.

Снимков в слотах пока нет — снимать страницу нечем, и это записано в TODO
модуля. Проверяется то, что уже обязано работать: ссылка ведёт на настоящий
поддомен превью, а не на что попало, и слоты картинок описаны полностью.
"""
import pytest

import email_gen

HOST = "klinika.tobisitepreview.com"


def letter(host=HOST, lead=None):
    return email_gen.build_email_2(lead, host)


# --- ссылка -------------------------------------------------------------------

async def test_link_leads_to_the_published_preview(gap_lead):
    lead = await gap_lead()
    second = letter(lead=lead)
    assert second.ok and HOST in second.body


@pytest.mark.parametrize("host", [
    "", "   ", None, "example.com", "klinika.tobisitepreview.com.evil.ru",
    "tobisitepreview.com",
])
async def test_letter_without_a_real_preview_is_not_built(gap_lead, host):
    lead = await gap_lead()
    second = letter(host, lead)
    # ссылка в никуда в коммерческом письме хуже, чем ненаписанное письмо
    assert second.needs_manual and "превью не опубликовано" in second.reason


async def test_subdomain_with_a_dot_is_refused(gap_lead):
    lead = await gap_lead()
    second = letter("pravo.i.dilo.tobisitepreview.com", lead)
    # wildcard-сертификат покрывает одну метку: у клиента откроется
    # предупреждение браузера, а не черновик
    assert second.needs_manual and "HTTPS не сработает" in second.reason


@pytest.mark.parametrize("host", [
    f"https://{HOST}", f"HTTPS://{HOST.upper()}", f"{HOST}/", f"  {HOST}  ",
])
async def test_host_is_understood_as_written(gap_lead, host):
    lead = await gap_lead()
    second = letter(host, lead)
    assert second.ok and f"чернетка: {HOST}" in second.body


# --- слоты снимков ------------------------------------------------------------

async def test_two_shots_for_the_two_screens(gap_lead):
    lead = await gap_lead()
    shots = letter(lead=lead).shots

    assert [s["cid"] for s in shots] == ["preview-mobile", "preview-desktop"]
    assert [s["view"] for s in shots] == ["mobile", "desktop"]
    assert {s["url"] for s in shots} == {f"https://{HOST}"}
    # файла ни в одном: снимать страницу пока нечем, слот честно пустой
    assert [s["file"] for s in shots] == ["", ""]


async def test_shots_of_one_letter_do_not_touch_another(gap_lead):
    lead = await gap_lead()
    letter(lead=lead).shots[0]["file"] = "не трогать шаблон"
    assert all(shot.get("file") is None for shot in email_gen.SHOTS_2)


async def test_letter_3_has_no_shots(gap_lead):
    lead = await gap_lead()
    assert email_gen.build_email_3(lead).shots == []
