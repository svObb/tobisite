"""Первая строка письма и тема — таблицей, без единого вызова модели (Д12 §3).

Вариант выбирается по hash(lead_id) % len: одинаковый лид всегда получает
одинаковую строку, а два соседних лида — разные. Подставляется только то, что
работник видел своими глазами: {v} из gap_value, {v1}/{v2} — его половины.
Таблица не может соврать, и это её главное свойство.

Первая строка собирается из двух частей: зачин с нишей и городом из карточки
плюс хвост наблюдения. Падежи не выводятся правилами — только таблицами
NICHE_FORMS и CITY_LOCATIVE, а что в них не попало, идёт без зачина.
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
            "checked it from my phone, the homepage needed {v} seconds",
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
            "had to pinch-zoom to read anything, the text doesn't fit the screen",
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
            "filled in your contact form, hit send, and the page just reloaded",
            "sent your contact form and got no confirmation, just a reload",
            "filled in the form on your site and nothing happened after send",
        ],
    },
    "no_prices": {
        "uk": [
            "не знайшов на сайті цін на {v}",
            "цін на {v} на сайті немає",
            "дивився, скільки коштує {v} — на сайті цього немає",
        ],
        "en": [
            "opened your site and couldn't find {v} pricing anywhere",
            "couldn't find what {v} costs anywhere on your site",
            "checked your site for {v} pricing and found nothing",
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

# «Сайта нет» — единственный тип, где зачин не приклеивается к хвосту, а
# входит в строку целиком: смотреть на сайте нечего, и вся фраза строится
# вокруг самого поиска. Хвосты выше остаются для ниш вне таблицы форм.
NO_SITE_LINES = {
    "uk": [
        "Шукав {niche} {city} — вашого сайту в Google не знайшов, тільки {v}",
        "Шукав {niche} {city}: сайту в Google і Google Maps немає, "
        "знайшов тільки {v}",
        "Шукав {niche} {city} — замість сайту знайшов тільки {v}",
    ],
    "en": [
        "I was looking for {niche} in {city} and couldn't find a site of "
        "yours, only {v}",
        "I searched for {niche} in {city}, and the only thing that came up "
        "for you was {v}",
        "I was looking for {niche} in {city}, found {v} but no site of your own",
    ],
}

# Ниша в той форме, в какой она стоит в зачине. В карточке она записана
# по-русски (ключи — config.NICHES), а письму нужен винительный падеж или
# английский артикль; ниша вне таблицы остаётся без зачина.
NICHE_FORMS = {
    "Стоматология": {"uk": "стоматолога", "en": "a dentist"},
    "Автосервис": {"uk": "автосервіс", "en": "an auto repair shop"},
    "Кафе/ресторан": {"uk": "де поїсти", "en": "a place to eat"},
    "Юрист": {"uk": "юриста", "en": "a lawyer"},
    "Салон красоты": {"uk": "салон краси", "en": "a beauty salon"},
    "Гостиница": {"uk": "готель", "en": "a hotel"},
    "Строительство": {"uk": "будівельників", "en": "a builder"},
}

# Зачин возвращает письму два якоря карточки — город и нишу, — которых у
# голого хвоста нет. Вариант выбирается по hash(lead_id) % 2, независимо от
# варианта хвоста (там % 3): пары не повторяются от лида к лиду.
OPENERS = {
    "uk": ["Шукав {niche} {city}", "Вибирав {niche} {city}"],
    "en": ["I was looking for {niche} in {city}",
           "While looking for {niche} in {city}, I"],
}
# Второй английский зачин кончается на «, I»: запятая в нём уже стоит, и хвост
# приклеивается пробелом, иначе в строке оказались бы две запятые подряд.
GLUED_OPENER_END = ", I"

# Местный падеж города — таблицей, а не правилом: «Кривий Ріг» → «у Кривому
# Розі» и «Ужгород» → «в Ужгороді» из именительного алгоритмом не выводятся.
# Ключи — как город пишут в карточке, включая русские написания. Город вне
# таблицы получает «у місті {как введено}»: неверный падеж в первой же строке
# письма читается хуже нейтрального оборота.
_LOCATIVES = (
    ("у Києві", ("Київ", "Киев")),
    ("у Харкові", ("Харків", "Харьков")),
    ("в Одесі", ("Одеса", "Одесса")),
    ("у Дніпрі", ("Дніпро", "Днепр")),
    ("у Львові", ("Львів", "Львов")),
    ("у Запоріжжі", ("Запоріжжя", "Запорожье")),
    ("у Кривому Розі", ("Кривий Ріг", "Кривой Рог")),
    ("у Миколаєві", ("Миколаїв", "Николаев")),
    ("у Вінниці", ("Вінниця", "Винница")),
    ("у Полтаві", ("Полтава",)),
    ("у Чернігові", ("Чернігів", "Чернигов")),
    ("у Черкасах", ("Черкаси", "Черкассы")),
    ("у Житомирі", ("Житомир",)),
    ("у Сумах", ("Суми", "Сумы")),
    ("у Хмельницькому", ("Хмельницький", "Хмельницкий")),
    ("в Ужгороді", ("Ужгород",)),
    ("у Чернівцях", ("Чернівці", "Черновцы")),
    ("у Рівному", ("Рівне", "Ровно")),
    ("в Івано-Франківську", ("Івано-Франківськ", "Ивано-Франковск")),
    ("у Тернополі", ("Тернопіль", "Тернополь")),
    ("у Луцьку", ("Луцьк", "Луцк")),
    ("у Кропивницькому", ("Кропивницький", "Кропивницкий")),
    ("у Херсоні", ("Херсон",)),
    ("у Мукачеві", ("Мукачево",)),
    ("у Броварах", ("Бровари",)),
    ("в Ірпені", ("Ірпінь", "Ирпень")),
    ("у Білій Церкві", ("Біла Церква", "Белая Церковь")),
    ("у Кам'янці-Подільському", ("Кам'янець-Подільський", "Каменец-Подольский")),
)
CITY_LOCATIVE = {name.lower(): form
                 for form, names in _LOCATIVES for name in names}

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


def niche_form(lead, lang: str | None = None) -> str:
    """Ниша в форме для зачина. Пусто — ниши нет в таблице, зачина не будет."""
    lang = lang or lang_of(lead)
    return NICHE_FORMS.get((lead.niche or "").strip(), {}).get(lang or "", "")


def uk_locative(city: str) -> str:
    """Город в местном падеже вместе с предлогом: «у Києві», «в Ужгороді»."""
    city = (city or "").strip()
    return CITY_LOCATIVE.get(city.lower(), f"у місті {city}")


def first_line(lead) -> str:
    """Первая строка письма: зачин из карточки плюс хвост из наблюдения.

    Пустая строка — фразы не нашлось.
    """
    lang = lang_of(lead)
    if lang is None:
        return ""
    niche = niche_form(lead, lang)
    if niche and lead.gap_type == "no_site":
        return _fill(_place(variant(lead.id, NO_SITE_LINES[lang]), lead, lang,
                            niche), lead.gap_value)
    options = FIRST_LINES.get(lead.gap_type or "", {}).get(lang)
    if not options:
        return ""
    tail = _fill(variant(lead.id, options), lead.gap_value)
    if not niche:
        # Ниши нет в таблице форм — зачин собрать не из чего, и письмо
        # начинается хвостом, как до появления зачинов.
        return tail[0].upper() + tail[1:]
    opener = _place(variant(lead.id, OPENERS[lang]), lead, lang, niche)
    glue = " " if opener.endswith(GLUED_OPENER_END) else ", "
    return opener + glue + tail


def subject(lead) -> str:
    lang = lang_of(lead)
    if lang not in SUBJECTS:
        return ""
    return variant(lead.id, SUBJECTS[lang]).format(name=(lead.name or "").strip())


def _place(template: str, lead, lang: str, niche: str) -> str:
    """Подстановка ниши и города. Через replace, чтобы не трогать {v}: он
    подставляется отдельно и может прийти из карточки с любыми символами."""
    city = (lead.city or "").strip()
    if lang == "uk":
        city = uk_locative(city)
    return template.replace("{niche}", niche).replace("{city}", city)


def _fill(template: str, value: str | None) -> str:
    value = (value or "").strip()
    if "{v1}" in template:
        first, _, second = value.partition(",")
        return template.format(v1=first.strip(), v2=second.strip())
    if "{v}" in template:
        return template.format(v=value)
    return template
