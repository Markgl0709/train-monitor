import os
import json
import logging
import asyncio
import httpx
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
DATA_FILE  = Path("prices.json")
CHECK_DAYS = 60

ROUTES = [
    {"from_name": "Düsseldorf Hbf", "from_code": "DUS"},
    {"from_name": "Köln Hbf",       "from_code": "KLN"},
]
TO_CODE = "PAR"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Referer": "https://www.eurostar.com/",
}

KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🔍 Проверить сейчас"), KeyboardButton("📋 Статус")],
        [KeyboardButton("💰 Топ-10 билетов"),   KeyboardButton("💶 Установить макс. цену")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

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
# Eurostar scraping via httpx
# ---------------------------------------------------------------------------

def _extract_journeys(data, from_name: str) -> list:
    """Parse Eurostar API JSON response into normalised journey dicts."""
    journeys = []

    # Try different top-level keys Eurostar might use
    raw = (
        data.get("outboundJourneys")
        or data.get("journeys")
        or data.get("trips")
        or data.get("trains")
        or []
    )
    if isinstance(data, list):
        raw = data

    for item in raw:
        # Departure time
        dep_raw = (
            item.get("departureDateTime")
            or item.get("departure")
            or item.get("departureDatetime")
            or item.get("departureTime")
        )
        if not dep_raw:
            continue
        try:
            dep_dt = datetime.fromisoformat(str(dep_raw)[:19])
        except ValueError:
            continue

        # Price — try multiple locations
        price = None
        for field in ["lowestPrice", "price", "minPrice", "cheapestPrice", "fare"]:
            obj = item.get(field)
            if obj is None:
                continue
            if isinstance(obj, (int, float)):
                price = float(obj)
                break
            if isinstance(obj, dict):
                amount = obj.get("amount") or obj.get("value") or obj.get("cents")
                if amount is not None:
                    try:
                        price = float(amount) / (100 if obj.get("currency") else 1)
                        break
                    except (TypeError, ValueError):
                        pass

        journeys.append({"departure": dep_dt.isoformat(), "price": price})

    return journeys


async def _fetch_day(
    client: httpx.AsyncClient,
    from_code: str,
    from_name: str,
    date: datetime,
) -> list:
    date_str = date.strftime("%Y-%m-%d")
    try:
        resp = await client.get(
            "https://www.eurostar.com/uk-en/train-search",
            params={
                "origin":         from_code,
                "destination":    TO_CODE,
                "outbound-date":  date_str,
                "adult":          "1",
            },
            timeout=30,
        )
        ct = resp.headers.get("content-type", "")
        logger.info(
            "Eurostar %s %s → %s | CT=%s | body[:300]=%s",
            from_name, date_str, resp.status_code, ct, resp.text[:300],
        )
        if resp.status_code == 200 and "json" in ct:
            data = resp.json()
            journeys = _extract_journeys(data, from_name)
            if journeys:
                logger.info("Got %d journeys for %s %s", len(journeys), from_name, date_str)
            else:
                logger.info("Parsed JSON OK but no journeys. Top-level keys: %s", list(data.keys()) if isinstance(data, dict) else type(data))
            return journeys
    except Exception as e:
        logger.warning("Fetch error %s %s: %s", from_name, date_str, e)
    return []


# ---------------------------------------------------------------------------
# Check logic
# ---------------------------------------------------------------------------

_check_lock: asyncio.Lock | None = None


def _get_check_lock() -> asyncio.Lock:
    global _check_lock
    if _check_lock is None:
        _check_lock = asyncio.Lock()
    return _check_lock


async def check_prices(application=None) -> int:
    lock = _get_check_lock()
    if lock.locked():
        logger.info("Check already running, skipping.")
        if application:
            await application.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text="⏳ Проверка уже выполняется, подождите окончания.",
            )
        return 0
    async with lock:
        return await _do_check_prices(application)


async def _do_check_prices(application=None) -> int:
    data      = load_data()
    old       = data.get("journeys", {})
    max_price = data.get("max_price")
    now       = datetime.now()

    tasks = [
        (route, now + timedelta(days=d))
        for route in ROUTES
        for d in range(CHECK_DAYS)
    ]

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        results = await asyncio.gather(*(
            _fetch_day(
                client,
                r["from_code"],
                r["from_name"],
                d.replace(hour=0, minute=0, second=0, microsecond=0),
            )
            for r, d in tasks
        ))

    updated = {}
    alerts  = []

    for (route, _), journeys in zip(tasks, results):
        from_name = route["from_name"]
        for j in journeys:
            try:
                departure = datetime.fromisoformat(j["departure"])
            except (KeyError, ValueError):
                continue

            key       = f"{from_name}|{departure.strftime('%Y%m%d%H%M')}"
            price_eur = j.get("price")

            if max_price and price_eur is not None and price_eur > max_price:
                continue

            updated[key] = {"price": price_eur, "departure": departure.isoformat()}

            old_entry = old.get(key)
            old_price = old_entry.get("price") if old_entry else None
            is_new    = old_entry is None
            price_dropped = (
                price_eur is not None
                and old_price is not None
                and price_eur < old_price - 0.01
            )
            if is_new or price_dropped:
                alerts.append({
                    "from_name": from_name,
                    "departure": departure,
                    "price":     price_eur,
                    "old_price": old_price,
                    "is_new":    is_new,
                })

    data["journeys"]   = updated
    data["last_check"] = now.isoformat()
    save_data(data)

    if alerts and application:
        alerts.sort(key=lambda x: x["departure"])
        for a in alerts:
            dep       = a["departure"]
            price     = a["price"]
            old_price = a["old_price"]
            badge     = "🆕 Новый рейс" if a["is_new"] else f"📉 Подешевел на {old_price - price:.0f}€"
            price_str = f"{price:.0f}€" if price is not None else "цена не указана"
            text = (
                f"{badge}\n"
                f"🚆 {a['from_name']} → Paris\n"
                f"📅 {dep.strftime('%d.%m.%Y')}  🕐 {dep.strftime('%H:%M')}\n"
                f"💶 {price_str}"
            )
            if not a["is_new"] and old_price is not None:
                text += f"  (было {old_price:.0f}€)"
            await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)

    logger.info("Check done — %d alerts, %d journeys tracked", len(alerts), len(updated))
    return len(alerts)


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Привет! Я слежу за ценами на поезда из Германии в Париж.\n\n"
        "Маршруты:\n• Düsseldorf Hbf → Paris\n• Köln Hbf → Paris\n\n"
        "Проверяю цены каждый час на ближайшие 60 дней.\n"
        "Уведомления приходят только при появлении новых или подешевевших билетов.",
        reply_markup=KEYBOARD,
    )


_awaiting_price: set[int] = set()


async def _run_check_and_notify(application: Application) -> None:
    try:
        count = await check_prices(application)
        msg = (
            f"✅ Готово! Отправлено уведомлений: {count}."
            if count
            else "✅ Готово! Новых или подешевевших билетов нет."
        )
    except Exception as e:
        msg = f"❌ Ошибка при проверке: {e}"
    await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)


async def handle_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🔄 Запускаю проверку, пришлю результат когда закончу.",
        reply_markup=KEYBOARD,
    )
    asyncio.create_task(_run_check_and_notify(context.application))


async def cmd_prices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data     = load_data()
    journeys = data.get("journeys", {})
    if not journeys:
        await update.message.reply_text(
            "Данных пока нет. Нажми 🔍 Проверить сейчас.", reply_markup=KEYBOARD
        )
        return

    tickets = []
    for key, val in journeys.items():
        try:
            from_name = key.split("|")[0]
            departure = datetime.fromisoformat(val["departure"])
            tickets.append((val.get("price"), departure, from_name))
        except (KeyError, ValueError, IndexError):
            continue

    tickets.sort(key=lambda x: (x[0] is None, x[0] or 0, x[1]))
    top = tickets[:10]

    has_prices = any(p is not None for p, *_ in top)
    header = "💰 Топ-10 самых дешёвых билетов:\n" if has_prices else "🚆 Ближайшие 10 рейсов:\n"
    lines  = [header]
    for i, (price, dep, from_name) in enumerate(top, 1):
        price_str = f"{price:.0f}€" if price is not None else "цена не указана"
        lines.append(
            f"{i}. {from_name} → Paris\n"
            f"   📅 {dep.strftime('%d.%m.%Y')}  🕐 {dep.strftime('%H:%M')}\n"
            f"   💶 {price_str}"
        )
    await update.message.reply_text("\n".join(lines), reply_markup=KEYBOARD)


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data       = load_data()
    last_check = data.get("last_check")
    max_price  = data.get("max_price")
    total      = len(data.get("journeys", {}))
    last_str   = (
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
        "Введите максимальную цену в EUR (например: 120).\nОтправьте 0, чтобы убрать ограничение.",
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
        data              = load_data()
        data["max_price"] = value if value > 0 else None
        save_data(data)
        msg = f"✅ Макс. цена: {value:.0f}€" if value > 0 else "✅ Ограничение цены снято."
        await update.message.reply_text(msg, reply_markup=KEYBOARD)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
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
        next_run_time=datetime.now(),
        id="hourly_check",
    )
    scheduler.start()
    logger.info("Scheduler started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
