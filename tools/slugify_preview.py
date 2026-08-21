"""Слаг превью: название компании -> метка поддомена *.tobisitepreview.com.

Universal SSL Free выдаёт сертификат ровно на один уровень wildcard, поэтому
точка в слаге ломает HTTPS у клиента: pravo.i.dilo.tobisitepreview.com выдаст
предупреждение браузера. Отсюда жёсткое правило — в слаге только [a-z0-9-],
и ни точек, ни апострофов, ни кириллицы.

Транслит украинский по КМУ 55-2010 (позиционные варианты є/ї/й/ю/я, зг -> zgh),
русский — отдельной таблицей: она включается, только если в тексте есть
буквы ы/э/ъ/ё и нет украинских і/ї/є/ґ.

    python tools/slugify_preview.py   # прогнать тесты
"""
import re
import unicodedata

# запас под суффикс дедупа: метка DNS не длиннее 63 символов
MAX_LEN = 48

SLUG_HOST_SUFFIX = ".tobisitepreview.com"

UA = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e",
    "є": "ie", "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "shch", "ь": "", "ю": "iu", "я": "ia",
}
UA_INITIAL = {"є": "ye", "ї": "yi", "й": "y", "ю": "yu", "я": "ya"}

RU = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

# латиница, которую NFKD не раскладывает (польский, скандинавы, немецкий)
LATIN_EXTRAS = {
    "ł": "l", "ø": "o", "đ": "d", "ß": "ss", "æ": "ae", "œ": "oe",
    "þ": "th", "ð": "d", "ı": "i",
}

APOSTROPHES = "'’‘`´ʼʼ"

_UA_ONLY = set("іїєґ")
_RU_ONLY = set("ыэъё")


def _table_for(text):
    """Русская таблица — только по её собственным буквам и без украинских."""
    if set(text) & _RU_ONLY and not set(text) & _UA_ONLY:
        return RU, {}
    return UA, UA_INITIAL


def _translit(text, table, initial):
    out = []
    start = True                       # начало слова: позиционные є/ї/й/ю/я
    i = 0
    while i < len(text):
        ch = text[i]
        if text[i:i + 2] == "зг":      # КМУ: зг -> zgh, иначе не отличить от ж
            out.append("zgh")
            i += 2
            start = False
            continue
        if ch in table:
            out.append(initial[ch] if start and ch in initial else table[ch])
            start = False
        else:
            out.append(ch)
            start = not ch.isalnum()
        i += 1
    return "".join(out)


def slugify(name, max_len=MAX_LEN):
    """Название -> метка поддомена. Всегда непустая и всегда [a-z0-9-]."""
    text = name.lower()
    for ch in APOSTROPHES:
        text = text.replace(ch, "")    # м'ясо -> miaso, а не m-iaso
    table, initial = _table_for(text)
    text = _translit(text, table, initial)
    text = "".join(LATIN_EXTRAS.get(ch, ch) for ch in text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return text.strip("-")[:max_len].strip("-") or "preview"


def unique_slug(name, exists, max_tries=50):
    """Свободный слаг: занятые получают суффикс -2, -3, ...

    exists — вызываемое slug -> bool (в publish_r2.py это head_object по R2).
    """
    base = slugify(name)
    if not exists(base):
        return base
    for n in range(2, max_tries + 1):
        candidate = f"{base}-{n}"
        if not exists(candidate):
            return candidate
    raise ValueError(f"не удалось подобрать свободный слаг для {name!r}")


if __name__ == "__main__":
    assert slugify('Юридична фірма "Право і Діло"') == "yurydychna-firma-pravo-i-dilo"
    assert slugify("ТОВ «Альфа.Бета» 2.0") == "tov-alfa-beta-2-0"
    assert "." not in slugify("adwokat.kyiv.ua")
    assert slugify("Сеть «Электрон»") == "set-elektron"          # русская таблица
    assert slugify("Kanzlei Müller & Söhne GmbH") == "kanzlei-muller-sohne-gmbh"
    assert slugify("Kancelaria Łukasz Mały") == "kancelaria-lukasz-maly"
    assert slugify("  —Юрист—  ") == "yuryst"
    assert slugify("!!! ???") == "preview"

    long_slug = slugify("Адвокатське об'єднання Захист Права і Свободи Громадян")
    assert len(long_slug) <= MAX_LEN and not long_slug.endswith("-"), long_slug

    taken = {"pravo-i-dilo", "pravo-i-dilo-2"}
    assert unique_slug("Право і Діло", taken.__contains__) == "pravo-i-dilo-3"
    assert unique_slug("Право і Воля", taken.__contains__) == "pravo-i-volia"

    print("ok")
