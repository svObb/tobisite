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
assert normalize_phone("не номер") is None
assert normalize_phone("12345", None) == "+12345"  # фолбэк: только цифры

print("smoke ok")
