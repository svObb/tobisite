import os
import sys
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# Тестовый режим: python main.py --test, для alembic — переменная QDIF_TEST=1.
# Боевой запуск (systemd) идёт без флага и в тест уйти не может.
TEST_MODE = "--test" in sys.argv or os.getenv("QDIF_TEST") == "1"


def _req(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise SystemExit(f"В .env не заполнена переменная {name}")
    return value


def _pick(test_name: str, prod_name: str) -> str:
    return _req(test_name if TEST_MODE else prod_name)


BOT_TOKEN = _pick("BOT_TEST_TOKEN", "BOT_TOKEN")
ADMIN_TG_ID = int(_req("ADMIN_TG_ID"))
ACCESS_CODE = _req("ACCESS_CODE")
DEFAULT_DAILY_LIMIT = int(os.getenv("DEFAULT_DAILY_LIMIT", "15"))
CANCEL_WINDOW_MIN = int(os.getenv("CANCEL_WINDOW_MIN", "60"))
TZ = ZoneInfo(os.getenv("TZ", "Europe/Kyiv"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.getenv("LOG_FILE", "bot.log")
if TEST_MODE and LOG_FILE:
    LOG_FILE = "test-" + LOG_FILE

_db_url = _pick("TEST_DATABASE_URL", "DATABASE_URL")
if TEST_MODE and _db_url == (os.getenv("DATABASE_URL") or "").strip():
    raise SystemExit(
        "TEST_DATABASE_URL совпадает с боевой DATABASE_URL — "
        "тестовые записи попали бы в рабочую базу"
    )
if _db_url.startswith("postgresql://"):
    _db_url = _db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
DATABASE_URL = _db_url

# [("Украина", "UA"), ...]
COUNTRIES = []
for part in os.getenv("COUNTRIES", "").split(","):
    part = part.strip()
    if not part:
        continue
    name, _, iso = part.partition("|")
    COUNTRIES.append((name.strip(), iso.strip().upper() or None))
COUNTRY_ISO = {name: iso for name, iso in COUNTRIES}

LANGUAGES = [s.strip() for s in os.getenv("LANGUAGES", "").split(",") if s.strip()]

NICHES = [
    "Стоматология", "Автосервис", "Кафе/ресторан", "Юрист",
    "Салон красоты", "Гостиница", "Строительство",
]

FOUND_VIA = ["Google Maps", "Поиск", "Instagram", "Facebook"]

CONTACT_TYPES = [
    ("phone", "Телефон"), ("email", "Email"), ("instagram", "Instagram"),
    ("telegram", "Telegram"), ("whatsapp", "WhatsApp"), ("viber", "Viber"),
]
CONTACT_TYPE_LABELS = dict(CONTACT_TYPES) | {"other": "Другое"}

STATUSES = [
    ("new", "Новый"), ("verified", "Проверен"), ("draft_ready", "Черновик готов"),
    ("sent", "Отправлено"), ("replied", "Ответил"), ("sold", "Продано"),
    ("refused", "Отказ"), ("rejected", "Отклонён"),
]
STATUS_LABELS = dict(STATUSES)
ACCEPTED_STATUSES = ["verified", "draft_ready", "sent", "replied", "sold", "refused"]

MAX_CONTACTS = 10
PAGE_SIZE = 10

INSTRUCTION_TEXT = (
    "<b>Что считается хорошим лидом</b>\n\n"
    "• Компания без сайта, либо сайт устаревший, медленный или без мобильной версии.\n"
    "• У компании есть телефон и признаки жизни: отзывы, часы работы, активность.\n"
    "• Платёжеспособная ниша: стоматологии, автосервисы, кафе и рестораны, "
    "юристы, салоны красоты, гостиницы, строительство.\n\n"
    "Обязательно указывай ссылку на источник (Google Maps, соцсеть, каталог) "
    "и минимум один контакт. Телефоны вводи в международном формате (+380…)."
)

EDITABLE_FIELDS = [
    ("name", "Название"), ("website_url", "Сайт"), ("source_url", "Источник"),
    ("country", "Страна"), ("city", "Город"), ("language", "Язык"),
    ("niche", "Ниша"), ("google_rating", "Рейтинг"), ("note", "Заметка"),
    ("screenshot_file_id", "Скриншот"), ("found_via", "Где нашли"),
]
FIELD_LABELS = dict(EDITABLE_FIELDS)
