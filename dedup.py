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
    raw = raw.strip()
    if not raw:
        return None
    try:
        parsed = phonenumbers.parse(raw, region)
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(
                parsed, phonenumbers.PhoneNumberFormat.E164
            )
    except phonenumbers.NumberParseException:
        pass
    digits = re.sub(r"\D", "", raw)
    return f"+{digits}" if digits else None
