"""Первая строка письма и тема — таблицей, без единого вызова модели (Д12 §3).

Вариант выбирается по hash(lead_id) % len: одинаковый лид всегда получает
одинаковую строку, а два соседних лида — разные. Подставляется только то, что
работник видел своими глазами: {v} из gap_value, {v1}/{v2} — его половины.
Таблица не может соврать, и это её главное свойство.
"""
# Первый вариант каждой строки — дословно из таблицы Д12 §3, второй и третий
# написаны в том же регистре (Д12 даёт по одному, план требует три).
FIRST_LINES = {
    "slow": {
        "uk": [
            "відкрив з телефону — головна вантажилась {v} секунд",
            "зайшов з телефону: головна відкривалась {v} секунд",
            "перевірив з мобільного — головна вантажилась {v} секунд",
        ],
        "en": [
            "opened it on my phone, the homepage took {v} seconds to load",
            "I checked it from my phone, the homepage needed {v} seconds",
            "on my phone your homepage took {v} seconds before anything showed",
        ],
    },
    "no_mobile": {
        "uk": [
            "сторінку доводиться розтягувати пальцями, текст не вміщається в екран",
            "з телефону текст виходить за екран, доводиться гортати вбік",
            "на екрані телефону сторінка не вміщається, читати можна лише зі збільшенням",
        ],
        "en": [
            "I had to pinch-zoom to read anything, the text doesn't fit the screen",
            "on a phone the text runs off the screen and I had to scroll sideways",
            "your page doesn't fit a phone screen, I had to zoom in to read it",
        ],
    },
    "no_booking": {
        "uk": [
            "записатись можна тільки дзвінком, форми на сайті немає",
            "на сайті немає форми запису — тільки телефон",
            "щоб записатись, треба дзвонити: форми на сайті я не знайшов",
        ],
        "en": [
            "the only way to book is by phone, there's no form on the site",
            "there's no booking form on the site, only a phone number",
            "to book I'd have to call, I couldn't find a form on the site",
        ],
    },
    "form_broken": {
        "uk": [
            "заповнив форму, натиснув «Надіслати» — сторінка перезавантажилась "
            "і нічого не сталось",
            "надіслав форму з сайту — сторінка перезавантажилась, підтвердження не було",
            "заповнив форму, після «Надіслати» не сталось нічого",
        ],
        "en": [
            "I filled in your contact form, hit send, and the page just reloaded",
            "I sent your contact form and got no confirmation, just a reload",
            "I filled in the form on your site and nothing happened after send",
        ],
    },
    "no_prices": {
        "uk": [
            "шукав ціни на {v} — на сайті їх немає",
            "не знайшов на сайті цін на {v}",
            "шукав, скільки коштує {v} — на сайті цього немає",
        ],
        "en": [
            "I looked for {v} pricing and couldn't find it anywhere",
            "I couldn't find what {v} costs anywhere on the site",
            "your site doesn't list prices for {v}",
        ],
    },
    "stale": {
        "uk": [
            "на головній досі висить «{v}»",
            "на головній сторінці досі стоїть «{v}»",
            "на сайті й досі написано «{v}»",
        ],
        "en": [
            'your homepage still says "{v}"',
            'your homepage still carries "{v}"',
            'the site still shows "{v}"',
        ],
    },
    "no_site": {
        "uk": [
            "шукав ваш сайт у Google і в Google Maps — знайшов тільки {v}",
            "шукав ваш сайт у Google — знайшов тільки {v}",
            "у пошуку і в Google Maps знайшов тільки {v}, свого сайту немає",
        ],
        "en": [
            "I looked for your site on Google and Maps and only found {v}",
            "I searched for your site and found only {v}",
            "on Google and Google Maps I only found {v}, no site of your own",
        ],
    },
    "no_https": {
        "uk": [
            "браузер показав «{v}» замість сайту",
            "замість сайту браузер показав «{v}»",
            "браузер попередив: «{v}»",
        ],
        "en": [
            'my browser showed "{v}" instead of your site',
            'my browser showed "{v}" instead of opening the site',
            'opening your site gives "{v}" in the browser',
        ],
    },
    "contact_mismatch": {
        "uk": [
            "на сайті телефон {v1}, у Google Maps {v2}",
            "на сайті вказано {v1}, а в Google Maps — {v2}",
            "номер на сайті {v1} не збігається з {v2} у Google Maps",
        ],
        "en": [
            "your site lists {v1}, Google Maps lists {v2}",
            "your site shows {v1} while Google Maps shows {v2}",
            "the number on your site is {v1}, on Google Maps it's {v2}",
        ],
    },
}

# Тема письма: 3–6 слов, без спам-слов и без обещаний. Она не должна выдавать
# содержание — только повод открыть.
SUBJECTS = {
    "uk": [
        "Коротко про сайт {name}",
        "Кілька слів про ваш сайт",
        "{name}: що я побачив",
    ],
    "en": [
        "a quick note about your site",
        "one thing on your site",
        "{name}: what I noticed",
    ],
}

LANGS = ("uk", "en")
# Языки писем — только uk и en (решение 6 этапа). Значения приходят из
# config.LANGUAGES, где они записаны по-русски; всё остальное — не наш язык,
# и письмо для такого лида собирается руками.
_LANG_ALIASES = {
    "украинский": "uk", "українська": "uk", "ukrainian": "uk", "ua": "uk",
    "uk": "uk",
    "английский": "en", "англійська": "en", "angliyska": "en",
    "english": "en", "en": "en",
}


def lang_of(lead) -> str | None:
    """Язык письма для лида. None — фраз на этот язык нет, письмо только руками."""
    return _LANG_ALIASES.get((lead.language or "").strip().lower())


def variant(lead_id: int, options: list) -> str:
    return options[hash(lead_id) % len(options)]


def first_line(lead) -> str:
    """Первая строка письма из наблюдения. Пустая строка — фразы не нашлось."""
    lang = lang_of(lead)
    options = FIRST_LINES.get(lead.gap_type or "", {}).get(lang or "")
    if not options:
        return ""
    return _fill(variant(lead.id, options), lead.gap_value)


def subject(lead) -> str:
    lang = lang_of(lead)
    if lang not in SUBJECTS:
        return ""
    return variant(lead.id, SUBJECTS[lang]).format(name=(lead.name or "").strip())


def _fill(template: str, value: str | None) -> str:
    value = (value or "").strip()
    if "{v1}" in template:
        first, _, second = value.partition(",")
        return template.format(v1=first.strip(), v2=second.strip())
    if "{v}" in template:
        return template.format(v=value)
    return template
