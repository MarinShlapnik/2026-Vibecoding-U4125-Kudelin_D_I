"""
TF2 Trading Bot — Telegram-бот для автоматизации торговли предметами TF2
на площадке steam-trader.net

Лаб. работа 1: Создание Telegram-бота
Лаб. работа 2: Подключение к данным (SQLite + API steam-trader.net)

Функционал:
- Автоматическое выставление предметов на продажу с андерпрайсингом (−1 коп.)
- Автоматическая покупка предметов по заданному прайс-листу
- Отслеживание продаж и уведомления в Telegram
- Хранение всех данных в SQLite базе данных
- Статистика продаж/покупок через команды бота

Команды бота:
/start    — приветствие и список команд
/stats    — общая статистика (продажи + покупки + прибыль)
/selling  — список предметов на продаже
/bought   — последние купленные предметы
/sold     — последние проданные предметы
/history  — история операций за N дней (по умолчанию 7)
/profit   — прибыль за период
/top      — топ предметов по выручке
/status   — текущий статус бота
/run      — запустить торговлю
/stop     — остановить торговлю
/help     — справка по командам
"""

import os
import sqlite3
import time
import logging
import threading
import requests
import warnings
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# === ЗАГРУЗКА КОНФИГУРАЦИИ ===
load_dotenv()
warnings.filterwarnings('ignore')

# Telegram
TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

# Steam Trader API
STEAM_TRADER_API_KEY = os.getenv("STEAM_TRADER_API_KEY")
BASE_URL = "https://api.steam-trader.net"
HEADERS = {"X-API-Key": STEAM_TRADER_API_KEY}
PROXIES = {"http": None, "https": None}  # отключаем прокси (фикс SSL через HAPP)

# Параметры торговли
GAME_ID = 440  # TF2
MIN_PRICE = 700  # минимальная цена по умолчанию (копейки)
INTERVAL_SECONDS = 60
MY_OFFER_PREFIX = os.getenv("MY_OFFER_PREFIX", "")

# База данных
DB_FILE = "trading_bot.db"

# Логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# === ЧЁРНЫЙ СПИСОК ===
BLACKLIST = [
    "Refined Metal",
    "Reclaimed Metal",
    "Scrap Metal",
    "Mann Co. Supply Crate Key"
]

# === СПИСОК ПОКУПОК: "название": макс. цена (копейки) ===
BUY_LIST = {
    "The Kiss King": 350,
    "Flashdance Footies": 350,
    "Dead of Night": 5000,
    "The Bruiser's Bandanna": 1500,
    "The Sub Zero Suit": 2000,
    "Weight Room Warmer": 900,
    "The Team Captain": 2400,
    "Graybanns": 900,
    "The Last Breath": 1200,
    "Blighted Beak": 1950,
    "The Macho Mann": 600,
    "The All-Father": 900,
    "The Hot Case": 2250,
}

# === КАСТОМНЫЕ ЦЕНЫ ===
CUSTOM_MIN_PRICES = {
    "Australium Gold": 9000,
    "Taunt: Disco Fever": 21000,
    "Mann Co. Supply Crate Key": 15500,
}

CUSTOM_MAX_PRICES = {
    "Batter's Helmet": 6000,
}

# Глобальное состояние
bot_running = False
cycle_count = 0
sold_items = set()


# =============================================
# БАЗА ДАННЫХ (SQLite)
# =============================================

def init_db():
    """Инициализация базы данных — создание таблиц если не существуют"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Таблица продаж
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_name TEXT NOT NULL,
            price INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Таблица покупок
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_name TEXT NOT NULL,
            price INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Таблица логов (история действий бота)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bot_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.commit()
    conn.close()
    logger.info(f"База данных {DB_FILE} инициализирована")


def db_add_sale(item_name: str, price: int):
    """Записать продажу в БД"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO sales (item_name, price) VALUES (?, ?)',
        (item_name, price)
    )
    conn.commit()
    conn.close()


def db_add_purchase(item_name: str, price: int):
    """Записать покупку в БД"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO purchases (item_name, price) VALUES (?, ?)',
        (item_name, price)
    )
    conn.commit()
    conn.close()


def db_add_log(action: str, details: str = ""):
    """Записать действие в лог"""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO bot_log (action, details) VALUES (?, ?)',
            (action, details)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def db_get_stats():
    """Получить общую статистику из БД"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute('SELECT COUNT(*), COALESCE(SUM(price), 0) FROM sales')
    sold_count, total_earned = cursor.fetchone()

    cursor.execute('SELECT COUNT(*), COALESCE(SUM(price), 0) FROM purchases')
    bought_count, total_spent = cursor.fetchone()

    conn.close()
    return {
        "sold_count": sold_count,
        "total_earned": total_earned,
        "bought_count": bought_count,
        "total_spent": total_spent,
        "profit": total_earned - total_spent
    }


def db_get_last_sales(limit: int = 5):
    """Получить последние продажи"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        'SELECT item_name, price, created_at FROM sales ORDER BY id DESC LIMIT ?',
        (limit,)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def db_get_last_purchases(limit: int = 5):
    """Получить последние покупки"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        'SELECT item_name, price, created_at FROM purchases ORDER BY id DESC LIMIT ?',
        (limit,)
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


def db_get_history(days: int = 7):
    """Получить историю операций за последние N дней"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')

    cursor.execute(
        'SELECT COUNT(*), COALESCE(SUM(price), 0) FROM sales WHERE created_at >= ?',
        (since,)
    )
    sales_count, sales_sum = cursor.fetchone()

    cursor.execute(
        'SELECT COUNT(*), COALESCE(SUM(price), 0) FROM purchases WHERE created_at >= ?',
        (since,)
    )
    purchases_count, purchases_sum = cursor.fetchone()

    conn.close()
    return {
        "days": days,
        "sales_count": sales_count,
        "sales_sum": sales_sum,
        "purchases_count": purchases_count,
        "purchases_sum": purchases_sum,
        "profit": sales_sum - purchases_sum
    }


def db_get_top_items(limit: int = 10):
    """Получить топ предметов по выручке"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT item_name, COUNT(*) as cnt, SUM(price) as total
        FROM sales
        GROUP BY item_name
        ORDER BY total DESC
        LIMIT ?
    ''', (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def db_get_profit_by_period(days: int = 30):
    """Прибыль за указанный период с разбивкой по дням"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')

    cursor.execute('''
        SELECT DATE(created_at) as day, SUM(price) as total
        FROM sales WHERE created_at >= ?
        GROUP BY DATE(created_at)
        ORDER BY day DESC
    ''', (since,))
    sales_by_day = cursor.fetchall()

    cursor.execute('''
        SELECT DATE(created_at) as day, SUM(price) as total
        FROM purchases WHERE created_at >= ?
        GROUP BY DATE(created_at)
        ORDER BY day DESC
    ''', (since,))
    purchases_by_day = cursor.fetchall()

    conn.close()
    return sales_by_day, purchases_by_day


# =============================================
# ЛОГИКА РАБОТЫ С API STEAM-TRADER
# =============================================

def send_tg_raw(message):
    """Отправка сообщения в Telegram (для фонового потока)"""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": message},
            timeout=10
        )
    except Exception:
        pass


def get_min_market_price(hash_name):
    """Получить минимальную цену на рынке (исключая свои лоты)"""
    for attempt in range(3):
        try:
            response = requests.get(
                f"{BASE_URL}/v1/market/search-item-by-hash-name",
                headers=HEADERS, verify=False, proxies=PROXIES,
                params={"gameid": GAME_ID, "hashname": hash_name, "includedescription": False},
                timeout=30
            )
            data = response.json()
            if data["status"] != "success" or not data.get("data") or not data["data"].get("sell_offers"):
                return None
            offers = [
                o["price"] for o in data["data"]["sell_offers"]
                if not o["id"].startswith(MY_OFFER_PREFIX)
            ]
            return min(offers) if offers else None
        except Exception as e:
            logger.warning(f"get_min_market_price попытка {attempt+1}: {e}")
            time.sleep(5)
    return None


def calculate_price(hash_name, market_price):
    """Рассчитать цену с андерпрайсингом −1 копейка"""
    item_min = CUSTOM_MIN_PRICES.get(hash_name, MIN_PRICE)
    item_max = CUSTOM_MAX_PRICES.get(hash_name, None)

    if market_price and market_price > 1:
        new_price = market_price - 1
    else:
        return item_max if item_max is not None else item_min

    new_price = max(new_price, item_min)
    if item_max is not None:
        new_price = min(new_price, item_max)
    return new_price


def get_inventory(statuses):
    """Получить инвентарь (0=новые, 1=на продаже, 2=проданные)"""
    all_items = []
    page = 1
    while True:
        for attempt in range(3):
            try:
                response = requests.get(
                    f"{BASE_URL}/v1/user/inventory",
                    headers=HEADERS, verify=False, proxies=PROXIES,
                    params={"gameid": GAME_ID, "statuses": statuses,
                            "includedescription": True, "page": page, "count": 25},
                    timeout=30
                )
                items = response.json()["data"]
                all_items.extend(items)
                if len(items) < 25:
                    return all_items
                page += 1
                time.sleep(1)
                break
            except Exception as e:
                logger.warning(f"get_inventory стр.{page} попытка {attempt+1}: {e}")
                time.sleep(5)
    return all_items


def add_to_sale(item_id, price):
    """Выставить предмет на продажу"""
    for attempt in range(3):
        try:
            response = requests.post(
                f"{BASE_URL}/v1/market/add-to-sale",
                headers=HEADERS, verify=False, proxies=PROXIES,
                json={"items": [{"id": item_id, "price": price}]},
                timeout=30
            )
            return response.status_code
        except Exception as e:
            logger.warning(f"add_to_sale попытка {attempt+1}: {e}")
            time.sleep(5)
    return None


def set_price(item_id, new_price):
    """Обновить цену выставленного предмета"""
    for attempt in range(3):
        try:
            response = requests.post(
                f"{BASE_URL}/v1/market/set-price",
                headers=HEADERS, verify=False, proxies=PROXIES,
                json=[{"id": item_id, "price": new_price}],
                timeout=30
            )
            return response.status_code
        except Exception as e:
            logger.warning(f"set_price попытка {attempt+1}: {e}")
            time.sleep(5)
    return None


def buy_item(item_id):
    """Купить предмет на маркете"""
    for attempt in range(3):
        try:
            response = requests.post(
                f"{BASE_URL}/v1/market/buy",
                headers=HEADERS, verify=False, proxies=PROXIES,
                json={"gameid": GAME_ID, "id": item_id},
                timeout=30
            )
            return response.status_code, response.json()
        except Exception as e:
            logger.warning(f"buy_item попытка {attempt+1}: {e}")
            time.sleep(5)
    return None, None


# =============================================
# ФОНОВЫЕ ЗАДАЧИ ТОРГОВЛИ
# =============================================

def sell_new_items():
    """Выставить новые предметы на продажу"""
    items = get_inventory(statuses=0)
    tradable = [
        item for item in items
        if not any("Not Tradable" in d for d in (item.get("descriptions") or []))
        and item["hash_name"] not in BLACKLIST
    ]
    if not tradable:
        return

    logger.info(f"Новых предметов для продажи: {len(tradable)}")
    for item in tradable:
        market_price = get_min_market_price(item["hash_name"])
        new_price = calculate_price(item["hash_name"], market_price)
        status = add_to_sale(item["id"], new_price)
        if status == 200:
            db_add_log("list", f"{item['hash_name']} за {new_price} коп.")
        time.sleep(1)


def update_prices():
    """Обновить цены выставленных предметов (андерпрайсинг)"""
    items = get_inventory(statuses=1)
    if not items:
        return
    updated = 0
    for item in items:
        market_price = get_min_market_price(item["hash_name"])
        if market_price is None:
            continue
        new_price = calculate_price(item["hash_name"], market_price)
        if new_price >= item["price"]:
            continue
        set_price(item["id"], new_price)
        updated += 1
        time.sleep(1)
    if updated:
        logger.info(f"Обновлено цен: {updated}")
        db_add_log("reprice", f"Обновлено {updated} шт.")


def check_sold():
    """Проверить проданные предметы и записать в БД"""
    global sold_items
    items = get_inventory(statuses=2)
    for item in items:
        if item["id"] not in sold_items:
            sold_items.add(item["id"])
            price = item.get("price", 0)
            hash_name = item["hash_name"]
            # Запись в базу данных
            db_add_sale(hash_name, price)
            db_add_log("sale", f"{hash_name} за {price} коп.")
            send_tg_raw(f"✅ Продано!\n📦 {hash_name}\n💰 {price / 100:.2f} ₽")
            logger.info(f"ПРОДАНО: {hash_name} за {price / 100:.2f} ₽")


def check_and_buy():
    """Проверить рынок и купить дешёвые предметы из BUY_LIST"""
    for hash_name, max_price in BUY_LIST.items():
        try:
            response = requests.get(
                f"{BASE_URL}/v1/market/search-item-by-hash-name",
                headers=HEADERS, verify=False, proxies=PROXIES,
                params={"gameid": GAME_ID, "hashname": hash_name, "includedescription": False},
                timeout=30
            )
            data = response.json()
            if data["status"] != "success" or not data.get("data") or not data["data"].get("sell_offers"):
                time.sleep(1)
                continue

            cheap = [o for o in data["data"]["sell_offers"] if o["price"] <= max_price]
            if not cheap:
                time.sleep(1)
                continue

            cheapest = min(cheap, key=lambda o: o["price"])
            status, result = buy_item(cheapest["id"])
            if status == 200:
                # Запись в базу данных
                db_add_purchase(hash_name, cheapest["price"])
                db_add_log("buy", f"{hash_name} за {cheapest['price']} коп.")
                send_tg_raw(f"🛒 Куплено!\n📦 {hash_name}\n💰 {cheapest['price'] / 100:.2f} ₽")
                logger.info(f"КУПЛЕНО: {hash_name} за {cheapest['price'] / 100:.2f} ₽")
        except Exception as e:
            logger.warning(f"Ошибка покупки {hash_name}: {e}")
        time.sleep(1)


def trading_loop():
    """Основной фоновый цикл торговли"""
    global bot_running, cycle_count
    logger.info("Торговый цикл запущен")
    db_add_log("start", "Торговый цикл запущен")
    send_tg_raw("🤖 Торговый бот запущен!")

    while bot_running:
        try:
            sell_new_items()
            update_prices()
            check_sold()
            check_and_buy()

            cycle_count += 1
            # Каждый час — отчёт
            if cycle_count % 60 == 0:
                stats = db_get_stats()
                report = format_stats_report(stats)
                send_tg_raw(report)

        except Exception as e:
            logger.error(f"Ошибка в цикле: {e}")
            db_add_log("error", str(e))

        time.sleep(INTERVAL_SECONDS)

    db_add_log("stop", "Торговый цикл остановлен")
    logger.info("Торговый цикл остановлен")


# =============================================
# ФОРМАТИРОВАНИЕ ОТЧЁТОВ
# =============================================

def format_stats_report(stats: dict) -> str:
    """Форматирование общей статистики"""
    earned = stats["total_earned"] / 100
    spent = stats["total_spent"] / 100
    profit = stats["profit"] / 100
    emoji = "📈" if profit >= 0 else "📉"

    return (
        f"📊 Общая статистика\n"
        f"{'─' * 26}\n"
        f"📦 Продано: {stats['sold_count']} шт. → {earned:.2f} ₽\n"
        f"🛒 Куплено: {stats['bought_count']} шт. → {spent:.2f} ₽\n"
        f"{emoji} Прибыль: {profit:.2f} ₽"
    )


def format_rows(rows, label="предмет"):
    """Форматирование строк из БД в текст"""
    if not rows:
        return "  Пока ничего"
    lines = []
    for name, price, date in rows:
        dt = date[:16] if len(date) > 16 else date
        lines.append(f"  • {name} — {price / 100:.2f} ₽ ({dt})")
    return "\n".join(lines)


# =============================================
# КОМАНДЫ TELEGRAM-БОТА
# =============================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик /start"""
    await update.message.reply_text(
        "🤖 TF2 Trading Bot\n\n"
        "Автоматизация торговли TF2 предметами\n"
        "на steam-trader.net\n\n"
        "📋 Команды:\n"
        "/stats — общая статистика\n"
        "/selling — предметы на продаже\n"
        "/sold — последние продажи\n"
        "/bought — последние покупки\n"
        "/history — история за N дней\n"
        "/profit — прибыль за период\n"
        "/top — топ предметов по выручке\n"
        "/status — статус бота\n"
        "/run — запустить торговлю\n"
        "/stop — остановить торговлю\n"
        "/help — справка"
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stats — общая статистика из SQLite"""
    stats = db_get_stats()
    report = format_stats_report(stats)

    # Добавляем последние продажи
    last_sales = db_get_last_sales(5)
    sales_text = format_rows(last_sales)

    last_purchases = db_get_last_purchases(5)
    purchases_text = format_rows(last_purchases)

    full = (
        f"{report}\n\n"
        f"🕐 Последние продажи:\n{sales_text}\n\n"
        f"🛒 Последние покупки:\n{purchases_text}"
    )
    await update.message.reply_text(full)


async def cmd_selling(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/selling — предметы на продаже (данные из API)"""
    try:
        items = get_inventory(statuses=1)
        if not items:
            await update.message.reply_text("📦 Нет предметов на продаже.")
            return

        total = sum(i.get("price", 0) for i in items) / 100
        lines = [f"📦 На продаже: {len(items)} шт. (на {total:.2f} ₽)\n"]
        for item in items[:20]:
            price = item.get("price", 0) / 100
            lines.append(f"  • {item['hash_name']} — {price:.2f} ₽")
        if len(items) > 20:
            lines.append(f"\n  ...и ещё {len(items) - 20} шт.")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")


async def cmd_sold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sold — последние продажи из БД"""
    rows = db_get_last_sales(10)
    if not rows:
        await update.message.reply_text("📦 Продаж пока нет.")
        return
    text = f"📦 Последние продажи:\n\n{format_rows(rows)}"
    await update.message.reply_text(text)


async def cmd_bought(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/bought — последние покупки из БД"""
    rows = db_get_last_purchases(10)
    if not rows:
        await update.message.reply_text("🛒 Покупок пока нет.")
        return
    text = f"🛒 Последние покупки:\n\n{format_rows(rows)}"
    await update.message.reply_text(text)


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/history N — история за последние N дней (по умолчанию 7)"""
    # Парсим аргумент (количество дней)
    days = 7
    if context.args:
        try:
            days = int(context.args[0])
            days = max(1, min(days, 365))  # ограничение 1-365
        except ValueError:
            pass

    h = db_get_history(days)
    profit = h["profit"] / 100
    emoji = "📈" if profit >= 0 else "📉"

    text = (
        f"📅 История за {h['days']} дней\n"
        f"{'─' * 26}\n"
        f"📦 Продаж: {h['sales_count']} шт. → {h['sales_sum'] / 100:.2f} ₽\n"
        f"🛒 Покупок: {h['purchases_count']} шт. → {h['purchases_sum'] / 100:.2f} ₽\n"
        f"{emoji} Прибыль: {profit:.2f} ₽"
    )
    await update.message.reply_text(text)


async def cmd_profit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/profit N — прибыль по дням за N дней (по умолчанию 30)"""
    days = 30
    if context.args:
        try:
            days = int(context.args[0])
            days = max(1, min(days, 365))
        except ValueError:
            pass

    sales_by_day, purchases_by_day = db_get_profit_by_period(days)

    # Собираем данные в словарь по дням
    daily = {}
    for day, total in sales_by_day:
        daily.setdefault(day, {"sales": 0, "purchases": 0})
        daily[day]["sales"] = total
    for day, total in purchases_by_day:
        daily.setdefault(day, {"sales": 0, "purchases": 0})
        daily[day]["purchases"] = total

    if not daily:
        await update.message.reply_text(f"📅 За последние {days} дней операций нет.")
        return

    lines = [f"💰 Прибыль по дням ({days} дн.)\n{'─' * 26}"]
    total_profit = 0
    for day in sorted(daily.keys(), reverse=True)[:15]:  # макс 15 дней
        s = daily[day]["sales"] / 100
        p = daily[day]["purchases"] / 100
        profit = s - p
        total_profit += profit
        emoji = "📈" if profit >= 0 else "📉"
        lines.append(f"  {day}: {emoji} {profit:+.2f} ₽  (продажи {s:.0f}, покупки {p:.0f})")

    lines.append(f"\n{'─' * 26}")
    lines.append(f"💎 Итого: {total_profit:+.2f} ₽")
    await update.message.reply_text("\n".join(lines))


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/top — топ предметов по выручке"""
    rows = db_get_top_items(10)
    if not rows:
        await update.message.reply_text("📊 Данных пока нет.")
        return

    lines = [f"🏆 Топ предметов по выручке\n{'─' * 26}"]
    for i, (name, cnt, total) in enumerate(rows, 1):
        lines.append(f"  {i}. {name} — {total / 100:.2f} ₽ ({cnt} шт.)")
    await update.message.reply_text("\n".join(lines))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/status — текущее состояние бота"""
    status = "✅ Работает" if bot_running else "⏸ Остановлен"
    await update.message.reply_text(
        f"🤖 Статус: {status}\n"
        f"🔄 Циклов: {cycle_count}\n"
        f"⏱ Интервал: {INTERVAL_SECONDS} сек.\n"
        f"🗄 БД: {DB_FILE}"
    )


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/run — запустить торговлю"""
    global bot_running
    if bot_running:
        await update.message.reply_text("⚠️ Бот уже работает!")
        return
    bot_running = True
    thread = threading.Thread(target=trading_loop, daemon=True)
    thread.start()
    await update.message.reply_text("✅ Торговля запущена!")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stop — остановить торговлю"""
    global bot_running
    if not bot_running:
        await update.message.reply_text("⚠️ Бот уже остановлен!")
        return
    bot_running = False
    await update.message.reply_text("⏸ Торговля остановлена.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/help — справка"""
    await update.message.reply_text(
        "📖 Справка по командам:\n\n"
        "/start — приветствие\n"
        "/stats — общая статистика\n"
        "/selling — что на продаже (API)\n"
        "/sold — последние продажи (БД)\n"
        "/bought — последние покупки (БД)\n"
        "/history 7 — история за N дней\n"
        "/profit 30 — прибыль по дням\n"
        "/top — топ предметов по выручке\n"
        "/status — статус торгового цикла\n"
        "/run — запустить торговлю\n"
        "/stop — остановить\n"
        "/help — эта справка"
    )


# =============================================
# ТОЧКА ВХОДА
# =============================================

def main():
    """Запуск бота"""
    if not TG_TOKEN:
        print("❌ Ошибка: TG_TOKEN не задан в .env!")
        return

    # Инициализация БД при старте
    init_db()

    app = ApplicationBuilder().token(TG_TOKEN).build()

    # Регистрация команд
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("selling", cmd_selling))
    app.add_handler(CommandHandler("sold", cmd_sold))
    app.add_handler(CommandHandler("bought", cmd_bought))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("profit", cmd_profit))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("help", cmd_help))

    logger.info("🤖 Бот запущен! Ctrl+C для остановки.")
    app.run_polling()


if __name__ == "__main__":
    main()
