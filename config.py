import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_TG_ID = int(os.environ["ADMIN_TG_ID"])
ACCESS_CODE = os.environ["ACCESS_CODE"]
DEFAULT_DAILY_LIMIT = int(os.getenv("DEFAULT_DAILY_LIMIT", "15"))
CANCEL_WINDOW_MIN = int(os.getenv("CANCEL_WINDOW_MIN", "60"))
TZ = ZoneInfo(os.getenv("TZ", "Europe/Kyiv"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.getenv("LOG_FILE", "bot.log")

_db_url = os.environ["DATABASE_URL"]
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
