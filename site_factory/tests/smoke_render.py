"""Смоук-рендер: три страницы на каждом пресете, на фикстуре без движка.

Секции здесь заданы руками, а не подобраны рецептом: смоук проверяет, что
шаблоны и токены сходятся, и остаётся способом посмотреть вёрстку в отрыве от
подбора. Настоящий конвейер — engine.render.render(profile).

    python site_factory/tests/smoke_render.py

Три композиции на пресет закрывают всю библиотеку, кроме hero_photo_left:
    <preset>.html        лид со всем: логотип, фон-фото, товары с фото, галерея
    <preset>-light.html  лид без единой картинки
    <preset>-map.html    лид с картой и карточками услуг

Результат — site_factory/build/smoke/. Открывать через превью-воркер (страница
тянет /assets/bundle.css и /assets/*.js), собрав бандл:
python tools/fetch_fonts.py && python tools/build_css.py
"""
import base64
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT.parent))  # запуск скриптом: репозиторий в sys.path

from site_factory.engine.render import (ASSETS_BASE, BASE_SCRIPTS,  # noqa: E402
                                        environment, resolve_preset)

PRESETS = ROOT / "tokens" / "presets.yaml"
OUT_DIR = ROOT / "build" / "smoke"


def placeholder(width: int, height: int, tone: str, stroke: str) -> dict:
    """Заглушка вместо фотографии: геометрия, по которой видно кадрирование.

    Data-URI, чтобы смоук не зависел от стейджинга картинок лида. Рисунок
    намеренно ничего не изображает — на месте настоящего снимка стоит сетка.
    """
    svg = (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' "
        f"height='{height}' viewBox='0 0 {width} {height}'>"
        f"<rect width='{width}' height='{height}' fill='{tone}'/>"
        f"<path d='M0 0 L{width} {height} M{width} 0 L0 {height}' "
        f"stroke='{stroke}' stroke-width='{max(width, height) // 220 + 1}'/>"
        f"<circle cx='{width // 2}' cy='{height // 2}' r='{min(width, height) // 5}' "
        f"fill='none' stroke='{stroke}' stroke-width='{max(width, height) // 220 + 1}'/>"
        "</svg>"
    )
    src = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return {"src": f"data:image/svg+xml;base64,{src}",
            "width": width, "height": height}


# Тёмный фон первого экрана: поверх него лежит .scrim и текст бумагой.
HERO_BG = placeholder(2000, 1125, "#4a4640", "#6f6a63")
LOGO = placeholder(320, 96, "#e6e3de", "#8f8a82")
PORTRAIT = placeholder(1600, 2000, "#dbd7d1", "#a9a49c")
MAP = placeholder(1600, 1200, "#dedad3", "#a9a49c")
PHOTOS = {name: placeholder(1200, 900, "#dbd7d1", "#a9a49c")
          for name in ("photo-2", "photo-3", "photo-4")}
GOODS = [placeholder(800, 800, "#e0ddd7", "#a9a49c") for _ in range(3)]

# Фикстура. Компания выдуманная целиком, телефон — несуществующий, из одних
# нулей. Цифры фикстуры (рейтинг, отзывы, цены) это входные данные, а не текст
# сайта: на страницу они попадают только через белый список fact-слотов, тем же
# путём, что и телефон. Ни одного показателя в free-заготовках здесь нет.
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

PHONE_HREF = "tel:+380000000000"
ADDRESS = "вул. Тестова, 1, Київ"
HOURS = ["Пн–Пт: 09:00–18:00", "Сб–Нд: за домовленістю"]

SERVICES = [
    {"name": "Договірне право",
     "blurb": "Складаємо та перевіряємо договори, знімаємо ризики до підпису."},
    {"name": "Судові спори",
     "blurb": "Ведемо справу в усіх інстанціях і готуємо позицію заздалегідь."},
    {"name": "Перевірки та штрафи",
     "blurb": "Супровід під час перевірок та оскарження рішень контролюючих органів."},
]

GOODS_ROWS = [
    {"name": "Абонемент на супровід", "price": "від 6 000 грн",
     "image": GOODS[0]},
    {"name": "Перевірка договору", "price": "1 800 грн", "image": GOODS[1]},
    {"name": "Реєстрація ТОВ", "price": "4 500 грн", "image": GOODS[2]},
]

HOURS_ROWS = [{"day": "Пн–Пт", "time": "09:00–18:00"},
              {"day": "Сб–Нд", "time": "за домовленістю"}]

CONTACTS = {
    "contacts_title": "Контакти",
    "address_label": "Адреса",
    "address": ADDRESS,
    "phone": FACTS["telephone"],
    "phone_href": PHONE_HREF,
    "email": FACTS["email"],
}

ABOUT = {
    "section_title": "Що варто знати до дзвінка",
    "about_text": "Тут зібрано те, з чим до нас звертаються найчастіше: напрями "
                  "роботи, години прийому та адреса офісу. Якщо ваше питання "
                  "ширше — зателефонуйте або опишіть ситуацію у формі.",
    "company_label": "Компанія",
    "business_name": FACTS["name"],
    "address_label": "Адреса",
    "address": ADDRESS,
}

FORM = {
    "section_title": "Залишити звернення",
    "lede": "Опишіть питання — зателефонуємо або напишемо у відповідь.",
    "phone": FACTS["telephone"],
    "phone_href": PHONE_HREF,
    "honeypot_label": "Не заповнюйте це поле",
    "name_label": "Імʼя",
    "phone_label": "Телефон",
    "message_label": "Коротко про питання",
    "submit_label": "Надіслати",
    "privacy_note": "Контакти з форми використовуємо лише для відповіді на звернення.",
}

FOOTER = {
    "business_name": FACTS["name"],
    "address": ADDRESS,
    "contacts_title": "Контакти",
    "phone": FACTS["telephone"],
    "phone_href": PHONE_HREF,
    "email": FACTS["email"],
    "hours_title": "Години прийому",
    "hours": HOURS,
    "legal_line": "Чернетка сайту, підготовлена для ознайомлення.",
}

PROOF = {
    "section_title": "Показники профілю",
    "stats": [{"value": "4,8", "label": "Оцінка в Google"},
              {"value": "34", "label": "Відгуків у Google"}],
    "source_note": "Дані з профілю Google Business.",
}


def section(role, template, slots, images=None):
    return {"id": role, "role": role, "template": template,
            "images": images or {}, "slots": slots}


HEADER_LOGO = section("header", "sections/header/header_logo.html.j2",
                      {"business_name": FACTS["name"],
                       "phone": FACTS["telephone"], "phone_href": PHONE_HREF},
                      {"logo": LOGO})
HEADER_WORDMARK = section("header", "sections/header/header_wordmark.html.j2",
                          {"business_name": FACTS["name"],
                           "phone": FACTS["telephone"],
                           "phone_href": PHONE_HREF})
INFO = section("info", "sections/info/info_hours_card.html.j2",
               dict(CONTACTS, section_title="Коли і де нас знайти",
                    hours=HOURS_ROWS))
CTA = section("cta", "sections/cta/cta_form_short.html.j2", FORM)
FOOTER_SECTION = section("footer", "sections/footer/footer_nap.html.j2", FOOTER)
PROOF_SECTION = section("proof", "sections/proof/proof_stats_bar.html.j2", PROOF)
ABOUT_SECTION = section("about", "sections/about/about_note.html.j2", ABOUT)

PAGES = {
    "": [
        HEADER_LOGO,
        section("hero", "sections/hero/hero_bg_photo.html.j2",
                {"eyebrow": "Адвокатське бюро",
                 "headline": "Юридична підтримка бізнесу в Києві",
                 "lede": "Супровід договорів, спорів і перевірок — від першої "
                         "консультації до рішення суду.",
                 "call_label": "Зателефонувати", "phone_href": PHONE_HREF,
                 "secondary_label": "Послуги", "secondary_target": "services"},
                {"hero_bg": HERO_BG}),
        section("products", "sections/products/products_grid.html.j2",
                {"section_title": "Послуги за прайсом", "products": GOODS_ROWS}),
        section("services", "sections/services/svc_two_col_rule.html.j2",
                {"section_title": "Напрями роботи", "services": SERVICES}),
        section("gallery", "sections/gallery/gallery_strip.html.j2", {}, PHOTOS),
        PROOF_SECTION,
        ABOUT_SECTION,
        INFO,
        CTA,
        FOOTER_SECTION,
    ],
    "-light": [
        HEADER_WORDMARK,
        section("hero", "sections/hero/hero_type_only.html.j2",
                {"eyebrow": "Адвокатське бюро",
                 "headline": "Юридична підтримка бізнесу в Києві",
                 "lede": "Супровід договорів, спорів і перевірок — від першої "
                         "консультації до рішення суду.",
                 "call_label": "Зателефонувати", "phone_href": PHONE_HREF,
                 "secondary_label": "Послуги", "secondary_target": "services"}),
        section("products", "sections/products/products_list.html.j2",
                {"section_title": "Послуги за прайсом",
                 "products": [{"name": row["name"], "price": row["price"]}
                              for row in GOODS_ROWS] +
                             [{"name": "Консультація", "price": None}]}),
        section("services", "sections/services/svc_list_icons.html.j2",
                {"section_title": "Напрями роботи", "services": SERVICES}),
        INFO,
        CTA,
        FOOTER_SECTION,
    ],
    "-map": [
        HEADER_LOGO,
        section("hero", "sections/hero/hero_split_map.html.j2",
                {"eyebrow": "Адвокатське бюро",
                 "headline": "Офіс у центрі Києва",
                 "lede": "Приймаємо за попереднім записом — зателефонуйте, і ми "
                         "домовимось про час.",
                 "address_label": "Адреса", "address": ADDRESS,
                 "hours_label": "Години", "hours": "Пн–Пт: 09:00–18:00",
                 "call_label": "Зателефонувати", "phone_href": PHONE_HREF,
                 "map_alt": "Карта з розташуванням офісу"},
                {"map": MAP}),
        section("services", "sections/services/svc_cards_3.html.j2",
                {"section_title": "Напрями роботи", "services": SERVICES}),
        PROOF_SECTION,
        ABOUT_SECTION,
        CTA,
        FOOTER_SECTION,
    ],
}


def site_for(sections: list) -> dict:
    """site.scripts — готовые адреса: их подставляет base/head.j2 как есть."""
    names = list(BASE_SCRIPTS)
    if any(s["template"].endswith("hero_bg_photo.html.j2") for s in sections):
        names.append("parallax")
    return {
        "lang": "uk",
        "title": "Адвокатське бюро «Фікстура» — Київ",
        "description": "Фікстура для смоук-рендера site_factory.",
        "assets_base": ASSETS_BASE,
        "scripts": [f"{ASSETS_BASE}/{name}.js" for name in names],
        "ui": {"skip_to_content": "Перейти до вмісту",
               "nav_label": "Розділи сторінки"},
    }


def main():
    tokens = yaml.safe_load(PRESETS.read_text(encoding="utf-8"))
    layout = environment(ROOT).get_template("base/layout.html.j2")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for preset in tokens["presets"]:
        for suffix, sections in PAGES.items():
            html = layout.render(
                preset=resolve_preset(preset, tokens),
                site=site_for(sections),
                facts=FACTS,
                sections=sections,
            )
            if "{{" in html or "{%" in html:
                raise SystemExit(f"{preset['id']}{suffix}: в выдаче остались "
                                 "неразрешённые теги Jinja")
            path = OUT_DIR / f"{preset['id']}{suffix}.html"
            path.write_text(html, encoding="utf-8")
            print(f"{path.relative_to(ROOT)}: "
                  f"{len(html.encode('utf-8')) / 1024:.1f} КБ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
