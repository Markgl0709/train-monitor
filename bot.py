import os
import json
import logging
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
NAVITIA_TOKEN   = os.environ["NAVITIA_TOKEN"]
DATA_FILE = Path("prices.json")
CHECK_DAYS = 60

NAVITIA_API = "https://api.navitia.io/v1"

# from_id / to_id are filled at startup via _resolve_station()
ROUTES = [
    {"from_name": "Düsseldorf Hbf", "to_name": "Paris-Nord", "from_id": None, "to_id": None},
    {"from_name": "Köln Hbf",       "to_name": "Paris-Nord", "from_id": None, "to_id": None},
]

KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🔍 Проверить сейчас"), KeyboardButton("📋 Статус")],
        [KeyboardButton("💰 Топ-10 билетов"), KeyboardButton("💶 Установить макс. цену")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# Users currently waiting to enter a max price
_awaiting_price: set[int] = set()


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def load_data() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"journeys": {}, "max_price": None, "last_check": None}


def save_data(data: dict) -> None:
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# Navitia helpers (run in a thread via run_in_executor)
# ---------------------------------------------------------------------------

def _navitia_get(path: str, params: dict) -> dict:
    resp = requests.get(
        f"{NAVITIA_API}{path}",
        params=params,
        auth=(NAVITIA_TOKEN, ""),
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def _resolve_station(name: str) -> Optional[str]:
    """Return Navitia stop_area ID for a station name, or None."""
    try:
        data = _navitia_get("/places", {"q": name, "type[]": "stop_area", "count": 1})
        return data["places"][0]["id"]
    except Exception as e:
        logger.error("Could not resolve station '%s': %s", name, e)
        return None


def _fetch_day(from_id: str, to_id: str, date: datetime) -> list:
    """Fetch journeys via Navitia API."""
    try:
        data = _navitia_get("/journeys", {
            "from":     from_id,
            "to":       to_id,
            "datetime": date.strftime("%Y%m%dT%H%M%S"),
            "count":    10,
        })
        return data.get("journeys", [])
    except Exception as e:
        logger.warning("Navitia error (%s → %s, %s): %s", from_id, to_id, date.date(), e)
        return []


def _parse_price(journey: dict) -> Optional[float]:
    """Return price in EUR or None if unavailable."""
    fare = journey.get("fare", {})
    if not fare.get("found"):
        return None
    value = fare.get("total", {}).get("value")
    return float(value) if value else None


def _price_label(price: Optional[float]) -> str:
    return f"{price:.0f}€" if price is not None else "цена не указана"


def _journey_key(from_name: str, journey: dict) -> Optional[str]:
    try:
        dep_str = journey["departure_date_time"]        # "20260416T100000"
        dep = datetime.strptime(dep_str, "%Y%m%dT%H%M%S")
        return f"{from_name}|{dep.strftime('%Y%m%d%H%M')}"
    except (KeyError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Core check logic
# ---------------------------------------------------------------------------

CONCURRENCY = 10  # parallel HAFAS requests


async def check_prices(application: Optional[Application] = None) -> int:
    """Fetch prices for all routes × next CHECK_DAYS days in parallel.

    Returns the number of notifications sent.
    """
    loop      = asyncio.get_event_loop()
    data      = load_data()
    old       = data.get("journeys", {})
    max_price = data.get("max_price")
    now       = datetime.now()

    # Build all (route, date) tasks upfront
    tasks = [
        (route, now + timedelta(days=d))
        for route in ROUTES
        for d in range(CHECK_DAYS)
    ]

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def fetch_one(route, date):
        check_dt = date.replace(hour=0, minute=0, second=0, microsecond=0)
        async with semaphore:
            result = await loop.run_in_executor(
                None, _fetch_day, route["from_id"], route["to_id"], check_dt
            )
        return route["from_name"], result

    results = await asyncio.gather(*(fetch_one(r, d) for r, d in tasks))

    updated = {}
    alerts  = []

    for from_name, journeys in results:
        for journey in journeys:
            key = _journey_key(from_name, journey)
            if key is None:
                continue

            try:
                departure = datetime.strptime(journey["departure_date_time"], "%Y%m%dT%H%M%S")
            except (KeyError, ValueError):
                continue

            price_eur = _parse_price(journey)
            if max_price and price_eur is not None and price_eur > max_price:
                continue

            updated[key] = {
                "price":     price_eur,
                "departure": departure.isoformat(),
            }

            old_entry = old.get(key)
            old_price = old_entry.get("price") if old_entry else None

            is_new        = old_entry is None
            price_dropped = (
                price_eur is not None
                and old_price is not None
                and price_eur < old_price - 0.01
            )
            if is_new or price_dropped:
                alerts.append(
                    {
                        "from_name": from_name,
                        "departure": departure,
                        "price":     price_eur,
                        "old_price": old_price,
                        "is_new":    is_new,
                    }
                )

    data["journeys"]   = updated
    data["last_check"] = now.isoformat()
    save_data(data)

    if alerts and application:
        bot = application.bot
        # Sort by date
        alerts.sort(key=lambda x: x["departure"])
        for a in alerts:
            dep       = a["departure"]
            price     = a["price"]
            old_price = a["old_price"]

            if a["is_new"]:
                badge = "🆕 Новый рейс"
            else:
                diff  = old_price - price
                badge = f"📉 Подешевел на {diff:.0f}€"

            text = (
                f"{badge}\n"
                f"🚆 {a['from_name']} → Paris\n"
                f"📅 {dep.strftime('%d.%m.%Y')}  🕐 {dep.strftime('%H:%M')}\n"
                f"💶 {_price_label(price)}"
            )
            if not a["is_new"] and old_price is not None:
                text += f"  (было {old_price:.0f}€)"

            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)

    logger.info("Check complete — %d alerts, %d journeys tracked", len(alerts), len(updated))
    return len(alerts)


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Привет! Я слежу за ценами на поезда из Германии в Париж.\n\n"
        "Маршруты:\n"
        "• Düsseldorf Hbf → Paris\n"
        "• Köln Hbf → Paris\n\n"
        "Проверяю цены каждый час на ближайшие 60 дней.\n"
        "Уведомления приходят только при появлении новых или подешевевших билетов.",
        reply_markup=KEYBOARD,
    )


async def handle_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔄 Запускаю проверку, подождите…", reply_markup=KEYBOARD)
    count = await check_prices(context.application)
    if count:
        await update.message.reply_text(f"✅ Готово! Отправлено уведомлений: {count}.")
    else:
        await update.message.reply_text("✅ Готово! Новых или подешевевших билетов нет.")


async def cmd_prices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data     = load_data()
    journeys = data.get("journeys", {})

    if not journeys:
        await update.message.reply_text(
            "Данных пока нет. Нажми 🔍 Проверить сейчас.", reply_markup=KEYBOARD
        )
        return

    # Build list of (price_or_None, departure_dt, from_name)
    tickets = []
    for key, val in journeys.items():
        try:
            from_name = key.split("|")[0]
            departure = datetime.fromisoformat(val["departure"])
            tickets.append((val.get("price"), departure, from_name))
        except (KeyError, ValueError, IndexError):
            continue

    # Sort: priced tickets first (cheapest), then unpriced by date
    tickets.sort(key=lambda x: (x[0] is None, x[0] or 0, x[1]))
    top = tickets[:10]

    header = "💰 Топ-10 самых дешёвых билетов:\n" if any(p is not None for p, *_ in top) \
             else "🚆 Ближайшие 10 рейсов (цены недоступны):\n"
    lines  = [header]
    for i, (price, dep, from_name) in enumerate(top, 1):
        lines.append(
            f"{i}. {from_name} → Paris\n"
            f"   📅 {dep.strftime('%d.%m.%Y')}  🕐 {dep.strftime('%H:%M')}\n"
            f"   💶 {_price_label(price)}"
        )

    await update.message.reply_text("\n".join(lines), reply_markup=KEYBOARD)


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data       = load_data()
    last_check = data.get("last_check")
    max_price  = data.get("max_price")
    total      = len(data.get("journeys", {}))

    last_str  = (
        datetime.fromisoformat(last_check).strftime("%d.%m.%Y %H:%M")
        if last_check else "ещё не было"
    )
    price_str = f"{max_price:.0f}€" if max_price else "не установлен (все цены)"

    await update.message.reply_text(
        f"📊 Статус бота\n\n"
        f"🕐 Последняя проверка: {last_str}\n"
        f"🎫 Отслеживается рейсов: {total}\n"
        f"💶 Макс. цена: {price_str}\n"
        f"⏰ Интервал проверки: каждый час",
        reply_markup=KEYBOARD,
    )


async def handle_set_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _awaiting_price.add(update.effective_user.id)
    await update.message.reply_text(
        "Введите максимальную цену в EUR (например: 120).\n"
        "Отправьте 0, чтобы убрать ограничение.",
        reply_markup=KEYBOARD,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text    = update.message.text or ""
    user_id = update.effective_user.id

    if text == "🔍 Проверить сейчас":
        await handle_check(update, context)
    elif text == "📋 Статус":
        await handle_status(update, context)
    elif text == "💰 Топ-10 билетов":
        await cmd_prices(update, context)
    elif text == "💶 Установить макс. цену":
        await handle_set_price(update, context)
    elif user_id in _awaiting_price:
        try:
            value = float(text.replace(",", "."))
        except ValueError:
            await update.message.reply_text("❌ Введите число, например: 120", reply_markup=KEYBOARD)
            return

        _awaiting_price.discard(user_id)
        data               = load_data()
        data["max_price"]  = value if value > 0 else None
        save_data(data)

        if value > 0:
            await update.message.reply_text(f"✅ Макс. цена установлена: {value:.0f}€", reply_markup=KEYBOARD)
        else:
            await update.message.reply_text("✅ Ограничение цены снято.", reply_markup=KEYBOARD)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    # Resolve Navitia station IDs once at startup
    for route in ROUTES:
        if route["from_id"] is None:
            route["from_id"] = _resolve_station(route["from_name"])
        if route["to_id"] is None:
            route["to_id"] = _resolve_station(route["to_name"])
        logger.info("Route: %s → %s  |  %s → %s",
                    route["from_name"], route["to_name"],
                    route["from_id"], route["to_id"])

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("prices", cmd_prices))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler = AsyncIOScheduler(timezone="Europe/Berlin")
    scheduler.add_job(
        check_prices,
        trigger="interval",
        hours=1,
        kwargs={"application": app},
        next_run_time=datetime.now(),  # run immediately on startup
        id="hourly_check",
    )
    scheduler.start()
    logger.info("Scheduler started — first check running now, then every hour.")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
