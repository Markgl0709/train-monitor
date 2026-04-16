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
from playwright.async_api import async_playwright, Browser, Playwright

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
CONCURRENCY = 1   # one page at a time to keep memory low

ROUTES = [
    {"from_name": "Düsseldorf Hbf", "from_eva": "8000085", "to_eva": "8796066"},
    {"from_name": "Köln Hbf",       "from_eva": "8000207", "to_eva": "8796066"},
]

KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🔍 Проверить сейчас"), KeyboardButton("📋 Статус")],
        [KeyboardButton("💰 Топ-10 билетов"), KeyboardButton("💶 Установить макс. цену")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# ---------------------------------------------------------------------------
# Playwright browser singleton
# ---------------------------------------------------------------------------

_pw: Optional[Playwright] = None
_browser: Optional[Browser] = None


async def _get_browser() -> Browser:
    global _pw, _browser
    if _browser is None or not _browser.is_connected():
        _pw = await async_playwright().start()
        _browser = await _pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-zygote",
                "--disable-extensions",
                "--disable-sync",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-translate",
                "--mute-audio",
                "--disable-blink-features=AutomationControlled",
                "--js-flags=--max-old-space-size=128",
            ],
        )
        logger.info("Playwright browser started.")
    return _browser


# Only one price check may run at a time
_check_lock: Optional[asyncio.Lock] = None

def _get_check_lock() -> asyncio.Lock:
    global _check_lock
    if _check_lock is None:
        _check_lock = asyncio.Lock()
    return _check_lock


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
# Scraping
# ---------------------------------------------------------------------------


def _extract_journeys_from_api(data: dict) -> list:
    """Try to extract normalised journey dicts from a captured API response."""
    raw = (
        data.get("verbindungen")
        or data.get("journeys")
        or data.get("connections")
        or []
    )
    journeys = []
    for item in raw:
        # --- departure ---
        dep_raw = (
            item.get("abfahrt")
            or item.get("abfahrtszeit")
            or item.get("departure")
            or item.get("departure_date_time")
        )
        if not dep_raw:
            # dive into first section
            sections = item.get("verbindungsAbschnitte") or item.get("sections") or item.get("legs") or []
            if sections:
                sec = sections[0]
                dep_raw = sec.get("abfahrt") or sec.get("departure")
        if not dep_raw:
            continue

        try:
            if "T" in str(dep_raw) and len(str(dep_raw)) > 12:
                dep_dt = datetime.fromisoformat(str(dep_raw)[:19])
            else:
                dep_dt = datetime.strptime(str(dep_raw)[:16], "%Y%m%dT%H%M")
        except ValueError:
            continue

        # --- price ---
        price = None
        for field in [
            "preisGuenstigster", "preis", "price", "fare",
            "angebotPreis", "bestPreis",
        ]:
            obj = item.get(field)
            if not obj:
                continue
            # price object with amount/betrag
            amount = obj.get("betrag") or obj.get("amount") or obj.get("value")
            if amount is not None:
                try:
                    price = float(amount)
                except (TypeError, ValueError):
                    pass
                break
            # price as a number directly
            if isinstance(obj, (int, float)):
                price = float(obj)
                break

        # Check angebote list
        if price is None:
            for angebot in (item.get("angebote") or []):
                p = angebot.get("preis") or angebot.get("price") or {}
                amount = p.get("betrag") or p.get("amount")
                if amount is not None:
                    try:
                        price = float(amount)
                        break
                    except (TypeError, ValueError):
                        pass

        journeys.append({"departure": dep_dt.isoformat(), "price": price})
    return journeys


async def _fetch_day(context, from_eva: str, to_eva: str, from_name: str, date: datetime) -> list:
    """Call bahn.de's internal API via JS fetch from within the page (same-origin, cookies included)."""
    page = await context.new_page()
    captured: list[dict] = []

    try:
        await page.goto("https://www.bahn.de", timeout=20_000, wait_until="load")

        date_str = date.strftime("%Y-%m-%dT06:00")

        # Call the internal bahn.de search API from within page JS context.
        # Same-origin fetch: cookies are included automatically, no CORS issues.
        result = await page.evaluate(f"""
            async () => {{
                const params = new URLSearchParams({{
                    sts: 'true',
                    so: {json.dumps(from_name)},
                    zo: 'Paris Nord',
                    kl: '2',
                    r: '13:16',
                    hd: {json.dumps(date_str)},
                    'hz': '[]',
                    ar: 'false',
                    s: 'true',
                    d: 'false',
                    soei: {json.dumps(from_eva)},
                    zoei: {json.dumps(to_eva)},
                    start: 'true'
                }});
                const url = '/web/api/angebote/fahrplan?' + params.toString();
                const r = await fetch(url, {{
                    credentials: 'include',
                    headers: {{
                        'Accept': 'application/json',
                        'X-Requested-With': 'XMLHttpRequest'
                    }}
                }});
                const text = await r.text();
                return {{status: r.status, body: text.slice(0, 4000)}};
            }}
        """)

        status = result.get("status")
        body   = result.get("body", "")
        logger.info("API %s %s — HTTP %s — body[:200]=%s", from_name, date.date(), status, body[:200])

        if status == 200:
            try:
                data = json.loads(body)
                journeys = _extract_journeys_from_api(data)
                if journeys:
                    logger.info("Got %d journeys for %s %s", len(journeys), from_name, date.date())
                    captured.extend(journeys)
                else:
                    logger.info("Parsed OK but no journeys extracted. Keys: %s", list(data.keys())[:8])
            except Exception as ex:
                logger.warning("JSON parse error (%s %s): %s | body=%s", from_name, date.date(), ex, body[:200])

    except Exception as e:
        logger.warning("Playwright error (%s %s): %s", from_name, date.date(), e)
    finally:
        await page.close()

    return captured


# ---------------------------------------------------------------------------
# Core check logic
# ---------------------------------------------------------------------------

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

    # Create a single browser context for the entire check cycle
    browser = await _get_browser()
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="de-DE",
    )

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def fetch_one(route, date):
        check_dt = date.replace(hour=0, minute=0, second=0, microsecond=0)
        async with semaphore:
            result = await _fetch_day(
                context, route["from_eva"], route["to_eva"], route["from_name"], check_dt
            )
        return route["from_name"], result

    results = await asyncio.gather(*(fetch_one(r, d) for r, d in tasks))

    await context.close()

    # Free Chromium memory after check cycle
    global _browser, _pw
    if _browser:
        await _browser.close()
        _browser = None
    if _pw:
        await _pw.stop()
        _pw = None

    updated = {}
    alerts  = []

    for from_name, journeys in results:
        for j in journeys:
            try:
                departure = datetime.fromisoformat(j["departure"])
            except (KeyError, ValueError):
                continue

            key = f"{from_name}|{departure.strftime('%Y%m%d%H%M')}"

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
        bot = application.bot
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
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)

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
        msg = f"✅ Готово! Отправлено уведомлений: {count}." if count else "✅ Готово! Новых или подешевевших билетов нет."
    except Exception as e:
        msg = f"❌ Ошибка при проверке: {e}"
    await application.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg)


async def handle_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🔄 Запускаю проверку, это займёт ~10–15 минут. Результат пришлю когда закончу.",
        reply_markup=KEYBOARD,
    )
    asyncio.create_task(_run_check_and_notify(context.application))


async def cmd_prices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data     = load_data()
    journeys = data.get("journeys", {})
    if not journeys:
        await update.message.reply_text("Данных пока нет. Нажми 🔍 Проверить сейчас.", reply_markup=KEYBOARD)
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
    header = "💰 Топ-10 самых дешёвых билетов:\n" if has_prices else "🚆 Ближайшие 10 рейсов (цены недоступны):\n"
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
    data      = load_data()
    last_check = data.get("last_check")
    max_price  = data.get("max_price")
    total      = len(data.get("journeys", {}))
    last_str   = datetime.fromisoformat(last_check).strftime("%d.%m.%Y %H:%M") if last_check else "ещё не было"
    price_str  = f"{max_price:.0f}€" if max_price else "не установлен (все цены)"
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
        msg = f"✅ Макс. цена установлена: {value:.0f}€" if value > 0 else "✅ Ограничение цены снято."
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
