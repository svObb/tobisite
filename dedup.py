import re

import phonenumbers


def normalize_domain(url: str | None) -> str | None:
    if not url:
        return None
    s = url.strip().lower()
    s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s)
    if s.startswith("www."):
        s = s[4:]
    s = s.rstrip("/")
    return s or None


def normalize_phone(raw: str, region: str | None = None) -> str | None:
    """Телефон → E.164, либо None, если номер не разобрался.

    Фолбэка «оставить одни цифры» здесь сознательно нет. Он давал на выходе
    строку вроде +0501234567, которая никогда не совпадёт с настоящим
    +380501234567, — то есть ровно ломал ту дедупликацию, ради которой поле
    value_norm и заведено. Номер, который phonenumbers не понимает, лучше
    отклонить на вводе и попросить международный формат.
    """
    raw = raw.strip()
    if not raw:
        return None
    try:
        parsed = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
