"""Приёмка наблюдения (Д12 §2): вопросы, примеры и семь правил проверки.

Чистые функции без aiogram и без базы — их гоняют юнит-тесты, а хендлеры формы
только показывают то, что здесь вернулось. Тексты вопросов, кнопок, примеров и
скриптованных отказов — украинские: работник в этот момент смотрит на украинский
сайт, и весь экран наблюдения написан на языке рынка.
"""
import hashlib
import re

import config

MIN_LEN = 12
MAX_LEN = 120
# Быстрее 20 секунд сайт физически не откроешь и не посмотришь (правило 6).
MIN_OBSERVE_SECONDS = 20
SLOW_MIN, SLOW_MAX = 2, 60

ASK_TYPE = ("Відкрий сайт компанії З ТЕЛЕФОНА і подивись 20 секунд. "
            "Що ти побачив?")

# Типы, у которых артефакт — выбор из трёх кнопок, а не свободный текст.
CHOICE_OPTIONS = {
    "no_booking": ["тільки телефон", "тільки Facebook", "нічого"],
    "form_broken": ["помилка", "нічого", "перезавантажилась"],
}
# Единственный тип, где артефакт — файл.
PHOTO_TYPE = "no_mobile"
# Типы, у которых артефакт — свободный текст: только их проверяет правило 1
# (длина) и правило 4 (копипаста).
TEXT_TYPES = {"no_site", "no_prices", "stale", "no_https", "contact_mismatch"}
# Кроме no_site и no_booking каждый тип говорит о том, что видно на сайте, —
# без сайта в карточке такое наблюдение не с чего снять.
NEEDS_SITE = {"slow", "no_mobile", "form_broken", "no_prices", "stale",
              "no_https", "contact_mismatch"}

QUESTIONS = {
    "no_site": "Де ти шукав сайт і що знайшлось замість нього?",
    "no_mobile": "Надішли скриншот сторінки з телефону.",
    "slow": "Скільки секунд вантажилась головна? Заміряй секундоміром.",
    "no_booking": "Як зараз можна записатись?",
    "form_broken": "Що сталось після натискання «Надіслати»?",
    "no_prices": "Що саме ти шукав і не знайшов?",
    "stale": "Постав цитату з сайту або дату, яка застаріла.",
    "no_https": "Що саме показав браузер? Перепиши текст попередження.",
    "contact_mismatch": "Постав два значення через кому: що на сайті і що в Google Maps.",
}

# Два хороших примера и один плохой, тремя строками. У кнопочных типов примеров
# нет: там ответ — одна из трёх кнопок, и показывать «приклади» нечего.
EXAMPLES = {
    "no_site": [
        "✅ шукав у Google і Google Maps — знайшов сторінку у Facebook",
        "✅ у Google Maps тільки телефон, свого сайту немає ніде",
        "❌ «сайту немає»",
    ],
    "no_mobile": [
        "✅ на скриншоті видно, що текст виходить за екран",
        "✅ на скриншоті видно горизонтальну прокрутку",
        "❌ скриншот з комп'ютера",
    ],
    "slow": ["✅ 8", "✅ 12", "❌ «довго», «дуже довго»"],
    "no_prices": [
        "✅ шукав ціну на імплантацію — цін на сайті немає",
        "✅ немає меню, тільки фото страв",
        "❌ «сайт поганий»",
    ],
    "stale": [
        "✅ на головній: «Графік роботи на 2023 рік»",
        "✅ останній запис у новинах: 12.03.2022",
        "❌ «інформація стара»",
    ],
    "no_https": [
        "✅ браузер написав «З'єднання не захищене»",
        "✅ замість сайту: «Ваше підключення не є приватним»",
        "❌ «сайт не працює»",
    ],
    "contact_mismatch": [
        "✅ +380501112233, +380671114455",
        "✅ вул. Січова 12, вул. Січова 40",
        "❌ «телефони різні»",
    ],
}

# Правило 2: оценка вместо факта. Цифра или цитата рядом снимают подозрение —
# «старий сайт: останній пост 2019» это уже наблюдение.
BANAL_RE = re.compile(
    r"плох|погано|старий сайт|некрасив|жахлив|не подобається|фігня|застарілий",
    re.IGNORECASE,
)
BANAL_ANSWER = ("Це оцінка, а не спостереження. Напиши, що саме ти побачив: "
                "цифру, цитату або що не спрацювало.")
# Правило 5: работник фиксирует факт, а не пишет письмо.
SELLING_RE = re.compile(r"зробимо вам|пропоную|ми можемо", re.IGNORECASE)
SELLING_ANSWER = ("Тут фіксуємо факт, а не пропозицію. Напиши, що саме ти "
                  "побачив на сайті.")
COPYPASTE_ANSWER = ("Таке саме спостереження вже є в іншій твоїй картці. "
                    "Відкрий сайт цієї компанії і напиши, що бачиш там.")

QUOTES = "\"'«»„“”‘’"
# Дата в тексте: 12.03.2022, 12/03/22 или просто год.
DATE_RE = re.compile(r"\d{1,2}[./]\d{1,2}[./]\d{2,4}|(?:19|20)\d{2}")
# Правило под contact_mismatch: ровно два непустых куска через запятую.
PAIR_RE = re.compile(r"^[^,]{2,},[^,]{2,}$")


def ask_value(gap_type: str) -> str:
    """Вопрос про артефакт плюс примеры — тем же сообщением."""
    lines = [QUESTIONS.get(gap_type, "")] + EXAMPLES.get(gap_type, [])
    return "\n".join(lines)


def type_error(gap_type: str, website_url: str | None) -> str | None:
    """Правило 3: связка типа разрыва с сайтом в карточке."""
    if gap_type not in config.GAP_TYPE_LABELS:
        return "Обери тип кнопкою."
    if gap_type == "no_site" and website_url:
        return ("У картці вже вказаний сайт — «Сайту немає» не підходить. "
                "Обери, що саме з ним не так.")
    if gap_type in NEEDS_SITE and not website_url:
        return ("Для цього типу потрібен сайт у картці. "
                "Обери «Сайту немає» або інший тип.")
    return None


def text_error(value: str) -> str | None:
    """Правила 1, 2 и 5 — общие для любого свободного текста наблюдения."""
    if not MIN_LEN <= len(value) <= MAX_LEN:
        return f"Потрібно від {MIN_LEN} до {MAX_LEN} символів. Напиши конкретніше."
    if SELLING_RE.search(value):
        return SELLING_ANSWER
    if BANAL_RE.search(value) and not _has_proof(value):
        return BANAL_ANSWER
    return None


def check_value(gap_type: str, raw: str) -> tuple[str | None, str | None]:
    """Артефакт → (значение, ошибка). Ровно одно из двух не None."""
    value = " ".join((raw or "").split())
    if gap_type == "slow":
        if not (value.isascii() and value.isdecimal()):
            return None, "Потрібне число секунд. Заміряй секундоміром."
        if not SLOW_MIN <= int(value) <= SLOW_MAX:
            return None, f"Число секунд від {SLOW_MIN} до {SLOW_MAX}."
        return value, None
    if gap_type in CHOICE_OPTIONS:
        if value not in CHOICE_OPTIONS[gap_type]:
            return None, "Обери один із варіантів кнопкою."
        return value, None
    err = text_error(value)
    if err:
        return None, err
    if gap_type == "stale" and not _has_quote_or_date(value):
        return None, ("Постав цитату в лапках або дату з сайту — інакше це "
                      "не факт, а враження.")
    if gap_type == "contact_mismatch" and not PAIR_RE.match(value):
        return None, ("Потрібні два значення через кому: що на сайті і що "
                      "в Google Maps.")
    return value, None


def check_note(raw: str) -> tuple[str | None, str | None]:
    """Необязательная деталь — те же правила длины, банальностей и продаж."""
    value = " ".join((raw or "").split())
    err = text_error(value)
    return (None, err) if err else (value, None)


def copypaste_hash(gap_type: str | None, value: str | None,
                   note: str | None) -> str | None:
    """Правило 4: отпечаток свободного текста наблюдения. None — сверять нечего.

    Сверяется только свободный текст. У кнопочных типов значений всего три,
    у slow — число от 2 до 60: там совпадение с прошлой карточкой означает не
    копипасту, а что сайты действительно похожи, и отказ был бы ложным.
    """
    free = " ".join(part for part in (
        value if gap_type in TEXT_TYPES else "", note or "") if part)
    free = " ".join(free.split()).lower()
    return hashlib.sha256(free.encode()).hexdigest() if free else None


def too_fast(seconds: float | None) -> bool:
    return seconds is not None and seconds < MIN_OBSERVE_SECONDS


def gap_line(gap_type: str | None, value: str | None, note: str | None) -> str:
    """Строка наблюдения для карточки и экрана подтверждения."""
    if not gap_type:
        return "не снято"
    label = config.GAP_TYPE_LABELS.get(gap_type, gap_type)
    parts = [label]
    if value:
        parts.append(value)
    if note:
        parts.append(note)
    return " — ".join(parts)


def _has_quote_or_date(value: str) -> bool:
    return any(q in value for q in QUOTES) or bool(DATE_RE.search(value))


def _has_proof(value: str) -> bool:
    """Цитата, дата или число рядом — то, что отличает факт от оценки."""
    return _has_quote_or_date(value) or any(ch.isdigit() for ch in value)
