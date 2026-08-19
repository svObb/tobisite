from dedup import normalize_domain, normalize_phone

assert normalize_domain("https://www.Example.com/") == "example.com"
assert normalize_domain("Example.COM") == "example.com"
assert normalize_domain("https://www.example.com") == normalize_domain("example.com/")
assert normalize_domain("") is None
assert normalize_domain(None) is None

# Ключевое свойство домена: адрес любой глубины сводится к одному и тому же
# хосту. Пока путь оставался в норме, shop.example.com/ua и shop.example.com/
# были для базы разными сайтами, и уникальный индекс пропускал дубль.
assert normalize_domain("http://example.com/page/") == "example.com"
assert normalize_domain("https://shop.example.com/ua") == "shop.example.com"
assert normalize_domain("https://example.com/a/b?utm=1#top") == "example.com"
assert normalize_domain("https://example.com?utm=1") == "example.com"
assert normalize_domain("https://example.com#top") == "example.com"
assert normalize_domain("https://www.example.com:8443/x") == "example.com"
assert normalize_domain("https://user:pass@example.com/x") == "example.com"
assert normalize_domain("//example.com/x") == "example.com"
assert (normalize_domain("https://example.com/ua")
        == normalize_domain("http://WWW.Example.com:80/en?a=1"))

# Поддомен — отдельный сайт, схлопывать его нельзя
assert normalize_domain("https://shop.example.com") != normalize_domain("https://example.com")

# IPv6-литерал: двоеточия внутри скобок не порт
assert normalize_domain("http://[2001:db8::1]:8080/x") == "[2001:db8::1]"

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
