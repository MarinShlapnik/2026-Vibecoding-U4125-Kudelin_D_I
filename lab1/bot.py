"""
TF2 Trading Bot — Telegram-бот для автоматизации торговли предметами TF2
на площадке steam-trader.net

Функционал:
- Автоматическое выставление предметов на продажу с андерпрайсингом (−1 коп.)
- Автоматическая покупка предметов по заданному прайс-листу
- Отслеживание продаж и уведомления в Telegram
- Статистика продаж/покупок через команды бота
- Хранение данных в JSON-файле

Команды бота:
/start   — приветствие и список команд
/stats   — общая статистика (продажи + покупки)
/selling — список предметов, выставленных на продажу
/bought  — последние купленные предметы
/status  — текущий статус бота (работает/остановлен)
/help    — справка по командам
"""

import os
import json
import time
import logging
import threading
import requests
import warnings
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, BotCommand
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
PROXIES = {"http": None, "https": None}  # отключаем прокси чтобы избежать SSL-ошибок

# Параметры торговли
GAME_ID = 440  # TF2
MIN_PRICE = 700  # минимальная цена по умолчанию (в копейках)
INTERVAL_SECONDS = 60  # интервал между циклами
MY_OFFER_PREFIX = os.getenv("MY_OFFER_PREFIX", "")

# Файлы данных
STATS_FILE = "stats.json"

# Логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# === ЧЁРНЫЙ СПИСОК (не выставлять на продажу) ===
BLACKLIST = [
    "Refined Metal",
    "Reclaimed Metal",
    "Scrap Metal",
    "Mann Co. Supply Crate Key"
]

# === СПИСОК ПОКУПОК: "название": максимальная цена (копейки) ===
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

# === Глобальное состояние ===
bot_running = False
cycle_count = 0
sold_items = set()


# =============================================
# РАБОТА СО СТАТИСТИКОЙ (JSON)
# =============================================

def load_stats():
    """Загрузка статистики из JSON-файла"""
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"total_sold": 0, "total_earned": 0, "total_bought": 0, "total_spent": 0, "sales": [], "purchases": []}


def save_stats(stats):
    """Сохранение статистики в JSON-файл"""
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)


def add_sale(hash_name, price):
    """Запись продажи в статистику"""
    stats = load_stats()
    stats["total_sold"] += 1
    stats["total_earned"] += price
    stats["sales"].append({
        "name": hash_name,
        "price": price,
        "date": datetime.now().strftime("%d.%m.%Y %H:%M")
    })
    save_stats(stats)


def add_purchase(hash_name, price):
    """Запись покупки в статистику"""
    stats = load_stats()
    stats["total_bought"] += 1
    stats["total_spent"] += price
    stats["purchases"].append({
        "name": hash_name,
        "price": price,
        "date": datetime.now().strftime("%d.%m.%Y %H:%M")
    })
    save_stats(stats)


def get_stats_report():
    """Формирование текстового отчёта по статистике"""
    stats = load_stats()
    earned = stats["total_earned"] / 100
    spent = stats.get("total_spent", 0) / 100
    profit = earned - spent

    # Последние 5 продаж
    last_sales = stats.get("sales", [])[-5:]
    sales_text = "\n".join([
        f"  • {s['name']} — {s['price'] / 100:.2f} ₽ ({s['date']})"
        for s in reversed(last_sales)
    ]) or "  Пока ничего"

    # Последние 5 покупок
    last_purchases = stats.get("purchases", [])[-5:]
    purchases_text = "\n".join([
        f"  • {p['name']} — {p['price'] / 100:.2f} ₽ ({p['date']})"
        for p in reversed(last_purchases)
    ]) or "  Пока ничего"

    return (
        f"📊 Статистика бота\n"
        f"{'=' * 28}\n"
        f"📦 Продано: {stats['total_sold']} шт. на {earned:.2f} ₽\n"
        f"🛒 Куплено: {stats.get('total_bought', 0)} шт. на {spent:.2f} ₽\n"
        f"💎 Прибыль: {profit:.2f} ₽\n\n"
        f"🕐 Последние продажи:\n{sales_text}\n\n"
        f"🛒 Последние покупки:\n{purchases_text}"
    )


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
    """Получить инвентарь по статусу (0=новые, 1=на продаже, 2=проданные)"""
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
        add_to_sale(item["id"], new_price)
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


def check_sold():
    """Проверить проданные предметы и записать в статистику"""
    global sold_items
    items = get_inventory(statuses=2)
    for item in items:
        if item["id"] not in sold_items:
            sold_items.add(item["id"])
            price = item.get("price", 0)
            hash_name = item["hash_name"]
            add_sale(hash_name, price)
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
                add_purchase(hash_name, cheapest["price"])
                send_tg_raw(f"🛒 Куплено!\n📦 {hash_name}\n💰 {cheapest['price'] / 100:.2f} ₽")
                logger.info(f"КУПЛЕНО: {hash_name} за {cheapest['price'] / 100:.2f} ₽")
        except Exception as e:
            logger.warning(f"Ошибка покупки {hash_name}: {e}")
        time.sleep(1)


def trading_loop():
    """Основной фоновый цикл торговли"""
    global bot_running, cycle_count
    logger.info("Торговый цикл запущен")
    send_tg_raw("🤖 Торговый бот запущен!")

    while bot_running:
        try:
            sell_new_items()
            update_prices()
            check_sold()
            check_and_buy()

            cycle_count += 1
            # Каждый час отправляем отчёт
            if cycle_count % 60 == 0:
                send_tg_raw(get_stats_report())

        except Exception as e:
            logger.error(f"Ошибка в цикле: {e}")

        time.sleep(INTERVAL_SECONDS)

    logger.info("Торговый цикл остановлен")


# =============================================
# КОМАНДЫ TELEGRAM-БОТА
# =============================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    await update.message.reply_text(
        "🤖 TF2 Trading Bot\n\n"
        "Автоматизация торговли предметами TF2\n"
        "на площадке steam-trader.net\n\n"
        "📋 Команды:\n"
        "/stats — статистика продаж и покупок\n"
        "/selling — предметы на продаже\n"
        "/bought — последние покупки\n"
        "/status — статус бота\n"
        "/run — запустить торговлю\n"
        "/stop — остановить торговлю\n"
        "/help — справка"
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /stats — показать статистику"""
    report = get_stats_report()
    await update.message.reply_text(report)


async def cmd_selling(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /selling — список предметов на продаже"""
    try:
        items = get_inventory(statuses=1)
        if not items:
            await update.message.reply_text("📦 Нет предметов на продаже.")
            return

        total = sum(i.get("price", 0) for i in items) / 100
        lines = [f"📦 На продаже: {len(items)} шт. (на {total:.2f} ₽)\n"]
        for item in items[:20]:  # показываем первые 20
            price = item.get("price", 0) / 100
            lines.append(f"  • {item['hash_name']} — {price:.2f} ₽")
        if len(items) > 20:
            lines.append(f"\n  ...и ещё {len(items) - 20} шт.")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")


async def cmd_bought(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /bought — последние покупки"""
    stats = load_stats()
    purchases = stats.get("purchases", [])[-10:]
    if not purchases:
        await update.message.reply_text("🛒 Покупок пока нет.")
        return

    lines = ["🛒 Последние покупки:\n"]
    for p in reversed(purchases):
        lines.append(f"  • {p['name']} — {p['price'] / 100:.2f} ₽ ({p['date']})")
    await update.message.reply_text("\n".join(lines))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /status — текущий статус"""
    status = "✅ Работает" if bot_running else "⏸ Остановлен"
    await update.message.reply_text(
        f"🤖 Статус: {status}\n"
        f"🔄 Циклов: {cycle_count}\n"
        f"⏱ Интервал: {INTERVAL_SECONDS} сек."
    )


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /run — запустить торговлю"""
    global bot_running
    if bot_running:
        await update.message.reply_text("⚠️ Бот уже работает!")
        return
    bot_running = True
    thread = threading.Thread(target=trading_loop, daemon=True)
    thread.start()
    await update.message.reply_text("✅ Торговля запущена!")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /stop — остановить торговлю"""
    global bot_running
    if not bot_running:
        await update.message.reply_text("⚠️ Бот уже остановлен!")
        return
    bot_running = False
    await update.message.reply_text("⏸ Торговля остановлена.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    await update.message.reply_text(
        "📖 Справка по командам:\n\n"
        "/start — приветствие\n"
        "/stats — статистика продаж и покупок\n"
        "/selling — что сейчас на продаже\n"
        "/bought — история покупок\n"
        "/status — статус торгового цикла\n"
        "/run — запустить фоновую торговлю\n"
        "/stop — остановить торговлю\n"
        "/help — эта справка"
    )


# =============================================
# ТОЧКА ВХОДА
# =============================================

def main():
    """Запуск Telegram-бота"""
    if not TG_TOKEN:
        print("❌ Ошибка: TG_TOKEN не задан в .env файле!")
        return

    app = ApplicationBuilder().token(TG_TOKEN).build()

    # Регистрация команд
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("selling", cmd_selling))
    app.add_handler(CommandHandler("bought", cmd_bought))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("help", cmd_help))

    logger.info("🤖 Бот запущен! Нажми Ctrl+C для остановки.")
    app.run_polling()


if __name__ == "__main__":
    main()
