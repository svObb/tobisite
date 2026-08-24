"""Смоук-рендер: layout + три секции на всех пресетах, на фикстуре без движка.

Секции здесь заданы руками, а не подобраны рецептом: смоук проверяет, что
шаблоны и токены сходятся, и остаётся способом посмотреть вёрстку в отрыве от
подбора. Настоящий конвейер — engine.render.render(profile).

    python site_factory/tests/smoke_render.py

Результат — site_factory/build/smoke/<preset>.html. Открывать после того, как
собран бандл: python tools/fetch_fonts.py && python tools/build_css.py
"""
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))  # запуск скриптом: репозиторий в sys.path

from site_factory.engine.render import environment, resolve_preset  # noqa: E402

PRESETS = ROOT / "tokens" / "presets.yaml"
OUT_DIR = ROOT / "build" / "smoke"


# Фикстура. Компания выдуманная целиком, телефон — несуществующий, из одних
# нулей. Никаких показателей (стаж, отзывы, рейтинг) здесь нет и быть не должно:
# они приходят только из Google Maps через белый список профиля.
FACTS = {
    "business_type": "LegalService",
    "name": "Адвокатське бюро «Фікстура»",
    "url": "https://example.com",
    "telephone": "+380 00 000 00 00",
    "email": "office@example.com",
    "address": {
        "street": "вул. Тестова, 1",
        "locality": "Київ",
        "country": "UA",
    },
}

SITE = {
    "lang": "uk",
    "title": "Адвокатське бюро «Фікстура» — Київ",
    "description": "Фікстура для смоук-рендера site_factory.",
    "assets_base": "/assets",
    "ui": {"skip_to_content": "Перейти до вмісту"},
}

SECTIONS = [
    {
        "id": "hero",
        "role": "hero",
        "template": "sections/hero/hero_type_only.html.j2",
        "images": {},
        "slots": {
            "eyebrow": "Адвокатське бюро",
            "headline": "Юридична підтримка бізнесу в Києві",
            "lede": "Супровід договорів, спорів і перевірок — від першої консультації "
                    "до рішення суду.",
            "call_label": "Зателефонувати",
            "phone_href": "tel:+380000000000",
            "secondary_label": "Послуги",
            "secondary_target": "services",
        },
    },
    {
        "id": "services",
        "role": "services",
        "template": "sections/services/svc_cards_3.html.j2",
        "images": {},
        "slots": {
            "section_title": "Напрями роботи",
            "services": [
                {
                    "name": "Договірне право",
                    "blurb": "Складаємо та перевіряємо договори, знімаємо ризики до підпису.",
                },
                {
                    "name": "Судові спори",
                    "blurb": "Ведемо справу в усіх інстанціях і готуємо позицію заздалегідь.",
                },
                {
                    "name": "Перевірки та штрафи",
                    "blurb": "Супровід під час перевірок та оскарження рішень "
                             "контролюючих органів.",
                },
            ],
        },
    },
    {
        "id": "footer",
        "role": "footer",
        "template": "sections/footer/footer_nap.html.j2",
        "images": {},
        "slots": {
            "business_name": FACTS["name"],
            "address": "вул. Тестова, 1, Київ",
            "contacts_title": "Контакти",
            "phone": FACTS["telephone"],
            "phone_href": "tel:+380000000000",
            "email": FACTS["email"],
            "hours_title": "Години прийому",
            "hours": ["Пн–Пт: 09:00–18:00", "Сб–Нд: за домовленістю"],
            "legal_line": "Чернетка сайту, підготовлена для ознайомлення.",
        },
    },
]


def main():
    tokens = yaml.safe_load(PRESETS.read_text(encoding="utf-8"))
    layout = environment(ROOT).get_template("base/layout.html.j2")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for preset in tokens["presets"]:
        html = layout.render(
            preset=resolve_preset(preset, tokens),
            site=SITE,
            facts=FACTS,
            sections=SECTIONS,
        )
        if "{{" in html or "{%" in html:
            raise SystemExit(f"{preset['id']}: в выдаче остались неразрешённые теги Jinja")
        path = OUT_DIR / f"{preset['id']}.html"
        path.write_text(html, encoding="utf-8")
        print(f"{path.relative_to(ROOT)}: {len(html.encode('utf-8')) / 1024:.1f} КБ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
