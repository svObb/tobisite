"""Форма заявки доходит до админ-чата.

До Worker (подэтап 5.5) — заглушка: наличие формы, action=/api/lead, method=post
и honeypot-поля. После — реальный POST с заголовком X-Tobisite-Test: 1, который
возвращает {"ok": true, "test": true} и в чат ничего не пишет (§4).

Сейчас проверка статическая и сети не касается. Она отвечает на вопрос
«форма вообще есть и указывает туда, куда надо», и не отвечает на вопрос
«заявка дошла». Второй вопрос закрывает CI-проверка после деплоя Worker:
POST на https://<slug>.tobisitepreview.com/api/lead с заголовком
X-Tobisite-Test: 1 и ожиданием {"ok": true, "test": true}. Когда она появится,
её место — здесь же, отдельной функцией, а эту оставить как быструю.
"""
from __future__ import annotations

import re

ACTION = "/api/lead"
METHOD = "post"
HONEYPOT = "company_website"
REQUIRED_FIELDS = ("name", "phone")

FORM = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.S)


def check(html: str) -> list[str]:
    forms = FORM.findall(html)
    if not forms:
        return [f"на странице нет формы с action={ACTION}"]

    problems = []
    for attrs, body in forms:
        if f'action="{ACTION}"' not in attrs:
            problems.append(f"форма без action=\"{ACTION}\": {attrs.strip()[:60]!r}")
        if f'method="{METHOD}"' not in attrs.lower():
            problems.append(f"форма без method=\"{METHOD}\": {attrs.strip()[:60]!r}")
        if f'name="{HONEYPOT}"' not in body:
            problems.append(f"в форме нет honeypot-поля {HONEYPOT!r}")
        problems += [f"в форме нет поля {field!r}" for field in REQUIRED_FIELDS
                     if f'name="{field}"' not in body]
    return problems
