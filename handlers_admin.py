import asyncio
import csv
import logging
import os
import tempfile
from datetime import datetime
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message
from sqlalchemy import and_, case, func, select, update
from sqlalchemy.exc import IntegrityError

import config
import costs
import draft_service
import email_gen
import keyboards as kb
import notify
import phrases
import queue_service
import services
from handlers_worker import (
    STALE, cb_id, edits_count, esc, fmt_lead, get_contacts, is_url, list_text,
    local, outcome_line, pick, safe_edit, send_screenshot, status_counts,
)
from models import (
    COMMISSION_MAX, COMMISSION_MIN, ClientService, CommissionChange, Contact,
    CostLedger, Lead, LeadEvent, Sale, Session, SuppressionEvent, Worker,
    commission_due, day_start, log_event, month_start, suppress_lead,
)
from scout.niches import NICHE_TAGS
from scout.runner import run_scout, run_scout_paste, scout_busy

log = logging.getLogger(__name__)
router = Router()

DATE_PRESETS = [("Сегодня", 0), ("7 дней", 6), ("30 дней", 29)]
ACTIVE = (Lead.cancelled_at.is_(None), Lead.deleted_at.is_(None))
# Строка админа в workers нужна его же лидам как автор, но управлять админом как
# работником нельзя: отключить, удалить или разослать самому себе — бессмыслица.
# В статистике по работникам и в фильтре «Работник» он при этом остаётся —
# иначе свои компании не найти. Админов может быть двое (6.16), и второй так же
# не работник.
NOT_ADMIN = Worker.tg_id.not_in(config.ADMIN_IDS)
# «у лида есть живой email» одним EXISTS — так же, как это считает 7.19 в SQL
HAS_EMAIL = (
    select(Contact.id).where(
        Contact.lead_id == Lead.id, Contact.ctype == "email",
        Contact.deleted_at.is_(None), Contact.lead_cancelled_at.is_(None),
    ).exists()
)


async def guard_admin_row(cb: CallbackQuery, wid: int) -> bool:
    """False — это строка админа либо строки нет; управлять ею нельзя.

    В список работников админ не попадает, но старое сообщение с кнопками живёт
    в чате вечно, а с карточки доступны «Отключить», «Удалить» и смена лимита.
    """
    async with Session() as s:
        worker = await s.get(Worker, wid)
    if worker is None:
        await cb.answer("Работник не найден", show_alert=True)
        return False
    if config.is_admin(worker.tg_id):
        await cb.answer("Это строка админа, а не работник", show_alert=True)
        return False
    return True


class Adm(StatesGroup):
    search = State()
    note = State()
    draft = State()
    limit = State()
    commission = State()
    sale = State()
    broadcast = State()
    letter = State()


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Админ-панель", reply_markup=kb.admin_menu())


# --- статистика --------------------------------------------------------------

@router.message(F.text == kb.BTN_A_STATS)
async def stats(message: Message, state: FSMContext):
    # кнопка нижнего меню прерывает форму добавления: без сброса состояния
    # следующее сообщение ушло бы в брошенную форму. set_state(None), а не
    # clear(): в тех же данных лежат фильтры списка
    await state.set_state(None)
    async with Session() as s:
        total = await s.scalar(select(func.count()).select_from(Lead).where(*ACTIVE))
        today = await s.scalar(
            select(func.count()).select_from(Lead)
            .where(*ACTIVE, Lead.created_at >= day_start())
        )
        week = await s.scalar(
            select(func.count()).select_from(Lead)
            .where(*ACTIVE, Lead.created_at >= day_start(6))
        )
        rows = await s.execute(
            select(Worker.name, func.count(Lead.id))
            .join(Lead, Lead.worker_id == Worker.id)
            .where(*ACTIVE)
            # по id, а не по имени: двух работников-тёзок группировка
            # по имени сливала в одну строку
            .group_by(Worker.id, Worker.name)
            .order_by(func.count(Lead.id).desc())
        )
        verified = await s.scalar(
            select(func.count()).select_from(Lead)
            .where(*ACTIVE, Lead.status == "verified")
        )
        with_email = await s.scalar(
            select(func.count()).select_from(Lead)
            .where(*ACTIVE, Lead.status == "verified", HAS_EMAIL)
        )
        by_status = await status_counts(s, ACTIVE)
    lines = [f"Всего: {total}", f"За сегодня: {today}", f"За 7 дней: {week}", "", "По работникам:"]
    lines += [f"  {esc(name)}: {cnt}" for name, cnt in rows] or ["  —"]
    # 7.19: без доли лидов с email не видно главной утечки воронки — письмо
    # некуда отправить; пара replied/replied_interested показывает, работает ли
    # само письмо, а не только доставка
    share = f" ({round(100 * with_email / verified)}%)" if verified else ""
    lines += [
        "",
        f"Проверенных: {verified}, с email-контактом: {with_email}{share}",
        f"Ответили: {by_status.get('replied', 0)} · "
        f"заинтересованы: {by_status.get('replied_interested', 0)}",
        outcome_line(by_status),
    ]
    await message.answer("\n".join(lines))


# --- расходы на ИИ (/costs) --------------------------------------------------

COST_WINDOWS = {
    "day": ("за сегодня", lambda: day_start()),
    "week": ("за 7 дней", lambda: day_start(6)),
    "month": ("за месяц", month_start),
}


def _n(v) -> str:
    """1234567 → «1 234 567»: числа токенов без разрядов не читаются."""
    return f"{int(v):,}".replace(",", " ")


@router.message(Command("costs"))
async def costs_report(message: Message, state: FSMContext, command: CommandObject):
    # /costs [day|week|month], по умолчанию month. Роутер уже отфильтрован
    # по is_admin в main.py, отдельный гейт не нужен.
    await state.set_state(None)
    arg = (command.args or "month").strip().lower()
    if arg not in COST_WINDOWS:
        await message.answer("Формат: /costs day|week|month")
        return
    label, since_fn = COST_WINDOWS[arg]
    since = since_fn()
    async with Session() as s:
        rows = (await s.execute(
            select(
                CostLedger.op, CostLedger.model,
                func.sum(CostLedger.api_calls),
                func.sum(CostLedger.cost_usd),
                func.sum(CostLedger.input_tokens),
                func.sum(CostLedger.output_tokens),
                func.sum(CostLedger.cache_read_tokens),
            )
            .where(CostLedger.created_at >= since)
            .group_by(CostLedger.op, CostLedger.model)
            .order_by(func.sum(CostLedger.cost_usd).desc())
        )).all()
    spent_month = await costs.month_spent()
    total = sum((r[3] for r in rows), start=0)
    lines = [f"<b>💸 Расходы {label}</b> (с {local(since)})"]
    if not rows:
        lines.append("Записей нет.")
    else:
        lines.append(f"Всего: ${total:.4f}")
        for op, model, calls, usd, t_in, t_out, t_cache in rows:
            lines.append(
                f"  {esc(op)}{' · ' + esc(model) if model else ''}: ${usd:.4f} — "
                f"{_n(calls)} выз., in {_n(t_in)}, out {_n(t_out)}, кэш {_n(t_cache)}"
            )
    cap = config.AI_MONTHLY_CAP_USD
    if cap > 0:
        pct = float(spent_month) / cap * 100
        lines.append(
            f"\nМесячный кэп: ${float(spent_month):.2f} из ${cap:.2f} ({pct:.0f}%)"
        )
        if pct >= 100:
            lines.append("⛔ Кэп исчерпан — платные операции остановлены.")
    else:
        lines.append("\nМесячный кэп выключен (AI_MONTHLY_CAP_USD=0).")
    await message.answer("\n".join(lines))


# --- подписки на доп-услуги (/subs, 16.13) -----------------------------------

SUBS_USAGE = (
    "Формат:\n"
    "/subs — активные подписки и MRR\n"
    "/subs add &lt;id лида&gt; &lt;услуга&gt; &lt;цена $/мес&gt; [заметка]\n"
    "/subs cancel &lt;номер подписки&gt;\n\n"
    "Услуги: " + ", ".join(sorted(s["id"] for s in services.SERVICES))
)


@router.message(Command("subs"))
async def subs_cmd(message: Message, state: FSMContext, command: CommandObject):
    await state.set_state(None)
    args = (command.args or "").split()
    if not args:
        await _subs_list(message)
    elif args[0] == "add" and len(args) >= 4:
        await _subs_add(message, args[1], args[2], args[3],
                        " ".join(args[4:]) or None)
    elif args[0] == "cancel" and len(args) == 2:
        await _subs_cancel(message, args[1])
    else:
        await message.answer(SUBS_USAGE)


async def _subs_list(message: Message):
    async with Session() as s:
        rows = (await s.execute(
            select(ClientService, Lead.name)
            .join(Lead, Lead.id == ClientService.lead_id)
            .where(ClientService.status == "active")
            .order_by(ClientService.id)
        )).all()
    if not rows:
        await message.answer("Активных подписок нет.\n\n" + SUBS_USAGE)
        return
    lines = ["<b>📦 Подписки на доп-услуги</b>"]
    mrr = Decimal(0)
    for cs, lead_name in rows:
        mrr += cs.price_usd
        lines.append(
            f"#{cs.id} {esc(lead_name)} — {esc(cs.service_id)}, "
            f"${cs.price_usd:.2f}/мес (с {local(cs.started_at)})"
        )
    lines.append(f"\nMRR доп-услуг: <b>${mrr:.2f}/мес</b>")
    await message.answer("\n".join(lines))


async def _subs_add(message: Message, lead_id_s: str, service_id: str,
                    price_s: str, note: str | None):
    # услуга — только из каталога: services.yml единственный источник истины,
    # опечатка здесь испортила бы всю статистику по услугам
    if service_id not in {s["id"] for s in services.SERVICES}:
        await message.answer(
            f"Не знаю услугу «{esc(service_id)}».\n\n{SUBS_USAGE}")
        return
    try:
        lead_id = int(lead_id_s)
        price = Decimal(price_s)
    except (ValueError, InvalidOperation):
        await message.answer(SUBS_USAGE)
        return
    if price < 0 or price >= 10 ** 8:
        await message.answer("Цена должна быть в пределах 0–99 999 999.")
        return
    async with Session() as s, s.begin():
        lead = await s.get(Lead, lead_id)
        if lead is None or lead.deleted_at or lead.cancelled_at:
            await message.answer(f"Лид #{lead_id} не найден или удалён.")
            return
        cs = ClientService(lead_id=lead_id, service_id=service_id,
                           price_usd=price, note=note)
        s.add(cs)
        await s.flush()
        log_event(s, lead_id, "sub_add", message.from_user.id,
                  field=service_id, new=str(price))
        num, name = cs.id, lead.name
    await message.answer(
        f"✅ Подписка #{num}: {esc(name)} — {esc(service_id)}, ${price:.2f}/мес")


async def _subs_cancel(message: Message, num_s: str):
    try:
        num = int(num_s)
    except ValueError:
        await message.answer(SUBS_USAGE)
        return
    async with Session() as s, s.begin():
        cs = await s.get(ClientService, num)
        if cs is None:
            await message.answer(f"Подписки #{num} нет.")
            return
        if cs.status == "canceled":
            await message.answer(f"Подписка #{num} уже отменена.")
            return
        cs.status = "canceled"
        cs.canceled_at = func.now()
        log_event(s, cs.lead_id, "sub_cancel", message.from_user.id,
                  field=cs.service_id, old="active", new="canceled")
    await message.answer(f"✅ Подписка #{num} отменена.")


# --- стоп-лист и журнал отписок (/stop, /stops, 9.27, 9.34) ------------------

# в команде слова короткие, в базе — полные: журнал читают не только мы
STOP_EVENTS = {"unsub": "unsubscribe", "complaint": "complaint",
               "manual": "manual"}
EVENT_LABELS = {"unsubscribe": "отписка", "complaint": "жалоба",
                "bounce": "баунс", "manual": "вручную"}
STOP_USAGE = (
    "Формат:\n"
    "/stop &lt;id лида&gt; [unsub|complaint|manual] [заметка]\n"
    "/stops [сколько] — журнал отписок и жалоб\n\n"
    "Закрывает компанию целиком: адрес, домен и «имя+город». "
    "Без слова считается отпиской."
)


@router.message(Command("stop"))
async def stop_cmd(message: Message, state: FSMContext, command: CommandObject):
    await state.set_state(None)
    args = (command.args or "").split()
    if not args:
        await message.answer(STOP_USAGE)
        return
    try:
        lead_id = int(args[0])
    except ValueError:
        await message.answer(STOP_USAGE)
        return
    # слово вида события необязательно: «/stop 42 просил больше не писать»
    # должно сработать, а не показать справку — отписку принимают как есть
    event = STOP_EVENTS.get(args[1] if len(args) > 1 else "", "")
    note = " ".join(args[2:] if event else args[1:])
    event = event or "unsubscribe"
    async with Session() as s, s.begin():
        lead = await s.get(Lead, lead_id)
        if lead is None:
            await message.answer(f"Лид #{lead_id} не найден.")
            return
        # удалённый и отменённый лиды тоже закрываются: просьбу не писать
        # исполняют независимо от того, в каком состоянии у нас карточка
        added = await suppress_lead(s, lead, event=event, source="telegram",
                                    note=note or None,
                                    actor_tg_id=message.from_user.id)
        dropped = await queue_service.cancel_drafts(
            s, lead_id, message.from_user.id, note="стоп-лист")
        log_event(s, lead_id, "suppressed", message.from_user.id,
                  field=event, new=note or None)
        name = lead.name
    lines = [f"🚫 {esc(name)} — {EVENT_LABELS[event]}, писать больше не будем.",
             f"Закрыто значений: {added}." if added else "Уже был в стоп-листе."]
    if dropped:
        lines.append(f"Снято с очереди карточек: {dropped}.")
    await message.answer("\n".join(lines))


@router.message(Command("stops"))
async def stops_cmd(message: Message, state: FSMContext, command: CommandObject):
    await state.set_state(None)
    try:
        limit = max(1, min(int(command.args or 20), 50))
    except ValueError:
        await message.answer(STOP_USAGE)
        return
    async with Session() as s:
        rows = (await s.execute(
            select(SuppressionEvent, Lead.name)
            .outerjoin(Lead, Lead.id == SuppressionEvent.lead_id)
            .order_by(SuppressionEvent.id.desc())
            .limit(limit)
        )).all()
    if not rows:
        await message.answer("Отписок и жалоб не было.\n\n" + STOP_USAGE)
        return
    lines = ["<b>🚫 Журнал отписок и жалоб</b>"]
    for ev, lead_name in rows:
        tail = f" — {esc(ev.note)}" if ev.note else ""
        lines.append(
            f"{local(ev.created_at)} · {EVENT_LABELS.get(ev.event, ev.event)} · "
            f"{esc(lead_name or '—')} ({esc(ev.source or '?')}){tail}"
        )
    await message.answer("\n".join(lines))


# --- цифры клиента для формулы потерянной выручки (/numbers, 9.35) -----------

NUMBERS_USAGE = (
    "Формат: /numbers &lt;id лида&gt; &lt;пропущено обращений в месяц&gt; "
    "&lt;конверсия %&gt; &lt;средний чек [валюта]&gt;\n"
    "Пример: /numbers 42 20 25 800 UAH (без валюты — USD)\n\n"
    "Только те цифры, которые назвал сам клиент: в письмо 3 они уйдут как есть."
)


@router.message(Command("numbers"))
async def numbers_cmd(message: Message, state: FSMContext,
                      command: CommandObject):
    await state.set_state(None)
    args = (command.args or "").split()
    if len(args) not in (4, 5):
        await message.answer(NUMBERS_USAGE)
        return
    try:
        lead_id, missed, pct = int(args[0]), int(args[1]), int(args[2])
    except ValueError:
        await message.answer(NUMBERS_USAGE)
        return
    deal = parse_deal(" ".join(args[3:]))
    if deal is None or missed <= 0 or not 0 < pct <= 100:
        await message.answer("Пропущенных — больше нуля, конверсия — 1–100, "
                             "чек — сумма вида «800» или «800 UAH».")
        return
    ticket, currency = deal
    async with Session() as s, s.begin():
        lead = await s.get(Lead, lead_id)
        if lead is None or lead.deleted_at:
            await message.answer(f"Лид #{lead_id} не найден или удалён.")
            return
        # JSONB меняем присваиванием: правку словаря на месте SQLAlchemy не
        # заметит и в базу ничего не уйдёт
        lead.enrichment = dict(lead.enrichment or {}) | {
            email_gen.LOSS_KEY: {
                "missed_per_month": missed, "conversion_pct": pct,
                "avg_ticket": str(ticket), "currency": currency,
            },
        }
        log_event(s, lead_id, "client_numbers", message.from_user.id,
                  new=f"{missed} / {pct}% / {ticket} {currency}")
        lang = phrases.lang_of(lead)
        line = email_gen.lost_revenue(lead, lang) if lang else ""
    answer = f"✅ Цифры лида #{lead_id} записаны."
    if line:
        answer += f"\n\nВ письмо 3 пойдёт строка:\n{esc(line)}"
    await message.answer(answer)


# --- лид-скаут (/scout, /scout_paste) ----------------------------------------

# фоновые задачи держим за ссылку: без неё сборщик мусора вправе убить
# задачу на середине прогона
_bg_tasks: set = set()


def _spawn(coro):
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


SCOUT_USAGE = (
    "Формат: /scout <страна> <ниша> <город>\n"
    "Пример: /scout Словакия Стоматология Košice\n"
    f"Ниши: {', '.join(NICHE_TAGS)}\n"
    "Город — как он назван в OpenStreetMap (обычно на местном языке)."
)


def _parse_scout_args(args: str) -> tuple[str, str, str] | None:
    """«<страна> <ниша> <город>» — ниша может быть из двух слов,
    поэтому матчим её по каталогу, а не сплитом по пробелам."""
    parts = args.strip().split(maxsplit=1)
    if len(parts) < 2 or parts[0] not in config.COUNTRY_ISO:
        return None
    country, rest = parts[0], parts[1].strip()
    for niche in sorted(NICHE_TAGS, key=len, reverse=True):
        if rest.lower().startswith(niche.lower()):
            city = rest[len(niche):].strip()
            if city:
                return country, niche, city
    return None


@router.message(Command("scout"))
async def scout_cmd(message: Message, state: FSMContext, command: CommandObject):
    await state.set_state(None)
    parsed = _parse_scout_args(command.args or "")
    if parsed is None:
        await message.answer(SCOUT_USAGE)
        return
    if scout_busy():
        await message.answer("Скаут уже работает — дождитесь дайджеста.")
        return
    country, niche, city = parsed
    _spawn(run_scout(message.bot, message.chat.id, country, niche, city))
    await message.answer(
        f"🔭 Скаут запущен: {niche}, {city} ({country}). Дайджест пришлю сюда."
    )


SCOUT_PASTE_USAGE = (
    "Формат:\n/scout_paste <страна> <ниша>\n"
    "со второй строки — домены через пробел или перенос строки.\n"
    "Пример:\n/scout_paste Словакия Стоматология\nzubar-ke.sk dental-x.sk\n\n"
    "Домены берутся руками из adstransparency.google.com — автоскрейпинг "
    "нарушает ToS Google."
)
SCOUT_PASTE_MAX = 50


@router.message(Command("scout_paste"))
async def scout_paste_cmd(message: Message, state: FSMContext,
                          command: CommandObject):
    await state.set_state(None)
    args = command.args or ""
    head, _, tail = args.partition("\n")
    parsed = _parse_scout_args(head + " —")  # «город» не нужен — добиваем заглушкой
    domains = [d for d in tail.split() if "." in d]
    if parsed is None or not domains:
        await message.answer(SCOUT_PASTE_USAGE)
        return
    if len(domains) > SCOUT_PASTE_MAX:
        await message.answer(f"Не больше {SCOUT_PASTE_MAX} доменов за раз.")
        return
    if scout_busy():
        await message.answer("Скаут уже работает — дождитесь дайджеста.")
        return
    country, niche, _ = parsed
    _spawn(run_scout_paste(message.bot, message.chat.id, country, niche, domains))
    await message.answer(
        f"🔭 Принято доменов: {len(domains)} ({niche}, has_ads). "
        "Дайджест пришлю сюда."
    )


# --- фильтры и список --------------------------------------------------------

def flt_conditions(flt: dict):
    conds = list(ACTIVE)
    if flt.get("worker_id"):
        conds.append(Lead.worker_id == flt["worker_id"])
    if flt.get("country"):
        conds.append(Lead.country == flt["country"])
    if flt.get("niche"):
        conds.append(Lead.niche == flt["niche"])
    if flt.get("status"):
        conds.append(Lead.status == flt["status"])
    if flt.get("days") is not None:
        conds.append(Lead.created_at >= day_start(flt["days"]))
    return conds


def flt_text(flt: dict) -> str:
    return (
        "<b>Фильтры</b>\n"
        f"Работник: {esc(flt.get('worker_name') or 'любой')}\n"
        f"Страна: {esc(flt.get('country') or 'любая')}\n"
        f"Ниша: {esc(flt.get('niche') or 'любая')}\n"
        f"Статус: {esc(config.STATUS_LABELS.get(flt.get('status'), 'любой'))}\n"
        f"Дата: {esc(flt.get('days_label') or 'любая')}"
    )


async def get_flt(state: FSMContext) -> dict:
    return (await state.get_data()).get("flt", {})


@router.message(F.text == kb.BTN_A_ALL)
async def all_leads(message: Message, state: FSMContext):
    await state.set_state(None)
    flt = await get_flt(state)
    await message.answer(flt_text(flt), reply_markup=kb.filters_kb())


@router.callback_query(F.data.startswith("afk:"))
async def filter_menu(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    await cb.answer()
    flt = await get_flt(state)
    if key == "reset":
        await state.update_data(flt={})
        await safe_edit(cb.message, flt_text({}), kb.filters_kb())
        return
    if key == "back":
        await safe_edit(cb.message, flt_text(flt), kb.filters_kb())
        return
    if key == "worker":
        async with Session() as s:
            workers = list(await s.scalars(
                select(Worker).where(Worker.deleted_at.is_(None)).order_by(Worker.name)
            ))
        await state.update_data(opts=[w.id for w in workers],
                                opt_names=[w.name for w in workers])
        await safe_edit(
            cb.message, "Работник:", kb.options_kb([w.name for w in workers], "afw")
        )
        return
    if key in ("country", "niche"):
        col = Lead.country if key == "country" else Lead.niche
        async with Session() as s:
            values = [v for v in await s.scalars(
                select(col).where(*ACTIVE).distinct().order_by(col)
            )]
        await state.update_data(opts=values)
        pfx = "afc" if key == "country" else "afn"
        await safe_edit(
            cb.message,
            "Страна:" if key == "country" else "Ниша:",
            kb.options_kb(values, pfx),
        )
        return
    if key == "status":
        await safe_edit(
            cb.message, "Статус:",
            kb.options_kb([lbl for _, lbl in config.STATUSES], "afs"),
        )
        return
    if key == "date":
        await safe_edit(
            cb.message, "Дата:",
            kb.options_kb([lbl for lbl, _ in DATE_PRESETS], "afd"),
        )


async def _set_filter(cb: CallbackQuery, state: FSMContext, **values):
    flt = await get_flt(state)
    flt.update(values)
    await state.update_data(flt=flt)
    await cb.answer()
    await safe_edit(cb.message, flt_text(flt), kb.filters_kb())


@router.callback_query(F.data.startswith("afw:"))
async def filter_worker(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    if key == "any":
        await _set_filter(cb, state, worker_id=None, worker_name=None)
        return
    d = await state.get_data()
    wid = pick(d.get("opts", []), key)
    if wid is None:
        await cb.answer(STALE, show_alert=True)
        return
    await _set_filter(
        cb, state, worker_id=wid, worker_name=pick(d.get("opt_names", []), key)
    )


@router.callback_query(F.data.startswith("afc:"))
async def filter_country(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    if key == "any":
        await _set_filter(cb, state, country=None)
        return
    d = await state.get_data()
    val = pick(d.get("opts", []), key)
    if val is None:
        await cb.answer(STALE, show_alert=True)
        return
    await _set_filter(cb, state, country=val)


@router.callback_query(F.data.startswith("afn:"))
async def filter_niche(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    if key == "any":
        await _set_filter(cb, state, niche=None)
        return
    d = await state.get_data()
    val = pick(d.get("opts", []), key)
    if val is None:
        await cb.answer(STALE, show_alert=True)
        return
    await _set_filter(cb, state, niche=val)


@router.callback_query(F.data.startswith("afs:"))
async def filter_status(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    if key == "any":
        await _set_filter(cb, state, status=None)
        return
    pair = pick(config.STATUSES, key)
    if pair is None:
        await cb.answer(STALE, show_alert=True)
        return
    await _set_filter(cb, state, status=pair[0])


@router.callback_query(F.data.startswith("afd:"))
async def filter_date(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    if key == "any":
        await _set_filter(cb, state, days=None, days_label=None)
        return
    preset = pick(DATE_PRESETS, key)
    if preset is None:
        await cb.answer(STALE, show_alert=True)
        return
    label, days = preset
    await _set_filter(cb, state, days=days, days_label=label)


async def page(session, conds, offset):
    total = await session.scalar(select(func.count()).select_from(Lead).where(*conds))
    rows = await session.scalars(
        select(Lead).where(*conds).order_by(Lead.id.desc())
        .offset(offset).limit(config.PAGE_SIZE)
    )
    return list(rows), total


@router.callback_query(F.data.startswith("alp:"))
async def all_page(cb: CallbackQuery, state: FSMContext):
    offset = await cb_id(cb)
    if offset is None:
        return
    conds = flt_conditions(await get_flt(state))
    async with Session() as s:
        leads, total = await page(s, conds, offset)
    await cb.answer()
    await safe_edit(
        cb.message,
        list_text(leads, total, offset),
        kb.leads_list_kb(leads, "alp", "acd", offset, total),
    )


@router.message(F.text == kb.BTN_MY)
async def my_leads(message: Message, state: FSMContext, worker: Worker):
    """Свои компании — тем же списком и той же карточкой, что и чужие.

    Работницкий «Мои компании» админу не подошёл бы: там нет смены статуса,
    а именно она и нужна — утвердить то, что сам занёс. Фильтр кладётся
    в состояние, поэтому пагинация «alp:» продолжает работать по нему же.
    """
    await state.set_state(None)
    flt = {"worker_id": worker.id, "worker_name": worker.name}
    await state.update_data(flt=flt)
    async with Session() as s:
        leads, total = await page(s, flt_conditions(flt), 0)
    await message.answer(
        list_text(leads, total, 0),
        reply_markup=kb.leads_list_kb(leads, "alp", "acd", 0, total),
    )


# --- карточка ----------------------------------------------------------------

async def show_card(target: Message, lead_id: int):
    async with Session() as s:
        lead = await s.get(Lead, lead_id)
        # deleted_at: запись нигде не показывается, но кнопка на неё могла
        # остаться в старом сообщении
        if not lead or lead.deleted_at:
            await target.answer("Запись не найдена.")
            return
        contacts = await get_contacts(s, lead_id)
        author = await s.get(Worker, lead.worker_id)
        edits = await edits_count(s, lead_id)
    shown = await send_screenshot(target, lead.screenshot_file_id)
    markup = (
        kb.cancelled_card_kb(lead_id) if lead.cancelled_at
        else kb.admin_card_kb(lead_id, can_build=lead.status == "verified")
    )
    text = fmt_lead(lead, contacts, author=author, edits=edits, admin=True)
    if not shown:
        text += '\n📷 Скриншот недоступен (сохранён прежним ботом).'
    await target.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("acd:"))
async def admin_card(cb: CallbackQuery):
    lead_id = await cb_id(cb)
    if lead_id is None:
        return
    await cb.answer()
    await show_card(cb.message, lead_id)


@router.callback_query(F.data.startswith("ups:"))
async def upsell_menu(cb: CallbackQuery):
    """«Что допродать» (16.6): топ-3 услуги под факты лида, без ИИ."""
    lead_id = await cb_id(cb)
    if lead_id is None:
        return
    async with Session() as s:
        lead = await s.get(Lead, lead_id)
    if not lead or lead.deleted_at:
        await cb.answer(STALE, show_alert=True)
        return
    await cb.answer()
    recs = services.recommend(lead)
    if not recs:
        await cb.message.answer("Подходящих доп-услуг под этого лида не нашлось.")
        return
    lines = [f"<b>💡 Что допродать: {esc(lead.name)}</b>"]
    for i, r in enumerate(recs, 1):
        svc = r["svc"]
        mark = " — ⚠️ pilot, только дружественным" if svc["status"] == "pilot" else ""
        lines += [
            "",
            f"{i}. <b>{esc(svc['name'])}</b>{mark}",
            f"Цена: {esc(svc['price'])} · себестоимость {esc(svc['cogs'])}",
            f"Почему: {esc('; '.join(r['why']))}",
            f"✉️ {esc(svc['pitch_en'])}",
        ]
    lines += [
        "",
        "<i>Порядок (16.7): сайт → подписка в момент продажи → подписка "
        "повторно после оплаты → доп-услуги после запуска. Активные "
        "допродажи — после 3 продаж сайтов (16.4).</i>",
    ]
    await cb.message.answer("\n".join(lines))


@router.callback_query(F.data.startswith("sts:"))
async def status_menu(cb: CallbackQuery):
    lead_id = await cb_id(cb)
    if lead_id is None:
        return
    await cb.answer()
    await cb.message.answer("Новый статус:", reply_markup=kb.statuses_kb(lead_id))


@router.callback_query(F.data.startswith("stv:"))
async def status_set(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    if len(parts) != 3 or parts[2] not in config.STATUS_LABELS:
        await cb.answer("Неизвестный статус", show_alert=True)
        return
    lead_id = await cb_id(cb)
    if lead_id is None:
        return
    # 6.17: «Отклонён» без причины работнику ничего не объясняет, поэтому
    # причина спрашивается до записи статуса, а не после
    if parts[2] == "rejected":
        await cb.answer()
        await cb.message.answer("Почему отклоняем?",
                                reply_markup=kb.reject_reasons_kb(lead_id))
        return
    await _apply_status(cb, state, lead_id, parts[2])


@router.callback_query(F.data.startswith("rjr:"))
async def reject_reason(cb: CallbackQuery, state: FSMContext):
    parts = (cb.data or "").split(":")
    lead_id = await cb_id(cb)
    if lead_id is None:
        return
    if len(parts) != 3 or parts[2] not in config.LEAD_REJECT_LABELS:
        await cb.answer("Неизвестная причина", show_alert=True)
        return
    await _apply_status(cb, state, lead_id, "rejected", parts[2])


async def _apply_status(cb: CallbackQuery, state: FSMContext, lead_id: int,
                        new: str, reason: str | None = None):
    async with Session() as s, s.begin():
        lead = await s.get(Lead, lead_id)
        if not lead or lead.deleted_at:
            await cb.answer("Запись недоступна", show_alert=True)
            return
        # то же самое стоит CHECK-констрейнтом в базе, но человеку нужен не
        # IntegrityError, а понятная причина и кнопка, которой это чинится
        if new == "verified" and not lead.gap_type:
            await cb.answer()
            await cb.message.answer(
                f"Сначала сними наблюдение по #{lead_id}: без него лид "
                "непригоден для письма.",
                reply_markup=kb.regap_kb(lead_id),
            )
            return
        old = lead.status
        lead.status = new
        lead.reject_reason = reason
        worker = await s.get(Worker, lead.worker_id)
        log_event(s, lead_id, "status_change", cb.from_user.id, "status", old, new)
        if reason:
            log_event(s, lead_id, "reject_reason", cb.from_user.id,
                      "reject_reason", None, reason)
        # автостоп цепочки (решение 5 этапа): ответ, продажа, отказ и
        # отклонение снимают с очереди всё, что по этому лиду ещё не решено
        if new in queue_service.STOP_LEAD_STATUSES:
            await queue_service.cancel_drafts(s, lead_id, cb.from_user.id,
                                              f"статус {new}")
    log.info("lead %s status %s -> %s", lead_id, old, new)
    await cb.answer("Статус изменён")
    text = (f"#{lead_id}: {config.STATUS_LABELS.get(old, old)} → "
            f"{config.STATUS_LABELS.get(new, new)}")
    if reason:
        text += f"\nПричина: {config.LEAD_REJECT_LABELS[reason]}"
    await cb.message.answer(text)
    # 6.14: решение по чужому лиду видит только админ — работнику о нём
    # сообщает бот, иначе о своей же продаже он узнаёт последним.
    # sold — единственный статус, о котором здесь молчим: о продаже работнику
    # пишет _notify_sale, и уже с суммой и комиссией. Два сообщения об одном
    # событии превращают уведомления в шум, который перестают читать.
    if new != "sold":
        await notify.lead_status(cb.bot, lead, worker, old, new, reason=reason,
                                 actor_tg_id=cb.from_user.id)
    else:
        await _ask_sale(cb.message, state, lead_id)


# --- продажа и начисление (7.12–7.15) ----------------------------------------
#
# Продажа записывается ровно один раз на лид: повторный перевод в sold не
# начисляет работнику второй раз за ту же сделку (UNIQUE lead_id). Процент
# копируется в строку продажи на этот момент — последующая смена комиссии
# работнику старые начисления не переписывает (7.13).

MAX_DEAL_AMOUNT = 10 ** 8  # столько не влезет в Numeric(10,2)


def parse_deal(raw: str) -> tuple[Decimal, str] | None:
    """«120» или «120 EUR» → (сумма, валюта). None — ввод не разобран."""
    parts = raw.replace(",", ".").split()
    if not 1 <= len(parts) <= 2:
        return None
    try:
        amount = Decimal(parts[0])
    except InvalidOperation:
        return None
    # is_finite отсекает «NaN» и «Infinity»: Decimal их принимает молча
    if not amount.is_finite() or not 0 < amount < MAX_DEAL_AMOUNT:
        return None
    if -amount.as_tuple().exponent > 2:
        return None
    currency = parts[1].upper() if len(parts) == 2 else "USD"
    if not (currency.isascii() and currency.isalpha() and len(currency) <= 8):
        return None
    return amount, currency


async def _ask_sale(target: Message, state: FSMContext, lead_id: int):
    async with Session() as s:
        sale = await s.scalar(select(Sale).where(Sale.lead_id == lead_id))
    if sale is not None:
        await target.answer(
            f"Продажа по #{lead_id} уже записана: "
            f"{sale.deal_amount:.2f} {esc(sale.currency)}, начисление "
            f"{sale.amount_due:.2f} {esc(sale.currency)}."
        )
        return
    await state.set_state(Adm.sale)
    await state.update_data(lead_id=lead_id)
    await target.answer(
        f"Сумма сделки по #{lead_id}: например «120» или «120 EUR» "
        "(без валюты — USD).",
        reply_markup=kb.cancel_kb(),
    )


@router.message(Adm.sale)
async def sale_save(message: Message, state: FSMContext):
    deal = parse_deal(message.text or "")
    if deal is None:
        await message.answer(
            "Нужна сумма больше нуля, максимум два знака после запятой: "
            "«120» или «120 EUR».",
            reply_markup=kb.cancel_kb(),
        )
        return
    amount, currency = deal
    d = await state.get_data()
    await state.clear()
    lead_id = d.get("lead_id", 0)
    async with Session() as s:
        lead = await s.get(Lead, lead_id)
        worker = await s.get(Worker, lead.worker_id) if lead else None
    if lead is None or lead.deleted_at or worker is None:
        await message.answer("Лид недоступен, продажа не записана.")
        return
    rate = worker.commission_pct
    due = commission_due(amount, rate)
    try:
        async with Session() as s, s.begin():
            s.add(Sale(lead_id=lead_id, worker_id=worker.id, deal_amount=amount,
                       currency=currency, rate_pct=rate, amount_due=due))
            log_event(s, lead_id, "sale_add", message.from_user.id,
                      field="deal_amount", new=f"{amount} {currency}")
    except IntegrityError:
        # uq_sales_lead: второе «Продано» по тому же лиду успело записаться
        await message.answer(f"Продажа по #{lead_id} уже записана.")
        return
    log.info("sale on lead %s: %s %s at %s%%", lead_id, amount, currency, rate)
    await message.answer(
        f"💰 Продажа по #{lead_id} записана: {amount:.2f} {esc(currency)}.\n"
        f"Начисление {esc(worker.name)}: {rate}% — {due:.2f} {esc(currency)}.\n"
        "Подтвердится отметкой «Деньги пришли» в разделе «Начисления»."
    )
    await _notify_sale(message, lead, worker, amount, currency, rate, due,
                       message.from_user.id)


async def _notify_sale(target: Message, lead, worker, amount, currency,
                       rate, due, actor_tg_id: int):
    """7.15: работник узнаёт о своей продаже сразу — и сразу же честно о том,
    что деньги считаются начисленными только после оплаты клиентом."""
    # те же три условия, что и в notify.lead_status: отстранённому работнику
    # бот не пишет, даже когда его старый лид дошёл до продажи
    if worker is None or worker.deleted_at or not worker.is_active:
        return
    if worker.tg_id == actor_tg_id:
        return  # свою же продажу автор только что увидел ответом на ввод
    text = (f"💰 Продажа по #{lead.id} {esc(lead.name)}: "
            f"{amount:.2f} {esc(currency)}.\n"
            f"Твоя комиссия {rate}% — {due:.2f} {esc(currency)}.\n"
            "Начисление подтвердится после того, как клиент оплатит счёт.")
    try:
        await target.bot.send_message(worker.tg_id, text)
    except Exception as e:
        log.warning("новость о продаже не дошла до tg=%s: %s", worker.tg_id, e)


@router.callback_query(F.data.startswith("hst:"))
async def history(cb: CallbackQuery):
    lead_id = await cb_id(cb)
    if lead_id is None:
        return
    async with Session() as s:
        lead = await s.get(Lead, lead_id)
        # 6.10: кнопка «История» могла остаться в старом сообщении — удалённый
        # лид нигде не показывается, и его историю показывать тоже нечего
        if not lead or lead.deleted_at:
            await cb.answer(STALE, show_alert=True)
            return
        events = list(await s.scalars(
            select(LeadEvent).where(LeadEvent.lead_id == lead_id)
            .order_by(LeadEvent.id.desc()).limit(30)
        ))
    await cb.answer()
    if not events:
        await cb.message.answer("Изменений не было.")
        return
    lines = [f"<b>История #{lead_id}</b>"]
    for e in events:
        part = f"{local(e.created_at)} — {esc(e.event)}"
        if e.field:
            part += f" ({esc(e.field)})"
        if e.old_value or e.new_value:
            part += f": {esc(e.old_value)} → {esc(e.new_value)}"
        lines.append(part + f" — tg {e.actor_tg_id}")
    await cb.message.answer("\n".join(lines))


@router.callback_query(F.data.startswith("drf:"))
async def draft_ask(cb: CallbackQuery, state: FSMContext):
    lead_id = await cb_id(cb)
    if lead_id is None:
        return
    await state.set_state(Adm.draft)
    await state.update_data(lead_id=lead_id)
    await cb.answer()
    await cb.message.answer("Ссылка на черновик:", reply_markup=kb.cancel_kb())


@router.message(Adm.draft)
async def draft_save(message: Message, state: FSMContext):
    val = (message.text or "").strip()
    if not is_url(val):
        await message.answer("Нужна ссылка http(s)://…", reply_markup=kb.cancel_kb())
        return
    d = await state.get_data()
    async with Session() as s, s.begin():
        lead = await s.get(Lead, d.get("lead_id", 0))
        if lead is None or lead.deleted_at:
            await state.clear()
            await message.answer("Запись недоступна.")
            return
        lead.draft_url = val
    await state.clear()
    await message.answer("Ссылка сохранена.")


# --- сборка черновика сайта ---------------------------------------------------
#
# Сборка идёт обычным await: она занимает секунды (один вызов модели на все
# слоты), а фоновая задача с отдельным сообщением об итоге стоила бы отдельного
# состояния и живущего между деплоями таск-реестра.

@router.callback_query(F.data.startswith("bdr:"))
async def build_draft_run(cb: CallbackQuery):
    lead_id = await cb_id(cb)
    if lead_id is None:
        return
    lead = await _buildable(cb, lead_id, "Черновик собирается только "
                                         "по проверенному лиду")
    if lead is None:
        return
    await cb.answer()
    note = await cb.message.answer("Собираю черновик…")
    result = await draft_service.build_draft(lead_id,
                                             actor_tg_id=cb.from_user.id)
    await safe_edit(note, draft_report(result))
    if result.notify_tg_id:
        await _ask_enrichment(cb, lead, result)


def draft_report(result) -> str:
    if result.ok:
        return (f"🧱 Черновик собран: {esc(result.summary)}.\n"
                "Публикация превью — руками, после деплоя Worker.")
    if result.needs_enrichment:
        missing = esc("\n".join(result.missing))
        return f"📋 Черновик не собрать, не хватает данных:\n{missing}"
    return f"Не собралось: {esc(result.reason)}"


async def _ask_enrichment(cb: CallbackQuery, lead, result):
    """Петля вверх по течению: дозаполняет карточку тот, кто её и завёл."""
    missing = esc("\n".join(result.missing))
    text = (f"📋 #{lead.id} {esc(lead.name)}: для черновика сайта не хватает "
            f"данных.\n{missing}\n"
            "Пришли их админу — отдельная форма появится позже.")
    try:
        await cb.bot.send_message(result.notify_tg_id, text)
    except Exception as e:
        log.warning("просьба дозаполнить не дошла до tg=%s: %s",
                    result.notify_tg_id, e)


# --- сборка письма в очередь --------------------------------------------------
#
# Описание черновика берётся из собранного черновика лида (draft_summary), и
# только если черновика нет, админ вводит его руками: именно из этой строки
# модель берёт ту единственную конкретную вещь, которую называет в offer, и
# выдумать её за админа код не имеет права.

SUMMARY_MIN, SUMMARY_MAX = 3, 14


@router.callback_query(F.data.startswith("bld:"))
async def build_letter_ask(cb: CallbackQuery, state: FSMContext):
    lead_id = await cb_id(cb)
    if lead_id is None:
        return
    lead = await _buildable(cb, lead_id, "Письмо собирается только "
                                         "по проверенному лиду")
    if lead is None:
        return
    summary = await draft_service.summary_for(lead, phrases.lang_of(lead))
    if summary:
        await cb.answer()
        note = await cb.message.answer("Собираю письмо…")
        await safe_edit(note, letter_report(await queue_service.enqueue(
            lead_id, actor_tg_id=cb.from_user.id, draft_summary=summary,
        )))
        return
    await state.set_state(Adm.letter)
    await state.update_data(lead_id=lead_id)
    await cb.answer()
    await cb.message.answer(
        f"Что в черновике главной? {SUMMARY_MIN}–{SUMMARY_MAX} слов, "
        "только то, что там действительно есть.\n"
        "Например: одна страница, таблица цен, форма записи.",
        reply_markup=kb.cancel_kb(),
    )


@router.message(Adm.letter)
async def build_letter_run(message: Message, state: FSMContext):
    summary = (message.text or "").strip()
    if not SUMMARY_MIN <= len(summary.split()) <= SUMMARY_MAX:
        await message.answer(f"Нужно {SUMMARY_MIN}–{SUMMARY_MAX} слов "
                             "про то, что в черновике.",
                             reply_markup=kb.cancel_kb())
        return
    d = await state.get_data()
    await state.clear()
    await message.answer("Собираю письмо…")
    await message.answer(letter_report(await queue_service.enqueue(
        d.get("lead_id", 0), actor_tg_id=message.from_user.id,
        draft_summary=summary,
    )))


def letter_report(result) -> str:
    if result.ok:
        return "✉️ Письмо в очереди. Разобрать — /queue"
    tail = "\nПисьмо придётся написать руками." if result.manual else ""
    return f"Не собралось: {esc(result.reason)}{tail}"


async def _buildable(cb: CallbackQuery, lead_id: int, refusal: str):
    """Лид, по которому можно собирать. None — уже ответили отказом."""
    async with Session() as s:
        lead = await s.get(Lead, lead_id)
    if not lead or lead.deleted_at or lead.cancelled_at:
        await cb.answer(STALE, show_alert=True)
        return None
    if lead.status != "verified":
        await cb.answer(refusal, show_alert=True)
        return None
    return lead


@router.callback_query(F.data.startswith("anz:"))
async def note_ask(cb: CallbackQuery, state: FSMContext):
    lead_id = await cb_id(cb)
    if lead_id is None:
        return
    await state.set_state(Adm.note)
    await state.update_data(lead_id=lead_id)
    await cb.answer()
    await cb.message.answer("Текст заметки:", reply_markup=kb.cancel_kb())


@router.message(Adm.note)
async def note_save(message: Message, state: FSMContext):
    d = await state.get_data()
    async with Session() as s, s.begin():
        lead = await s.get(Lead, d.get("lead_id", 0))
        if lead is None or lead.deleted_at:
            await state.clear()
            await message.answer("Запись недоступна.")
            return
        lead.admin_note = (message.text or "").strip() or None
    await state.clear()
    await message.answer("Заметка сохранена.")


@router.callback_query(F.data.startswith("del:"))
async def delete_lead(cb: CallbackQuery):
    lead_id = await cb_id(cb)
    if lead_id is None:
        return
    async with Session() as s, s.begin():
        lead = await s.get(Lead, lead_id)
        if not lead or lead.deleted_at:
            await cb.answer("Запись недоступна", show_alert=True)
            return
        # datetime, а не func.now(): при expire_on_commit=False в атрибуте объекта
        # осталась бы SQL-конструкция вместо даты
        now = datetime.now(config.TZ)
        lead.deleted_at = now
        await s.execute(
            update(Contact).where(Contact.lead_id == lead_id).values(deleted_at=now)
        )
        await queue_service.cancel_drafts(s, lead_id, cb.from_user.id,
                                          "лид удалён")
        log_event(s, lead_id, "delete", cb.from_user.id)
    log.info("lead %s soft-deleted", lead_id)
    await cb.answer("Удалено")
    await cb.message.answer(f"Запись #{lead_id} удалена (мягко).")


# --- поиск -------------------------------------------------------------------

@router.message(F.text == kb.BTN_A_SEARCH)
async def search_ask(message: Message, state: FSMContext):
    await state.set_state(Adm.search)  # заодно гасит форму добавления

    await message.answer("Название компании:", reply_markup=kb.cancel_kb())


@router.message(Adm.search)
async def search_run(message: Message, state: FSMContext):
    q = (message.text or "").strip()
    if not q:
        await message.answer("Введите текст:", reply_markup=kb.cancel_kb())
        return
    # set_state(None), а не clear(): в тех же данных лежат фильтры списка,
    # и поиск — не повод их терять
    await state.set_state(None)
    await state.update_data(query=q)
    await _search_page(message, q, 0)


def like_pattern(q: str) -> str:
    """% и _ из ввода — литералы, а не подстановочные знаки."""
    esc_q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc_q}%"


async def _search_page(target: Message, q: str, offset: int):
    if not q:
        await target.answer("Запрос потерялся, нажмите «🔍 Поиск» заново.")
        return
    conds = [*ACTIVE, Lead.name.ilike(like_pattern(q), escape="\\")]
    async with Session() as s:
        leads, total = await page(s, conds, offset)
    await target.answer(
        list_text(leads, total, offset),
        reply_markup=kb.leads_list_kb(leads, "spg", "acd", offset, total),
    )


@router.callback_query(F.data.startswith("spg:"))
async def search_page(cb: CallbackQuery, state: FSMContext):
    offset = await cb_id(cb)
    if offset is None:
        return
    d = await state.get_data()
    await cb.answer()
    await _search_page(cb.message, d.get("query", ""), offset)


# --- отменённые --------------------------------------------------------------

@router.message(F.text == kb.BTN_A_CANCELLED)
async def cancelled_list(message: Message, state: FSMContext):
    await state.set_state(None)
    await _cancelled_page(message, 0, edit=False)


async def _cancelled_page(target: Message, offset: int, edit: bool):
    conds = [Lead.cancelled_at.is_not(None), Lead.deleted_at.is_(None)]
    async with Session() as s:
        leads, total = await page(s, conds, offset)
    markup = kb.leads_list_kb(leads, "cxp", "acd", offset, total)
    text = list_text(leads, total, offset)
    if edit:
        await safe_edit(target, text, markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("cxp:"))
async def cancelled_page(cb: CallbackQuery):
    offset = await cb_id(cb)
    if offset is None:
        return
    await cb.answer()
    await _cancelled_page(cb.message, offset, edit=True)


@router.callback_query(F.data.startswith("rst:"))
async def restore_lead(cb: CallbackQuery):
    lead_id = await cb_id(cb)
    if lead_id is None:
        return
    try:
        async with Session() as s, s.begin():
            lead = await s.get(Lead, lead_id)
            # 6.9: у удалённого лида восстанавливать нечего — получилась бы
            # запись «активная и удалённая сразу», невидимая ни в одном списке,
            # но занимающая домен и телефоны в дедупе
            if not lead or lead.deleted_at:
                await cb.answer(STALE, show_alert=True)
                return
            if not lead.cancelled_at:
                await cb.answer("Запись не отменена", show_alert=True)
                return
            lead.cancelled_at = None
            lead.cancelled_by = None
            await s.execute(
                update(Contact).where(Contact.lead_id == lead_id)
                .values(lead_cancelled_at=None)
            )
            log_event(s, lead_id, "restore", cb.from_user.id)
    except IntegrityError as e:
        log.warning("restore conflict lead=%s: %s", lead_id, e.orig)
        await cb.answer()
        await cb.message.answer(
            "Нельзя восстановить: в базе уже есть активный дубликат."
        )
        return
    log.info("lead %s restored", lead_id)
    await cb.answer("Восстановлено")
    await cb.message.answer(f"Запись #{lead_id} восстановлена.")


# --- работники ---------------------------------------------------------------

@router.message(F.text == kb.BTN_A_WORKERS)
async def workers_list(message: Message, state: FSMContext):
    await state.set_state(None)
    await _workers_page(message, 0, edit=False)


async def _workers_page(target: Message, offset: int, edit: bool):
    async with Session() as s:
        total = await s.scalar(
            select(func.count()).select_from(Worker)
            .where(Worker.deleted_at.is_(None), NOT_ADMIN)
        )
        workers = list(await s.scalars(
            select(Worker).where(Worker.deleted_at.is_(None), NOT_ADMIN)
            .order_by(Worker.id).offset(offset).limit(config.PAGE_SIZE)
        ))
        # .all() обязателен: у Result есть .keys(), поэтому dict() принимает его
        # за мапу и пытается индексировать — «object is not subscriptable»
        counts = dict((await s.execute(
            select(Lead.worker_id, func.count(Lead.id)).where(*ACTIVE)
            .group_by(Lead.worker_id)
        )).all())
    lines = [f"Работников: {total}"] + [
        f"{esc(w.name)} — {counts.get(w.id, 0)}"
        f"{'' if w.is_active else ' (отключён)'}"
        for w in workers
    ]
    markup = kb.workers_kb(workers, offset, total)
    text = "\n".join(lines) if workers else "Пока никого."
    if edit:
        await safe_edit(target, text, markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("wlp:"))
async def workers_page(cb: CallbackQuery):
    offset = await cb_id(cb)
    if offset is None:
        return
    await cb.answer()
    await _workers_page(cb.message, offset, edit=True)


@router.callback_query(F.data.startswith("wcd:"))
async def worker_card(cb: CallbackQuery):
    wid = await cb_id(cb)
    if wid is None:
        return
    if not await guard_admin_row(cb, wid):
        return
    async with Session() as s:
        worker = await s.get(Worker, wid)
        total = await s.scalar(
            select(func.count()).select_from(Lead)
            .where(*ACTIVE, Lead.worker_id == wid)
        )
        today = await s.scalar(
            select(func.count()).select_from(Lead)
            .where(*ACTIVE, Lead.worker_id == wid, Lead.created_at >= day_start())
        )
        changes = list(await s.scalars(
            select(CommissionChange).where(CommissionChange.worker_id == wid)
            .order_by(CommissionChange.id.desc()).limit(3)
        ))
    limit = worker.daily_limit if worker.daily_limit is not None else config.DEFAULT_DAILY_LIMIT
    lines = [
        f"<b>{esc(worker.name)}</b>", f"tg id: {worker.tg_id}",
        f"Добавлено: {total}, сегодня: {today}",
        f"Лимит: {limit}"
        f"{'' if worker.daily_limit is not None else ' (по умолчанию)'}",
        f"Комиссия: {worker.commission_pct}%",
        f"Статус: {'активен' if worker.is_active else 'отключён'}",
        f'<a href="tg://user?id={worker.tg_id}">Открыть в Telegram</a>',
    ]
    if changes:
        lines.append("\nСмены комиссии:")
        lines += [f"  {local(c.created_at)}: {c.old_pct}% → {c.new_pct}% "
                  f"— tg {c.changed_by}" for c in changes]
    await cb.answer()
    await cb.message.answer("\n".join(lines),
                            reply_markup=kb.worker_card_kb(worker))


@router.callback_query(F.data.startswith("wlm:"))
async def worker_limit_ask(cb: CallbackQuery, state: FSMContext):
    wid = await cb_id(cb)
    if wid is None or not await guard_admin_row(cb, wid):
        return
    await state.set_state(Adm.limit)
    await state.update_data(worker_id=wid)
    await cb.answer()
    await cb.message.answer(
        "Новый дневной лимит (число, или 0 — вернуть значение по умолчанию):",
        reply_markup=kb.cancel_kb(),
    )


MAX_DAILY_LIMIT = 1000


@router.message(Adm.limit)
async def worker_limit_save(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    # isdecimal, а не isdigit: у isdigit истинны «²» и подобные, а int() на них падает.
    # Верхняя граница — чтобы не переполнить int4 в колонке daily_limit.
    if not raw.isdecimal() or int(raw) > MAX_DAILY_LIMIT:
        await message.answer(
            f"Нужно число от 0 до {MAX_DAILY_LIMIT}:", reply_markup=kb.cancel_kb()
        )
        return
    value = int(raw)
    d = await state.get_data()
    async with Session() as s, s.begin():
        worker = await s.get(Worker, d.get("worker_id", 0))
        if worker is None:
            await state.clear()
            await message.answer("Работник не найден.")
            return
        worker.daily_limit = value or None
    await state.clear()
    await message.answer(
        f"Лимит: {value}" if value else "Лимит сброшен на значение по умолчанию."
    )


@router.callback_query(F.data.startswith("wcm:"))
async def worker_commission_ask(cb: CallbackQuery, state: FSMContext):
    wid = await cb_id(cb)
    if wid is None or not await guard_admin_row(cb, wid):
        return
    await state.set_state(Adm.commission)
    await state.update_data(worker_id=wid)
    await cb.answer()
    await cb.message.answer(
        f"Новая комиссия работника, {COMMISSION_MIN}–{COMMISSION_MAX}%. "
        "Уже записанные продажи не изменятся — в них процент зафиксирован.",
        reply_markup=kb.cancel_kb(),
    )


@router.message(Adm.commission)
async def worker_commission_save(message: Message, state: FSMContext):
    raw = (message.text or "").strip().rstrip("%").strip()
    if not raw.isdecimal() or not COMMISSION_MIN <= int(raw) <= COMMISSION_MAX:
        await message.answer(
            f"Нужно число от {COMMISSION_MIN} до {COMMISSION_MAX}:",
            reply_markup=kb.cancel_kb(),
        )
        return
    value = int(raw)
    d = await state.get_data()
    async with Session() as s, s.begin():
        worker = await s.get(Worker, d.get("worker_id", 0))
        if worker is None:
            await state.clear()
            await message.answer("Работник не найден.")
            return
        old = worker.commission_pct
        if old != value:
            worker.commission_pct = value
            s.add(CommissionChange(worker_id=worker.id, old_pct=old,
                                   new_pct=value,
                                   changed_by=message.from_user.id))
    await state.clear()
    if old == value:
        await message.answer(f"Комиссия и так {value}%.")
        return
    log.info("worker %s commission %s%% -> %s%%", worker.id, old, value)
    await message.answer(f"Комиссия: {old}% → {value}%.")


@router.callback_query(F.data.startswith("wof:"))
async def worker_toggle(cb: CallbackQuery):
    wid = await cb_id(cb)
    if wid is None or not await guard_admin_row(cb, wid):
        return
    async with Session() as s, s.begin():
        worker = await s.get(Worker, wid)
        if worker is None:
            await cb.answer("Работник не найден", show_alert=True)
            return
        worker.is_active = not worker.is_active
        active = worker.is_active
    await cb.answer("Готово")
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("Работник включён." if active else "Работник отключён.")


@router.callback_query(F.data.startswith("wdl:"))
async def worker_delete_ask(cb: CallbackQuery):
    wid = await cb_id(cb)
    if wid is None or not await guard_admin_row(cb, wid):
        return
    await cb.answer()
    await cb.message.answer(
        "Удалить работника? Добавленные им компании останутся в базе, "
        "но доступ к боту закроется навсегда — повторно зарегистрироваться "
        "по общему коду он не сможет.",
        reply_markup=kb.worker_delete_kb(wid),
    )


@router.callback_query(F.data.startswith("wdy:"))
async def worker_delete(cb: CallbackQuery):
    wid = await cb_id(cb)
    if wid is None or not await guard_admin_row(cb, wid):
        return
    async with Session() as s, s.begin():
        worker = await s.get(Worker, wid)
        if worker is None or worker.deleted_at:
            await cb.answer("Работник не найден", show_alert=True)
            return
        worker.deleted_at = datetime.now(config.TZ)
        worker.is_active = False
        name = worker.name
    log.info("worker %s soft-deleted by tg_id=%s", wid, cb.from_user.id)
    await cb.answer("Удалено")
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(f"{esc(name)} удалён, доступ к боту закрыт.")


# --- начисления (7.14) --------------------------------------------------------
#
# Три корзины, и порядок между ними — договор с работником: пока клиент не
# заплатил, начисление только ожидается; выплатить можно лишь то, что уже
# пришло. Суммы разводятся по валютам — складывать доллары с евро нельзя.

WAITING = Sale.received_at.is_(None)
DUE = and_(Sale.received_at.isnot(None), Sale.paid_at.is_(None))
PAID = Sale.paid_at.isnot(None)


def _bucket(cond):
    return func.coalesce(func.sum(case((cond, Sale.amount_due), else_=0)), 0)


@router.message(F.text == kb.BTN_A_PAYOUTS)
async def payouts(message: Message, state: FSMContext):
    await state.set_state(None)
    await _payouts_summary(message)


@router.callback_query(F.data.startswith("pay:"))
async def payouts_back(cb: CallbackQuery):
    await cb.answer()
    await _payouts_summary(cb.message)


async def _payouts_summary(target: Message):
    async with Session() as s:
        rows = (await s.execute(
            select(Worker.id, Worker.name, Sale.currency,
                   _bucket(WAITING), _bucket(DUE), _bucket(PAID))
            .join(Sale, Sale.worker_id == Worker.id)
            .group_by(Worker.id, Worker.name, Sale.currency)
            .order_by(Worker.name, Sale.currency)
        )).all()
    if not rows:
        await target.answer("Продаж пока нет.")
        return
    lines = ["<b>💵 Начисления</b>"]
    workers = {}
    for wid, name, currency, waiting, due, paid in rows:
        workers.setdefault(wid, name)
        lines += [
            f"\n<b>{esc(name)}</b> — {esc(currency)}",
            f"  ждёт оплаты клиентом: {waiting:.2f}",
            f"  к выплате: {due:.2f}",
            f"  выплачено: {paid:.2f}",
        ]
    await target.answer("\n".join(lines),
                        reply_markup=kb.payouts_kb(workers.items()))


@router.callback_query(F.data.startswith("pwk:"))
async def payouts_worker(cb: CallbackQuery):
    wid = await cb_id(cb)
    if wid is None:
        return
    await cb.answer()
    await _payouts_worker(cb.message, wid)


async def _payouts_worker(target: Message, wid: int):
    async with Session() as s:
        worker = await s.get(Worker, wid)
        rows = (await s.execute(
            select(Sale, Lead.name).join(Lead, Lead.id == Sale.lead_id)
            .where(Sale.worker_id == wid)
            .order_by(Sale.id.desc()).limit(config.PAGE_SIZE)
        )).all()
    if worker is None:
        await target.answer("Работник не найден.")
        return
    if not rows:
        await target.answer(f"У {esc(worker.name)} продаж нет.")
        return
    lines = [f"<b>💵 {esc(worker.name)}</b>"]
    for sale, lead_name in rows:
        lines += [
            f"\n#{sale.id} · лид #{sale.lead_id} {esc(lead_name)}",
            f"  {sale.deal_amount:.2f} {esc(sale.currency)} × {sale.rate_pct}% "
            f"= {sale.amount_due:.2f} {esc(sale.currency)}",
            f"  деньги: {local(sale.received_at) if sale.received_at else '—'}"
            f" · выплата: {local(sale.paid_at) if sale.paid_at else '—'}",
        ]
    await target.answer("\n".join(lines),
                        reply_markup=kb.sales_kb([s for s, _ in rows]))


@router.callback_query(F.data.startswith("prc:"))
async def sale_received(cb: CallbackQuery):
    await _mark_sale(cb, "received")


@router.callback_query(F.data.startswith("ppd:"))
async def sale_paid(cb: CallbackQuery):
    await _mark_sale(cb, "paid")


async def _mark_sale(cb: CallbackQuery, kind: str):
    """Обе отметки идемпотентны: повторное нажатие не двигает дату и не пишет
    второе событие. Кнопка живёт в чате вечно, а нажать её могут дважды."""
    sale_id = await cb_id(cb)
    if sale_id is None:
        return
    async with Session() as s, s.begin():
        sale = await s.get(Sale, sale_id, with_for_update=True)
        if sale is None:
            await cb.answer(STALE, show_alert=True)
            return
        wid = sale.worker_id
        if kind == "paid" and sale.received_at is None:
            await cb.answer("Сначала отметьте, что деньги пришли",
                            show_alert=True)
            return
        already = getattr(sale, f"{kind}_at") is not None
        if not already:
            now = datetime.now(config.TZ)
            setattr(sale, f"{kind}_at", now)
            log_event(s, sale.lead_id, f"sale_{kind}", cb.from_user.id,
                      field=f"sale#{sale.id}", new=local(now))
    if already:
        await cb.answer("Уже отмечено")
    else:
        log.info("sale %s marked %s by tg_id=%s", sale_id, kind,
                 cb.from_user.id)
        await cb.answer("Отмечено")
    await _payouts_worker(cb.message, wid)


# --- рассылка ----------------------------------------------------------------

@router.message(F.text == kb.BTN_A_BROADCAST)
async def broadcast_ask(message: Message, state: FSMContext):
    await state.set_state(Adm.broadcast)
    await message.answer("Текст сообщения всем работникам:", reply_markup=kb.cancel_kb())


@router.message(Adm.broadcast)
async def broadcast_send(message: Message, state: FSMContext):
    text = message.text or ""
    await state.clear()
    async with Session() as s:
        targets = list(await s.scalars(
            select(Worker.tg_id).where(Worker.is_active.is_(True),
                                       Worker.deleted_at.is_(None), NOT_ADMIN)
        ))
    sent = 0
    for tg_id in targets:
        # parse_mode=None: по умолчанию у бота HTML, и любой «<» в тексте админа
        # ронял бы отправку каждому получателю
        try:
            await message.bot.send_message(tg_id, text, parse_mode=None)
            sent += 1
        except TelegramRetryAfter as e:
            log.warning("broadcast flood limit, wait %s s", e.retry_after)
            await asyncio.sleep(e.retry_after)
            try:
                await message.bot.send_message(tg_id, text, parse_mode=None)
                sent += 1
            except Exception as e2:
                log.warning("broadcast failed for %s: %s", tg_id, e2)
        except Exception as e:
            log.warning("broadcast failed for %s: %s", tg_id, e)
        await asyncio.sleep(0.05)
    await message.answer(f"Отправлено: {sent} из {len(targets)}")


# --- CSV ---------------------------------------------------------------------

def csv_safe(v):
    """Гасит формулы Excel: значение работника вида =HYPERLINK(...) там исполнится."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


CSV_HEADER = [
    "id", "дата", "работник", "название", "сайт", "источник", "страна", "город",
    "язык", "ниша", "рейтинг", "статус", "возможный_дубликат", "где_нашли",
    "заметка", "контакты", "черновик", "заметка_админа",
]


@router.message(F.text == kb.BTN_A_CSV)
async def export_csv(message: Message, state: FSMContext):
    await state.set_state(None)
    # выгрузка уважает фильтры «Все компании»: раньше CSV молча отдавал всю
    # базу, и настроенный фильтр по стране/статусу выглядел применённым, но не был
    flt = await get_flt(state)
    conds = flt_conditions(flt)
    filtered = any(
        flt.get(k) is not None
        for k in ("worker_id", "country", "niche", "status", "days")
    )
    fd, path = tempfile.mkstemp(suffix=".csv", prefix="tobisite_")
    os.close(fd)
    rows_written = 0
    try:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(CSV_HEADER)
            last_id = 0
            while True:
                async with Session() as s:
                    leads = list(await s.scalars(
                        select(Lead).where(*conds, Lead.id > last_id)
                        .order_by(Lead.id).limit(500)
                    ))
                    if not leads:
                        break
                    ids = [l.id for l in leads]
                    names = dict((await s.execute(
                        select(Worker.id, Worker.name)
                        .where(Worker.id.in_([l.worker_id for l in leads]))
                    )).all())
                    contacts = list(await s.scalars(
                        select(Contact).where(
                            Contact.lead_id.in_(ids), Contact.deleted_at.is_(None)
                        ).order_by(Contact.id)
                    ))
                by_lead = {}
                for c in contacts:
                    label = c.ctype_other if c.ctype == "other" and c.ctype_other else \
                        config.CONTACT_TYPE_LABELS.get(c.ctype, c.ctype)
                    by_lead.setdefault(c.lead_id, []).append(f"{label}: {c.value}")
                for l in leads:
                    writer.writerow([csv_safe(v) for v in (
                        l.id, local(l.created_at), names.get(l.worker_id, ""), l.name,
                        l.website_url or "", l.source_url, l.country, l.city,
                        l.language, l.niche, l.google_rating or "",
                        config.STATUS_LABELS.get(l.status, l.status),
                        "да" if l.possible_duplicate else "",
                        l.found_via, l.note or "",
                        " | ".join(by_lead.get(l.id, [])),
                        l.draft_url or "", l.admin_note or "",
                    )])
                    rows_written += 1
                last_id = ids[-1]
        await message.answer_document(
            FSInputFile(path, filename="companies.csv"),
            caption=f"Записей: {rows_written}"
                    + (" (по текущим фильтрам)" if filtered else ""),
        )
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# CSV начислений (7.16) — заготовка данных акта агента: договор требует, чтобы
# вознаграждение подтверждалось отчётом с количественными показателями.
# Период — месяц продажи; он же колонка, по которой отчёт режется на акты.
PAYOUTS_CSV_HEADER = [
    "id", "период", "дата", "работник", "лид", "компания", "сумма_сделки",
    "валюта", "процент", "начисление", "деньги_пришли", "выплачено", "заметка",
]


def month_key(dt: datetime) -> str:
    return dt.astimezone(config.TZ).strftime("%Y-%m")


@router.callback_query(F.data.startswith("pcs:"))
async def export_payouts_csv(cb: CallbackQuery):
    await cb.answer()
    fd, path = tempfile.mkstemp(suffix=".csv", prefix="tobisite_payouts_")
    os.close(fd)
    rows_written = 0
    try:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(PAYOUTS_CSV_HEADER)
            last_id = 0
            while True:
                async with Session() as s:
                    rows = (await s.execute(
                        select(Sale, Worker.name, Lead.name)
                        .join(Worker, Worker.id == Sale.worker_id)
                        .join(Lead, Lead.id == Sale.lead_id)
                        .where(Sale.id > last_id).order_by(Sale.id).limit(500)
                    )).all()
                if not rows:
                    break
                for sale, worker_name, lead_name in rows:
                    writer.writerow([csv_safe(v) for v in (
                        sale.id, month_key(sale.created_at),
                        local(sale.created_at), worker_name, sale.lead_id,
                        lead_name, f"{sale.deal_amount:.2f}", sale.currency,
                        sale.rate_pct, f"{sale.amount_due:.2f}",
                        local(sale.received_at) if sale.received_at else "",
                        local(sale.paid_at) if sale.paid_at else "",
                        sale.note or "",
                    )])
                    rows_written += 1
                last_id = rows[-1][0].id
        if not rows_written:
            await cb.message.answer("Продаж пока нет — выгружать нечего.")
            return
        await cb.message.answer_document(
            FSInputFile(path, filename="payouts.csv"),
            caption=f"Начислений: {rows_written}",
        )
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
