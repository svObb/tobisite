import os
import sys
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# Тестовый режим: python main.py --test, для alembic — переменная TOBISITE_TEST=1.
# Боевой запуск (systemd) идёт без флага и в тест уйти не может.
TEST_MODE = "--test" in sys.argv or os.getenv("TOBISITE_TEST") == "1"


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
# Имя строки админа в workers: админ тоже заносит компании, а у лида worker_id
# обязателен. Под этим именем админ виден в статистике по работникам и в CSV.
# Необязательная: с _req выкат кода потребовал бы сначала править .env на сервере.
ADMIN_NAME = (os.getenv("ADMIN_NAME") or "").strip() or "Администратор"

# Второй админ (6.16): EXTRA_ADMINS=«tg_id|Имя, tg_id|Имя», формат тот же, что
# у COUNTRIES. Пусто — админ один, как было. Права одинаковые: отдельной роли
# модератора канон не описывает, а придумывать её на пустом месте нечего.
EXTRA_ADMINS = []
for _part in os.getenv("EXTRA_ADMINS", "").split(","):
    _part = _part.strip()
    if not _part:
        continue
    _tg, _, _name = _part.partition("|")
    _tg = _tg.strip()
    if not (_tg.isascii() and _tg.isdecimal()):
        raise SystemExit(
            f"EXTRA_ADMINS: «{_part}» — нужен tg_id или «tg_id|Имя»"
        )
    EXTRA_ADMINS.append(
        (int(_tg), _name.strip() or f"Администратор {len(EXTRA_ADMINS) + 2}")
    )

# Основной админ всегда первый: его строка в workers уже есть на бою, и вторую
# под тем же человеком заводить нельзя.
ADMIN_IDS = [ADMIN_TG_ID] + [tg for tg, _ in EXTRA_ADMINS if tg != ADMIN_TG_ID]
ADMIN_NAMES = dict(EXTRA_ADMINS) | {ADMIN_TG_ID: ADMIN_NAME}


def is_admin(tg_id: int) -> bool:
    return tg_id in ADMIN_IDS


DEFAULT_DAILY_LIMIT = int(os.getenv("DEFAULT_DAILY_LIMIT", "15"))
CANCEL_WINDOW_MIN = int(os.getenv("CANCEL_WINDOW_MIN", "60"))
# Месячный потолок расходов на ИИ и платные API, $ (раздел 20 плана).
# 0 выключает кэп и алерты — так жить не стоит.
AI_MONTHLY_CAP_USD = float(os.getenv("AI_MONTHLY_CAP_USD", "150"))
# Потолок карточек, которые скаут может импортировать за день (15.23):
# модерация — ручной труд, топить её сотней сырых записей нельзя
SCOUT_DAILY_RAW_LIMIT = int(os.getenv("SCOUT_DAILY_RAW_LIMIT", "50"))
# PageSpeed Insights для лучших кандидатов скаута (15.12): бесплатно, но
# Lighthouse думает 15–40 сек на URL — поэтому потолок на прогон; 0 выключает.
# Ключ не обязателен: без него квота ~1 запрос/сек с IP, нашим объёмам хватает.
PSI_MAX_PER_RUN = int(os.getenv("PSI_MAX_PER_RUN", "5"))
# Сколько спорных карточек прогона уходит в ИИ-гейт (15.18). Потолок держит
# и деньги, и размер одного ответа модели: пачка едет одним запросом.
# 0 выключает гейт — спорные карточки поедут на модерацию сырыми.
SCOUT_GATE_MAX = int(os.getenv("SCOUT_GATE_MAX", "20"))
# Как часто бот забирает из R2 открытия превью (10.20). Наружных портов у него
# нет, поэтому события ждут в бакете, а не приходят сами. 0 выключает опрос;
# без ключей R2 он и так молча спит.
PREVIEW_HITS_POLL_SEC = int(os.getenv("PREVIEW_HITS_POLL_SEC", "120"))
PAGESPEED_API_KEY = (os.getenv("PAGESPEED_API_KEY") or "").strip()
# Счета подписки (12.29, 12.16, 12.30). Сколько дней у клиента на оплату,
# как часто повторять напоминание по неоплаченному и за сколько дней
# предупреждать о следующем счёте. Ежемесячность самого цикла — не настройка:
# подписка помесячная (12.2).
INVOICE_DUE_DAYS = int(os.getenv("INVOICE_DUE_DAYS", "7"))
INVOICE_REMIND_EVERY_DAYS = int(os.getenv("INVOICE_REMIND_EVERY_DAYS", "3"))
INVOICE_NOTICE_DAYS = int(os.getenv("INVOICE_NOTICE_DAYS", "3"))
# Как часто бот смотрит на календарь счетов. Раз в час: счета живут в днях,
# чаще незачем. 0 выключает фоновую задачу — цикл двигают руками, /invoice run.
BILLING_POLL_SEC = int(os.getenv("BILLING_POLL_SEC", "3600"))
# Цена одного вызова платного не-ИИ API, $ (20.3): Places считается по SKU,
# Twilio — по сообщению. Числа берутся из прайса провайдера и живут в .env:
# выдуманная цена в коде врала бы в юнит-экономике месяцами. Не задана —
# вызовы всё равно идут в журнал, но с нулевой стоимостью.
API_PRICES = {
    "places": float(os.getenv("PLACES_CALL_USD", "0")),
    "twilio": float(os.getenv("TWILIO_SMS_USD", "0")),
}
# Ключ Anthropic для генерации писем и слотов черновиков. Необязательный:
# без него генерация недоступна и лид уходит в ручную ветку — фолбэка на
# выдумывание текста нет и не будет.
ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
# Дозаполнение услуг и часов моделью при обогащении с сайта (дорожка III).
# Выключено по умолчанию: разбор DOM справляется на большинстве сайтов, а
# бюджет ключа ≤$20/мес тратится на письма и слоты черновиков.
ENRICH_AI = (os.getenv("ENRICH_AI", "0").strip().lower()
             in ("1", "true", "yes"))
# Подпись и юридические строки письма (Д12 §1, слой 0). Настоящий почтовый
# адрес появится после подключения M365 — до тех пор заглушка, и это ещё одна
# причина, по которой письма наружу не уходят.
SIGNATURE_NAME = (os.getenv("SIGNATURE_NAME") or "").strip()
SIGNATURE_COMPANY = (os.getenv("SIGNATURE_COMPANY") or "").strip()
POSTAL_ADDRESS = (os.getenv("POSTAL_ADDRESS") or "").strip()
# Ссылка отписки (9.30): подставлять её будет Instantly своим merge-тегом,
# точное написание узнаем при подключении. Пусто — строки в письме нет, и
# email_legal.missing() считает это незакрытым требованием.
UNSUBSCRIBE_TAG = (os.getenv("UNSUBSCRIBE_TAG") or "").strip()
TZ = ZoneInfo(os.getenv("TZ", "Europe/Kyiv"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.getenv("LOG_FILE", "bot.log")
if TEST_MODE and LOG_FILE:
    LOG_FILE = "test-" + LOG_FILE

_db_url = _pick("TEST_DATABASE_URL", "DATABASE_URL")


def db_key(url: str) -> tuple:
    """(хост, порт, база) — то, что реально определяет, куда пойдут записи.

    Сырое сравнение строк пропускало главный сценарий ошибки: у Neon pooled-хост
    отличается от прямого только суффиксом -pooler в первой метке имени, и одна
    и та же база под двумя строками выглядела «разными». Регистр, querystring
    (?ssl=require), учётные данные и явный :5432 — тоже не различия.
    """
    u = urlsplit(url.strip())
    host = (u.hostname or "").lower()
    first, dot, rest = host.partition(".")
    if first.endswith("-pooler"):
        host = first[: -len("-pooler")] + dot + rest
    try:
        port = u.port or 5432
    except ValueError:
        port = 5432
    return host, port, (u.path or "").rstrip("/")


_prod_url = (os.getenv("DATABASE_URL") or "").strip()
if TEST_MODE and _prod_url and db_key(_db_url) == db_key(_prod_url):
    raise SystemExit(
        "TEST_DATABASE_URL указывает на ту же базу, что и боевая DATABASE_URL "
        "(совпадают хост, порт и имя базы) — тестовые записи попали бы "
        "в рабочую базу"
    )
if _db_url.startswith("postgresql://"):
    _db_url = _db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
DATABASE_URL = _db_url

# PgBouncer в transaction-режиме (так работают pooled-хосты Neon и Supabase) не
# переваривает prepared statements asyncpg — прилетает DuplicatePreparedStatement.
# Прямое подключение к локальному Postgres их переваривает, и кэш там нужен:
# он экономит round-trip на каждом повторном запросе. Боевая база локальная,
# тестовая — pooled в Neon, поэтому режим определяется по хосту, а не жёстко.
# DB_POOLED=1/0 в .env перебивает автоопределение, если хост назван иначе.
_pooled = os.getenv("DB_POOLED", "").strip().lower()
if _pooled in ("1", "true", "yes"):
    POOLED_DB = True
elif _pooled in ("0", "false", "no"):
    POOLED_DB = False
else:
    POOLED_DB = "pooler" in DATABASE_URL

CONNECT_ARGS = {"statement_cache_size": 0} if POOLED_DB else {}

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

# raw/candidate стоят до new: это стадии ДО ручной проверки — их создаёт
# только лид-скаут (раздел 15), человек через форму так статус не выставит
STATUSES = [
    ("raw", "Скаут: сырой"), ("candidate", "Скаут: кандидат"),
    ("new", "Новый"), ("verified", "Проверен"), ("draft_ready", "Черновик готов"),
    ("sent", "Отправлено"), ("replied", "Ответил"),
    # отдельно от replied: ответ бывает и «спасибо, не надо». Пара счётчиков
    # replied / replied_interested и есть та воронка, по которой видно, работает
    # ли письмо, а не только доставка (7.19)
    ("replied_interested", "Заинтересован"), ("sold", "Продано"),
    ("refused", "Отказ"), ("rejected", "Отклонён"),
]
STATUS_LABELS = dict(STATUSES)
ACCEPTED_STATUSES = ["verified", "draft_ready", "sent", "replied",
                     "replied_interested", "sold", "refused"]

# О чём работнику сообщает бот (6.14): решения по его лиду и исход сделки.
# Внутренних шагов конвейера (draft_ready, sent) здесь нет намеренно — сделать
# с ними работнику нечего, а поток служебных сообщений он читать перестанет.
WORKER_NOTIFY_STATUSES = ["verified", "rejected", "refused", "replied",
                          "replied_interested", "sold"]

# Причины отклонения лида админом (6.17): работнику уходит не голое «отклонён»,
# а то, что он починит в следующей карточке. Ключи лежат в базе под
# CHECK-констрейнтом — новая причина требует миграции, как и новый статус.
LEAD_REJECT_REASONS = [
    ("no_contact", "Нет рабочего контакта"),
    ("not_our_niche", "Не наша ниша"),
    ("site_is_fine", "Сайт в порядке"),
    ("closed", "Компания закрыта"),
    ("duplicate", "Дубль существующего"),
    ("gap_weak", "Наблюдение не подтвердилось"),
    ("bad_data", "Данные карточки неверны"),
    ("other", "Другое"),
]
LEAD_REJECT_LABELS = dict(LEAD_REJECT_REASONS)

# 6.13: админ узнаёт о каждой новой компании. Выключается на время массового
# заноса, когда поток сообщений перестаёт что-либо значить.
NOTIFY_NEW_LEAD = (os.getenv("NOTIFY_NEW_LEAD", "1").strip().lower()
                   not in ("0", "false", "no"))

# Типы разрыва (Д12 §2): по одному на лид, каждый обязывает к своему артефакту —
# числу, цитате, скриншоту или паре значений. Подписи кнопок — из Д12 §2.
# Порядок = порядок кнопок, 9 штук в 3 ряда.
GAP_TYPES = [
    ("no_site", "Сайту немає"),
    ("no_mobile", "Не адаптований під телефон"),
    ("slow", "Вантажився довго"),
    ("no_booking", "Немає онлайн-запису"),
    ("form_broken", "Форма не працює"),
    ("no_prices", "Немає цін або меню"),
    ("stale", "Застаріла інформація"),
    ("no_https", "Браузер попереджає"),
    ("contact_mismatch", "Телефон/адреса не збігаються"),
]
GAP_TYPE_LABELS = dict(GAP_TYPES)

# Потолки касаний — константы кода, а не .env: это закон компании (Д12 §8),
# а не настройка, которую правят на сервере между деплоями.
MAX_TOUCHES_PER_LEAD = 3
MAX_MESSENGER_MESSAGES = 4

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
