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
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pyhafas import HafasClient
from pyhafas.profile import DBProfile

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
DATA_FILE = Path("prices.json")
CHECK_DAYS = 60

ROUTES = [
    {"from_name": "Düsseldorf Hbf", "from_id": "8000085", "to_id": "8796066"},
    {"from_name": "Köln Hbf",        "from_id": "8000207", "to_id": "8796066"},
]

KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🔍 Проверить сейчас"), KeyboardButton("📋 Статус")],
        [KeyboardButton("💶 Установить макс. цену")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

hafas = HafasClient(DBProfile())

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
# HAFAS fetching (runs in a thread so it doesn't block the event loop)
# ---------------------------------------------------------------------------

def _fetch_day(from_id: str, to_id: str, date: datetime) -> list:
    """Synchronous HAFAS call — run via run_in_executor."""
    try:
        from pyhafas.types.fptf import Station
        origin      = Station(id=from_id, name="")
        destination = Station(id=to_id,   name="")
        return hafas.journeys(
            origin=origin,
            destination=destination,
            date=date,
            max_changes=2,
            max_journeys=20,
        )
    except Exception as e:
        logger.warning("HAFAS error (%s → %s, %s): %s", from_id, to_id, date.date(), e)
        return []


def _parse_price(journey) -> Optional[float]:
    """Return price in EUR or None if unavailable."""
    price = getattr(journey, "price", None)
    if price is None:
        return None
    if hasattr(price, "amount"):
        amount = float(price.amount)
        # HAFAS sometimes returns cents (e.g. 4900 for 49 €)
        return amount / 100 if amount > 500 else amount
    if isinstance(price, (int, float)):
        return float(price)
    return None


def _journey_key(from_name: str, journey) -> Optional[str]:
    try:
        dep = journey.legs[0].departure
        return f"{from_name}|{dep.strftime('%Y%m%d%H%M')}"
    except (IndexError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Core check logic
# ---------------------------------------------------------------------------

async def check_prices(application: Optional[Application] = None) -> int:
    """Fetch prices for all routes × next CHECK_DAYS days.

    Returns the number of notifications sent.
    """
    loop      = asyncio.get_event_loop()
    data      = load_data()
    old       = data.get("journeys", {})
    updated   = {}
    alerts    = []
    max_price = data.get("max_price")
    now       = datetime.now()

    for route in ROUTES:
        from_name = route["from_name"]
        from_id   = route["from_id"]
        to_id     = route["to_id"]

        for day_offset in range(CHECK_DAYS):
            check_date = now + timedelta(days=day_offset)
            # Replace hour so we get departures for the whole day
            check_dt = check_date.replace(hour=0, minute=0, second=0, microsecond=0)

            journeys = await loop.run_in_executor(
                None, _fetch_day, from_id, to_id, check_dt
            )

            for journey in journeys:
                price_eur = _parse_price(journey)
                if price_eur is None:
                    continue
                if max_price and price_eur > max_price:
                    continue

                key = _journey_key(from_name, journey)
                if key is None:
                    continue

                try:
                    departure = journey.legs[0].departure
                except (IndexError, AttributeError):
                    continue

                updated[key] = {"price": price_eur, "departure": departure.isoformat()}

                old_price = old.get(key, {}).get("price")
                if old_price is None or price_eur < old_price - 0.01:
                    alerts.append(
                        {
                            "from_name": from_name,
                            "departure": departure,
                            "price":     price_eur,
                            "old_price": old_price,
                        }
                    )

            await asyncio.sleep(0.3)  # be gentle with the API

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

            if old_price is None:
                badge = "🆕 Новый билет"
            else:
                diff  = old_price - price
                badge = f"📉 Подешевел на {diff:.0f}€"

            text = (
                f"{badge}\n"
                f"🚆 {a['from_name']} → Paris\n"
                f"📅 {dep.strftime('%d.%m.%Y')}  🕐 {dep.strftime('%H:%M')}\n"
                f"💶 {price:.0f}€"
            )
            if old_price is not None:
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

    # Build list of (price, departure_dt, from_name)
    tickets = []
    for key, val in journeys.items():
        try:
            from_name = key.split("|")[0]
            departure = datetime.fromisoformat(val["departure"])
            tickets.append((val["price"], departure, from_name))
        except (KeyError, ValueError, IndexError):
            continue

    tickets.sort(key=lambda x: x[0])
    top = tickets[:3]

    lines = ["💰 Топ-3 самых дешёвых билета:\n"]
    for i, (price, dep, from_name) in enumerate(top, 1):
        lines.append(
            f"{i}. {from_name} → Paris\n"
            f"   📅 {dep.strftime('%d.%m.%Y')}  🕐 {dep.strftime('%H:%M')}\n"
            f"   💶 {price:.0f}€"
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
