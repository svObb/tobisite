from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config

CANCEL_CB = "x"

BTN_ADD = "➕ Добавить компанию"
BTN_MY = "📋 Мои компании"
BTN_MY_STATS = "📊 Моя статистика"
BTN_HELP = "ℹ️ Инструкция"

BTN_A_STATS = "📊 Статистика"
BTN_A_ALL = "📋 Все компании"
BTN_A_SEARCH = "🔍 Поиск"
BTN_A_CANCELLED = "🗑 Отменённые"
BTN_A_WORKERS = "👥 Работники"
BTN_A_BROADCAST = "📢 Написать всем"
BTN_A_CSV = "📤 Выгрузить CSV"


def worker_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ADD)],
            [KeyboardButton(text=BTN_MY), KeyboardButton(text=BTN_MY_STATS)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
    )


def admin_menu():
    # BTN_ADD и BTN_MY — те же константы, что у работника: админ заносит компании
    # тем же мастером. Двусмысленности нет, роутеры разведены фильтром is_admin.
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ADD)],
            [KeyboardButton(text=BTN_MY), KeyboardButton(text=BTN_A_STATS)],
            [KeyboardButton(text=BTN_A_ALL), KeyboardButton(text=BTN_A_SEARCH)],
            [KeyboardButton(text=BTN_A_CANCELLED), KeyboardButton(text=BTN_A_WORKERS)],
            [KeyboardButton(text=BTN_A_BROADCAST), KeyboardButton(text=BTN_A_CSV)],
        ],
        resize_keyboard=True,
    )


def menu(is_admin: bool):
    """Меню по роли. Форма добавления общая, а возвращаться из неё надо в своё."""
    return admin_menu() if is_admin else worker_menu()


def _cancel(b: InlineKeyboardBuilder):
    b.button(text="❌ Отмена", callback_data=CANCEL_CB)


def cancel_kb():
    b = InlineKeyboardBuilder()
    _cancel(b)
    return b.as_markup()


def choices_kb(options, pfx, other=True, skip=False, extra=None, per_row=2):
    """options — список подписей; callback = f"{pfx}:{индекс}", Другое = pfx:oth."""
    b = InlineKeyboardBuilder()
    for i, label in enumerate(options):
        b.button(text=label, callback_data=f"{pfx}:{i}")
    b.adjust(*([per_row] * ((len(options) + per_row - 1) // per_row or 1)))
    tail = InlineKeyboardBuilder()
    if other:
        tail.button(text="Другое", callback_data=f"{pfx}:oth")
    if extra:
        for label, cb in extra:
            tail.button(text=label, callback_data=cb)
    if skip:
        tail.button(text="Пропустить", callback_data=f"{pfx}:skip")
    _cancel(tail)
    tail.adjust(1)
    b.attach(tail)
    return b.as_markup()


def skip_kb(pfx):
    b = InlineKeyboardBuilder()
    b.button(text="Пропустить", callback_data=f"{pfx}:skip")
    _cancel(b)
    b.adjust(1)
    return b.as_markup()


def website_kb():
    b = InlineKeyboardBuilder()
    b.button(text="Сайта нет", callback_data="ws:none")
    _cancel(b)
    b.adjust(1)
    return b.as_markup()


def contact_more_kb():
    b = InlineKeyboardBuilder()
    b.button(text="➕ Да", callback_data="cm:yes")
    b.button(text="➡️ Дальше", callback_data="cm:next")
    _cancel(b)
    b.adjust(2, 1)
    return b.as_markup()


def confirm_kb():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Отправить", callback_data="cf:send")
    b.button(text="✏️ Заново", callback_data="cf:redo")
    _cancel(b)
    b.adjust(1)
    return b.as_markup()


def dup_kb():
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, всё равно отправить", callback_data="dup:yes")
    _cancel(b)
    b.adjust(1)
    return b.as_markup()


def saved_kb(lead_id):
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Редактировать", callback_data=f"led:{lead_id}")
    b.button(text="🚫 Отменить отправку", callback_data=f"lcx:{lead_id}")
    b.adjust(1)
    return b.as_markup()


def admin_saved_kb(lead_id):
    """Итог сохранения для админа: утвердить свою же компанию в одно нажатие.

    Работницкий saved_kb не подходит — в нём «Отменить отправку», а её хендлер
    живёт в роутере с фильтром ~is_admin. Оба callback'а ниже уже обслуживаются
    существующими хендлерами админки.
    """
    b = InlineKeyboardBuilder()
    b.button(text="✅ Утвердить", callback_data=f"stv:{lead_id}:verified")
    b.button(text="🗂 Открыть карточку", callback_data=f"acd:{lead_id}")
    b.adjust(1)
    return b.as_markup()


BTN_REGAP = "📸 Переснять наблюдение"


def regap_kb(lead_id):
    b = InlineKeyboardBuilder()
    b.button(text=BTN_REGAP, callback_data=f"rgp:{lead_id}")
    b.adjust(1)
    return b.as_markup()


def my_card_kb(lead_id, can_edit, can_cancel):
    b = InlineKeyboardBuilder()
    if can_edit:
        b.button(text="✏️ Редактировать", callback_data=f"led:{lead_id}")
    b.button(text=BTN_REGAP, callback_data=f"rgp:{lead_id}")
    if can_cancel:
        b.button(text="🚫 Отменить отправку", callback_data=f"lcx:{lead_id}")
    b.button(text="⬅️ К списку", callback_data="mlp:0")
    b.adjust(1)
    return b.as_markup()


def admin_card_kb(lead_id):
    b = InlineKeyboardBuilder()
    b.button(text="✏️ Редактировать", callback_data=f"led:{lead_id}")
    b.button(text=BTN_REGAP, callback_data=f"rgp:{lead_id}")
    b.button(text="🔄 Сменить статус", callback_data=f"sts:{lead_id}")
    b.button(text="🔗 Ссылка на черновик", callback_data=f"drf:{lead_id}")
    b.button(text="📝 Моя заметка", callback_data=f"anz:{lead_id}")
    b.button(text="📜 История", callback_data=f"hst:{lead_id}")
    b.button(text="🗑 Удалить", callback_data=f"del:{lead_id}")
    b.button(text="💡 Что допродать", callback_data=f"ups:{lead_id}")
    b.adjust(2)
    return b.as_markup()


def cancelled_card_kb(lead_id):
    b = InlineKeyboardBuilder()
    b.button(text="♻️ Восстановить", callback_data=f"rst:{lead_id}")
    b.button(text="⬅️ К списку", callback_data="cxp:0")
    b.adjust(1)
    return b.as_markup()


def filters_kb():
    b = InlineKeyboardBuilder()
    for label, key in [
        ("Работник", "worker"), ("Страна", "country"), ("Ниша", "niche"),
        ("Статус", "status"), ("Дата", "date"),
    ]:
        b.button(text=label, callback_data=f"afk:{key}")
    b.adjust(2)
    tail = InlineKeyboardBuilder()
    tail.button(text="♻️ Сброс", callback_data="afk:reset")
    tail.button(text="📋 Показать", callback_data="alp:0")
    tail.adjust(2)
    b.attach(tail)
    return b.as_markup()


def options_kb(labels, pfx, back="afk:back"):
    b = InlineKeyboardBuilder()
    for i, label in enumerate(labels):
        b.button(text=label, callback_data=f"{pfx}:{i}")
    b.adjust(2)
    tail = InlineKeyboardBuilder()
    tail.button(text="Любой", callback_data=f"{pfx}:any")
    tail.button(text="⬅️ Назад", callback_data=back)
    tail.adjust(2)
    b.attach(tail)
    return b.as_markup()


def workers_kb(workers, offset, total):
    if not workers:
        return None
    b = InlineKeyboardBuilder()
    for w in workers:
        mark = "" if w.is_active else "🚫 "
        b.button(text=f"{mark}{w.name}", callback_data=f"wcd:{w.id}")
    b.adjust(1)
    page_row(b, "wlp", offset, total)
    return b.as_markup()


def worker_card_kb(worker):
    b = InlineKeyboardBuilder()
    b.button(text="⚙️ Изменить лимит", callback_data=f"wlm:{worker.id}")
    b.button(
        text="✅ Включить" if not worker.is_active else "🚫 Отключить",
        callback_data=f"wof:{worker.id}",
    )
    b.button(text="🗑 Удалить", callback_data=f"wdl:{worker.id}")
    b.button(text="⬅️ К списку", callback_data="wlp:0")
    b.adjust(1)
    return b.as_markup()


def worker_delete_kb(worker_id):
    b = InlineKeyboardBuilder()
    b.button(text="🗑 Да, удалить", callback_data=f"wdy:{worker_id}")
    b.button(text="⬅️ Отмена", callback_data=f"wcd:{worker_id}")
    b.adjust(1)
    return b.as_markup()


def page_row(b: InlineKeyboardBuilder, pfx, offset, total):
    row = InlineKeyboardBuilder()
    if offset > 0:
        row.button(text="⬅️", callback_data=f"{pfx}:{max(0, offset - config.PAGE_SIZE)}")
    if offset + config.PAGE_SIZE < total:
        row.button(text="➡️", callback_data=f"{pfx}:{offset + config.PAGE_SIZE}")
    b.attach(row)


def leads_list_kb(leads, page_pfx, card_pfx, offset, total):
    if not leads:
        return None
    b = InlineKeyboardBuilder()
    for lead in leads:
        b.button(text=f"#{lead.id} {lead.name[:30]}", callback_data=f"{card_pfx}:{lead.id}")
    b.adjust(1)
    page_row(b, page_pfx, offset, total)
    return b.as_markup()


def statuses_kb(lead_id):
    b = InlineKeyboardBuilder()
    for key, label in config.STATUSES:
        b.button(text=label, callback_data=f"stv:{lead_id}:{key}")
    b.adjust(2)
    _cancel(b)
    return b.as_markup()


def edit_fields_kb(lead_id):
    b = InlineKeyboardBuilder()
    for key, label in config.EDITABLE_FIELDS:
        b.button(text=label, callback_data=f"ef:{lead_id}:{key}")
    b.button(text="Контакты", callback_data=f"ecm:{lead_id}")
    b.adjust(2)
    _cancel(b)
    return b.as_markup()


def contacts_menu_kb(lead_id, contacts):
    b = InlineKeyboardBuilder()
    for c in contacts:
        label = config.CONTACT_TYPE_LABELS.get(c.ctype, c.ctype)
        if c.ctype == "other" and c.ctype_other:
            label = c.ctype_other
        b.button(text=f"✏️ {label}: {c.value[:20]}", callback_data=f"ece:{c.id}")
        b.button(text="🗑", callback_data=f"ecd:{c.id}")
    b.adjust(2)
    tail = InlineKeyboardBuilder()
    tail.button(text="➕ Добавить контакт", callback_data=f"eca:{lead_id}")
    _cancel(tail)
    tail.adjust(1)
    b.attach(tail)
    return b.as_markup()
