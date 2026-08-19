import re

import phonenumbers

# Схема в начале строки: http://, https://, любая другая по RFC 3986, а также
# протокол-относительный «//host» — иначе он весь ушёл бы в путь и дал None.
_SCHEME = re.compile(r"^(?:[a-z][a-z0-9+.-]*:)?//")


def normalize_domain(url: str | None) -> str | None:
    """URL → голый домен, по которому и ищется дубликат.

    Путь, query и фрагмент отбрасываются намеренно. Раньше они оставались,
    и в базу ложилось shop.example.com/ua — тогда та же компания под адресом
    shop.example.com/ проходила и проверку в форме, и уникальный индекс
    uq_leads_domain_norm_active, то есть дедупликация сайтов не работала
    ни для одного адреса сложнее корня.
    """
    if not url:
        return None
    s = url.strip().lower()
    s = _SCHEME.sub("", s)
    # хост кончается на первом же разделителе: /path, ?query, #fragment
    s = re.split(r"[/?#]", s, maxsplit=1)[0]
    # user:pass@host — учётные данные к домену не относятся
    s = s.rpartition("@")[2]
    if s.startswith("["):
        # IPv6-литерал: двоеточия внутри скобок — часть адреса, а не порт
        host, sep, rest = s.partition("]")
        s = host + sep
    else:
        s = s.split(":", 1)[0]
    if s.startswith("www."):
        s = s[4:]
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
