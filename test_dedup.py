from dedup import normalize_domain, normalize_phone

assert normalize_domain("https://www.Example.com/") == "example.com"
assert normalize_domain("http://example.com/page/") == "example.com/page"
assert normalize_domain("Example.COM") == "example.com"
assert normalize_domain("https://www.example.com") == normalize_domain("example.com/")
assert normalize_domain("") is None
assert normalize_domain(None) is None

assert normalize_phone("+380 50 123 45 67") == "+380501234567"
assert normalize_phone("050 123 45 67", "UA") == "+380501234567"
assert normalize_phone("+380501234567") == normalize_phone("050-123-45-67", "UA")

# Неразобранный номер — это None, а не «+ и одни цифры». Прежний фолбэк давал
# для 050 123 45 67 строку +0501234567, которая никогда не совпадёт с настоящим
# +380501234567, и уникальный индекс по value_norm пропускал такой дубль.
assert normalize_phone("не номер") is None
assert normalize_phone("12345", None) is None
assert normalize_phone("050 123 45 67", None) is None

# Ключевое свойство: один и тот же номер в любой записи даёт одну и ту же норму,
# если region берётся из страны лида одинаково при проверке и при сохранении.
assert normalize_phone("050 123 45 67", "UA") == normalize_phone("+380501234567", "UA")
assert normalize_phone("0905 123 456", "SK") == normalize_phone("+421905123456", "SK")

print("smoke ok")
