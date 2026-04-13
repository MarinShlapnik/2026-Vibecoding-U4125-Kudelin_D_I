# 🤖 TF2 Trading Bot (v2 — с SQLite)

Telegram-бот для автоматизации торговли предметами Team Fortress 2 на площадке [steam-trader.net](https://steam-trader.net).

## Источники данных

| Источник | Тип | Описание |
|----------|-----|----------|
| steam-trader.net API | REST API | Инвентарь, цены, покупка/продажа |
| SQLite (`trading_bot.db`) | База данных | Статистика продаж, покупок, логи |

## Структура базы данных

```sql
-- Таблица продаж
CREATE TABLE sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_name TEXT NOT NULL,
    price INTEGER NOT NULL,          -- цена в копейках
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Таблица покупок
CREATE TABLE purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_name TEXT NOT NULL,
    price INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Лог действий бота
CREATE TABLE bot_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,             -- start/stop/sale/buy/error/reprice
    details TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## Команды бота

| Команда       | Описание                              | Источник данных |
|---------------|---------------------------------------|-----------------|
| `/start`      | Приветствие                           | —               |
| `/stats`      | Общая статистика                      | SQLite          |
| `/selling`    | Предметы на продаже                   | API             |
| `/sold`       | Последние продажи                     | SQLite          |
| `/bought`     | Последние покупки                     | SQLite          |
| `/history N`  | История за N дней                     | SQLite          |
| `/profit N`   | Прибыль по дням за N дней             | SQLite          |
| `/top`        | Топ предметов по выручке              | SQLite          |
| `/status`     | Статус торгового цикла                | —               |
| `/run`        | Запустить торговлю                    | API             |
| `/stop`       | Остановить торговлю                   | —               |

## Установка и запуск

```bash
pip install -r requirements.txt
cp .env.example .env
# заполнить .env
python bot.py
```

## Стек технологий

- **Python 3.10+**
- **python-telegram-bot** — Telegram Bot API
- **requests** — HTTP-запросы к steam-trader.net API
- **sqlite3** — встроенная БД Python (хранение статистики)
- **python-dotenv** — конфигурация через .env

## Структура проекта

```
├── bot.py              # Основной код бота
├── trading_bot.db      # SQLite база данных (создаётся автоматически)
├── requirements.txt    # Зависимости
├── .env.example        # Пример конфигурации
├── .env                # Конфигурация (не в git)
└── README.md           # Документация
```
