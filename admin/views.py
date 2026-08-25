"""Роуты панели: пять экранов, все только на чтение.

Даты показываются в Europe/Kyiv, как в боте (local() в handlers_worker.py):
цифры и время в панели и в сообщениях бота должны совпадать буквально.
"""
import pathlib
from decimal import Decimal
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import config
from models import month_start

from . import queries
from .app import READ_METHODS

router = APIRouter()
templates = Jinja2Templates(
    directory=str(pathlib.Path(__file__).resolve().parent / "templates")
)

# те же пресеты дат, что DATE_PRESETS в handlers_admin.py
DATE_PRESETS = [("Сегодня", 0), ("7 дней", 6), ("30 дней", 29)]


def local(dt) -> str:
    return dt.astimezone(config.TZ).strftime("%d.%m.%Y %H:%M")


def thousands(v) -> str:
    """1234567 → «1 234 567», как _n() в боте."""
    return f"{int(v):,}".replace(",", " ")


templates.env.filters["local"] = local
templates.env.filters["thousands"] = thousands
templates.env.filters["usd2"] = lambda v: f"{v:.2f}"
templates.env.filters["usd4"] = lambda v: f"{v:.4f}"


def render(request: Request, name: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request, name=name,
        context={"email": getattr(request.state, "email", ""), **context},
    )


def leads_url(flt: dict, offset: int) -> str:
    """Ссылка на страницу списка с теми же фильтрами: их держит querystring."""
    params = [(k, v) for k, v in (
        ("status", flt["status"]), ("country", flt["country"]),
        ("niche", flt["niche"]), ("worker", flt["worker_id"]),
        ("days", flt["days"]),
    ) if v not in (None, "")]
    if offset:
        params.append(("offset", offset))
    return "/leads?" + urlencode(params) if params else "/leads"


@router.api_route("/", methods=READ_METHODS, response_class=HTMLResponse)
async def dashboard(request: Request):
    async with request.app.state.db() as session:
        rows = await queries.funnel(session)
        mrr = await queries.mrr(session)
        spent = await queries.month_spent(session)
        events = await queries.recent_events(session)
    cap = config.AI_MONTHLY_CAP_USD
    return render(
        request, "dashboard.html", active="dashboard", funnel=rows,
        leads_total=sum(n for _, _, n in rows), mrr=mrr, spent=spent, cap=cap,
        pct=(float(spent) / cap * 100 if cap > 0 else None), events=events,
    )


@router.api_route("/leads", methods=READ_METHODS, response_class=HTMLResponse)
async def leads(request: Request, status: str = "", country: str = "",
                niche: str = "", worker: int | None = Query(None, ge=1),
                days: int | None = Query(None, ge=0),
                offset: int = Query(0, ge=0)):
    flt = {"worker_id": worker, "country": country, "niche": niche,
           "status": status, "days": days}
    # htmx просит только таблицу — фильтры и их варианты перерисовывать незачем
    partial = "HX-Request" in request.headers
    async with request.app.state.db() as session:
        rows, total = await queries.leads_page(session, flt, offset)
        options = {} if partial else await queries.filter_options(session)
    context = dict(
        active="leads", rows=rows, total=total, offset=offset, flt=flt,
        page_size=queries.PAGE_SIZE,
        prev_url=leads_url(flt, offset - queries.PAGE_SIZE) if offset else None,
        next_url=(leads_url(flt, offset + queries.PAGE_SIZE)
                  if offset + queries.PAGE_SIZE < total else None),
        statuses=config.STATUSES, labels=config.STATUS_LABELS,
        presets=DATE_PRESETS, **options,
    )
    return render(request, "leads_table.html" if partial else "leads.html",
                  **context)


@router.api_route("/leads/{lead_id}", methods=READ_METHODS, response_class=HTMLResponse)
async def lead(request: Request, lead_id: int):
    async with request.app.state.db() as session:
        card = await queries.lead_card(session, lead_id)
    if card is None:
        raise HTTPException(404, "Запись не найдена")
    return render(request, "lead.html", active="leads",
                  labels=config.STATUS_LABELS,
                  contact_labels=config.CONTACT_TYPE_LABELS, **card)


@router.api_route("/metrics", methods=READ_METHODS, response_class=HTMLResponse)
async def weekly_metrics(request: Request, weeks: int = Query(None, ge=1, le=52)):
    """Метрики недели (13.1), юнит-экономика (20.10) и превью-хиты (13.4)."""
    async with request.app.state.db() as session:
        weeks_rows = await queries.weekly(session, weeks or queries.WEEKS)
        units = await queries.unit_costs(session, month_start())
        published, opened = await queries.preview_funnel(session)
        leads_rows = await queries.preview_leads(session)
        hits = await queries.preview_recent(session)
    return render(
        request, "metrics.html", active="metrics", weeks=weeks_rows,
        units=units, since=month_start(), published=published, opened=opened,
        conversion=(opened / published * 100 if published else None),
        preview_leads=leads_rows, hits=hits,
    )


@router.api_route("/costs", methods=READ_METHODS, response_class=HTMLResponse)
async def cost_ledger(request: Request, window: str = "month"):
    if window not in queries.COST_WINDOWS:
        window = "month"
    label, since_fn = queries.COST_WINDOWS[window]
    since = since_fn()
    async with request.app.state.db() as session:
        rows = await queries.cost_breakdown(session, since)
        recent = await queries.cost_recent(session)
        spent = await queries.month_spent(session)
    cap = config.AI_MONTHLY_CAP_USD
    return render(
        request, "costs.html", active="costs", rows=rows, recent=recent,
        window=window, window_label=label, since=since,
        total=sum((r[3] for r in rows), start=Decimal(0)), spent=spent, cap=cap,
        pct=(float(spent) / cap * 100 if cap > 0 else None),
        windows=queries.COST_WINDOWS,
    )


@router.api_route("/subs", methods=READ_METHODS, response_class=HTMLResponse)
async def subscriptions(request: Request):
    async with request.app.state.db() as session:
        active_rows = await queries.subscriptions(session, "active")
        canceled = await queries.subscriptions(session, "canceled",
                                               newest_first=True)
    return render(
        request, "subs.html", active="subs", rows=active_rows,
        canceled=canceled,
        mrr=sum((cs.price_usd for cs, _ in active_rows), start=Decimal(0)),
    )
