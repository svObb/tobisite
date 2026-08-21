"""Автопроверки черновика. Режутся последними: один черновик с чужим
телефоном стоит дороже всей экономии (§6).

run_all прогоняет все пять и возвращает {имя проверки: [проблемы]}, оставляя
только непустые. Пустой словарь — черновик можно публиковать.
"""
from __future__ import annotations

from . import a11y, form_e2e, nap, placeholders, scroll


def run_all(html: str, profile=None, palette: dict | None = None) -> dict:
    results = {
        "placeholders": placeholders.check(html),
        "scroll": scroll.check(html),
        "a11y": a11y.check(html, palette),
        "form_e2e": form_e2e.check(html),
    }
    if profile is not None:
        results["nap"] = nap.check(html, profile)
    return {name: problems for name, problems in results.items() if problems}
