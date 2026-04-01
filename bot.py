import os
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)


TOKEN = os.getenv("BOT_TOKEN")
DB_NAME = "finance.db"

# ===== Категорії =====
EXPENSE_CATEGORIES = [
    "Їжа",
    "Транспорт",
    "Реклама",
    "Оренда",
    "Підписки",
    "Техніка",
    "Побут",
    "Інше",
]

INCOME_CATEGORIES = [
    "Клієнт",
    "Продаж",
    "Зарплата",
    "Повернення",
    "Інше",
]

INCOME_SCOPES = ["ФОП", "Особисті"]

# ===== Стани =====
CHOOSING = 1

INCOME_SCOPE = 2
INCOME_CATEGORY = 3
INCOME_AMOUNT = 4
INCOME_COMMENT = 5
INCOME_CONFIRM = 6

EXPENSE_CATEGORY = 7
EXPENSE_AMOUNT = 8
EXPENSE_COMMENT = 9
EXPENSE_CONFIRM = 10


# ===== Клавіатури =====
def main_keyboard():
    keyboard = [
        ["Додати дохід", "Додати витрату"],
        ["Сьогодні", "Статистика за місяць"],
        ["Загальна статистика", "По категоріях за місяць"],
        ["Останні записи", "Видалити останній запис"],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)


def keyboard_from_list(items, extra_rows=None):
    rows = [[item] for item in items]
    if extra_rows:
        rows.extend(extra_rows)
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)


def confirm_keyboard():
    return ReplyKeyboardMarkup(
        [["Підтвердити", "Скасувати"]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


# ===== БД =====
def get_conn():
    return sqlite3.connect(DB_NAME)


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            last_monthly_report_sent TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            tx_type TEXT NOT NULL CHECK(tx_type IN ('income', 'expense')),
            scope TEXT,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            comment TEXT,
            created_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


def register_user(user_id: int, chat_id: int):
    conn = get_conn()
    cur = conn.cursor()
    now = datetime.now().isoformat()

    cur.execute("""
        INSERT INTO users (user_id, chat_id, created_at, last_monthly_report_sent)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET chat_id = excluded.chat_id
    """, (user_id, chat_id, now, None))

    conn.commit()
    conn.close()


def update_last_monthly_report_sent(user_id: int, month_key: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE users
        SET last_monthly_report_sent = ?
        WHERE user_id = ?
    """, (month_key, user_id))
    conn.commit()
    conn.close()


def get_all_users():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, chat_id, last_monthly_report_sent
        FROM users
    """)
    rows = cur.fetchall()
    conn.close()
    return rows


def add_transaction(user_id: int, tx_type: str, amount: float, category: str, comment: str | None, scope: str | None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO transactions (user_id, tx_type, scope, category, amount, comment, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        tx_type,
        scope,
        category,
        amount,
        comment,
        datetime.now().isoformat()
    ))
    conn.commit()
    conn.close()


def delete_last_transaction(user_id: int):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, tx_type, amount, category, comment, scope, created_at
        FROM transactions
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 1
    """, (user_id,))
    row = cur.fetchone()

    if row:
        cur.execute("DELETE FROM transactions WHERE id = ?", (row[0],))
        conn.commit()

    conn.close()
    return row


def parse_amount(text: str) -> float | None:
    text = text.replace(",", ".").strip()
    try:
        value = Decimal(text)
        if value <= 0:
            return None
        return float(value)
    except (InvalidOperation, ValueError):
        return None


# ===== Дати / діапазони =====
def month_range(dt: datetime | None = None):
    if dt is None:
        dt = datetime.now()

    start = datetime(dt.year, dt.month, 1)

    if dt.month == 12:
        end = datetime(dt.year + 1, 1, 1)
    else:
        end = datetime(dt.year, dt.month + 1, 1)

    return start.isoformat(), end.isoformat()


def day_range(dt: datetime | None = None):
    if dt is None:
        dt = datetime.now()

    start = datetime(dt.year, dt.month, dt.day)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def prev_month_range():
    now = datetime.now()
    first_day_this_month = datetime(now.year, now.month, 1)
    last_day_prev_month = first_day_this_month - timedelta(days=1)
    return month_range(last_day_prev_month), last_day_prev_month.strftime("%Y-%m")


# ===== Запити статистики =====
def get_stats_for_range(user_id: int, start_iso: str, end_iso: str):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN tx_type='income' THEN amount END), 0),
            COALESCE(SUM(CASE WHEN tx_type='expense' THEN amount END), 0)
        FROM transactions
        WHERE user_id = ?
          AND created_at >= ?
          AND created_at < ?
    """, (user_id, start_iso, end_iso))

    income, expense = cur.fetchone()
    conn.close()
    return float(income), float(expense)


def get_total_stats(user_id: int):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN tx_type='income' THEN amount END), 0),
            COALESCE(SUM(CASE WHEN tx_type='expense' THEN amount END), 0)
        FROM transactions
        WHERE user_id = ?
    """, (user_id,))

    income, expense = cur.fetchone()
    conn.close()
    return float(income), float(expense)


def get_recent_transactions(user_id: int, limit: int = 10):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT tx_type, scope, category, amount, comment, created_at
        FROM transactions
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
    """, (user_id, limit))

    rows = cur.fetchall()
    conn.close()
    return rows


def get_month_category_breakdown(user_id: int):
    start, end = month_range()

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT tx_type, category, COALESCE(SUM(amount), 0)
        FROM transactions
        WHERE user_id = ?
          AND created_at >= ?
          AND created_at < ?
        GROUP BY tx_type, category
        ORDER BY tx_type, SUM(amount) DESC
    """, (user_id, start, end))

    rows = cur.fetchall()
    conn.close()
    return rows


def get_prev_month_category_breakdown(user_id: int):
    (start, end), _ = prev_month_range()

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT tx_type, category, COALESCE(SUM(amount), 0)
        FROM transactions
        WHERE user_id = ?
          AND created_at >= ?
          AND created_at < ?
        GROUP BY tx_type, category
        ORDER BY tx_type, SUM(amount) DESC
    """, (user_id, start, end))

    rows = cur.fetchall()
    conn.close()
    return rows


# ===== Форматування =====
def format_overall_stats(user_id: int) -> str:
    month_income, month_expense = get_stats_for_range(user_id, *month_range())
    total_income, total_expense = get_total_stats(user_id)

    month_balance = month_income - month_expense
    total_balance = total_income - total_expense

    return (
        f"📅 За поточний місяць:\n"
        f"Доходи: {month_income:.2f} грн\n"
        f"Витрати: {month_expense:.2f} грн\n"
        f"Баланс: {month_balance:.2f} грн\n\n"
        f"📊 Загалом від початку:\n"
        f"Доходи: {total_income:.2f} грн\n"
        f"Витрати: {total_expense:.2f} грн\n"
        f"Баланс: {total_balance:.2f} грн"
    )


def format_today_stats(user_id: int) -> str:
    income, expense = get_stats_for_range(user_id, *day_range())
    balance = income - expense

    return (
        f"📆 Сьогодні:\n"
        f"Доходи: {income:.2f} грн\n"
        f"Витрати: {expense:.2f} грн\n"
        f"Баланс: {balance:.2f} грн"
    )


def build_category_breakdown_text(rows, title="📂 По категоріях за місяць"):
    incomes = []
    expenses = []

    for tx_type, category, amount in rows:
        if tx_type == "income":
            incomes.append((category, float(amount)))
        else:
            expenses.append((category, float(amount)))

    lines = [title, ""]

    lines.append("Доходи:")
    if incomes:
        for category, amount in incomes:
            lines.append(f"• {category}: {amount:.2f} грн")
    else:
        lines.append("• Немає")

    lines.append("")
    lines.append("Витрати:")
    if expenses:
        for category, amount in expenses:
            lines.append(f"• {category}: {amount:.2f} грн")
    else:
        lines.append("• Немає")

    return "\n".join(lines)


def build_pending_summary(data: dict) -> str:
    tx_type = data.get("tx_type")
    amount = data.get("amount")
    category = data.get("category")
    comment = data.get("comment")
    scope = data.get("scope")

    lines = ["Перевір дані перед збереженням:", ""]

    if tx_type == "income":
        lines.append("Тип: Дохід")
        lines.append(f"Позначення: {scope}")
    else:
        lines.append("Тип: Витрата")

    lines.append(f"Категорія: {category}")
    lines.append(f"Сума: {amount:.2f} грн")
    lines.append(f"Коментар: {comment if comment else '—'}")

    return "\n".join(lines)


def build_monthly_report_text(user_id: int):
    (start, end), month_key = prev_month_range()
    income, expense = get_stats_for_range(user_id, start, end)
    balance = income - expense
    breakdown = get_prev_month_category_breakdown(user_id)

    title = f"📅 Щомісячний звіт за {month_key}"
    stats = (
        f"{title}\n\n"
        f"Доходи: {income:.2f} грн\n"
        f"Витрати: {expense:.2f} грн\n"
        f"Баланс: {balance:.2f} грн\n\n"
    )
    return stats + build_category_breakdown_text(breakdown, "📂 Розбивка по категоріях")


# ===== Команди =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    register_user(user_id, chat_id)

    await update.message.reply_text(
        "Привіт. Я бот для обліку доходів і витрат.\n\n"
        "Вибери дію кнопкою нижче.",
        reply_markup=main_keyboard(),
    )
    return CHOOSING


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Дію скасовано.", reply_markup=main_keyboard())
    return CHOOSING


# ===== Дохід =====
async def start_income(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["tx_type"] = "income"

    await update.message.reply_text(
        "Познач дохід:",
        reply_markup=keyboard_from_list(INCOME_SCOPES, extra_rows=[["Скасувати"]]),
    )
    return INCOME_SCOPE


async def income_scope(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "Скасувати":
        return await cancel(update, context)

    if text not in INCOME_SCOPES:
        await update.message.reply_text("Вибери кнопку: ФОП або Особисті.")
        return INCOME_SCOPE

    context.user_data["scope"] = text

    await update.message.reply_text(
        "Вибери категорію доходу:",
        reply_markup=keyboard_from_list(INCOME_CATEGORIES, extra_rows=[["Скасувати"]]),
    )
    return INCOME_CATEGORY


async def income_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "Скасувати":
        return await cancel(update, context)

    if text not in INCOME_CATEGORIES:
        await update.message.reply_text("Вибери категорію кнопкою.")
        return INCOME_CATEGORY

    context.user_data["category"] = text

    await update.message.reply_text(
        "Введи суму доходу:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return INCOME_AMOUNT


async def income_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount = parse_amount(update.message.text)

    if amount is None:
        await update.message.reply_text("Некоректна сума. Напиши число, наприклад 500 або 500.50")
        return INCOME_AMOUNT

    context.user_data["amount"] = amount
    await update.message.reply_text("Напиши коментар або '-' якщо без коментаря:")
    return INCOME_COMMENT


async def income_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comment = update.message.text.strip()
    if comment == "-":
        comment = None

    context.user_data["comment"] = comment

    await update.message.reply_text(
        build_pending_summary(context.user_data),
        reply_markup=confirm_keyboard(),
    )
    return INCOME_CONFIRM


async def income_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "Скасувати":
        return await cancel(update, context)

    if text != "Підтвердити":
        await update.message.reply_text("Натисни Підтвердити або Скасувати.")
        return INCOME_CONFIRM

    user_id = update.effective_user.id

    add_transaction(
        user_id=user_id,
        tx_type="income",
        amount=context.user_data["amount"],
        category=context.user_data["category"],
        comment=context.user_data.get("comment"),
        scope=context.user_data.get("scope"),
    )

    scope = context.user_data.get("scope")
    amount = context.user_data["amount"]
    category = context.user_data["category"]
    comment = context.user_data.get("comment")

    msg = (
        f"✅ Дохід збережено\n"
        f"Позначення: {scope}\n"
        f"Категорія: {category}\n"
        f"Сума: {amount:.2f} грн\n"
        f"Коментар: {comment if comment else '—'}\n\n"
        f"{format_today_stats(user_id)}\n\n"
        f"{format_overall_stats(user_id)}"
    )

    context.user_data.clear()
    await update.message.reply_text(msg, reply_markup=main_keyboard())
    return CHOOSING


# ===== Витрата =====
async def start_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["tx_type"] = "expense"

    await update.message.reply_text(
        "Вибери категорію витрати:",
        reply_markup=keyboard_from_list(EXPENSE_CATEGORIES, extra_rows=[["Скасувати"]]),
    )
    return EXPENSE_CATEGORY


async def expense_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "Скасувати":
        return await cancel(update, context)

    if text not in EXPENSE_CATEGORIES:
        await update.message.reply_text("Вибери категорію кнопкою.")
        return EXPENSE_CATEGORY

    context.user_data["category"] = text

    await update.message.reply_text(
        "Введи суму витрати:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return EXPENSE_AMOUNT


async def expense_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount = parse_amount(update.message.text)

    if amount is None:
        await update.message.reply_text("Некоректна сума. Напиши число, наприклад 250 або 250.75")
        return EXPENSE_AMOUNT

    context.user_data["amount"] = amount
    await update.message.reply_text("Напиши коментар або '-' якщо без коментаря:")
    return EXPENSE_COMMENT


async def expense_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comment = update.message.text.strip()
    if comment == "-":
        comment = None

    context.user_data["comment"] = comment

    await update.message.reply_text(
        build_pending_summary(context.user_data),
        reply_markup=confirm_keyboard(),
    )
    return EXPENSE_CONFIRM


async def expense_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "Скасувати":
        return await cancel(update, context)

    if text != "Підтвердити":
        await update.message.reply_text("Натисни Підтвердити або Скасувати.")
        return EXPENSE_CONFIRM

    user_id = update.effective_user.id

    add_transaction(
        user_id=user_id,
        tx_type="expense",
        amount=context.user_data["amount"],
        category=context.user_data["category"],
        comment=context.user_data.get("comment"),
        scope=None,
    )

    amount = context.user_data["amount"]
    category = context.user_data["category"]
    comment = context.user_data.get("comment")

    msg = (
        f"✅ Витрату збережено\n"
        f"Категорія: {category}\n"
        f"Сума: {amount:.2f} грн\n"
        f"Коментар: {comment if comment else '—'}\n\n"
        f"{format_today_stats(user_id)}\n\n"
        f"{format_overall_stats(user_id)}"
    )

    context.user_data.clear()
    await update.message.reply_text(msg, reply_markup=main_keyboard())
    return CHOOSING


# ===== Меню =====
async def today_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(format_today_stats(user_id), reply_markup=main_keyboard())
    return CHOOSING


async def month_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    month_income, month_expense = get_stats_for_range(user_id, *month_range())
    month_balance = month_income - month_expense

    text = (
        f"📅 Статистика за поточний місяць:\n"
        f"Доходи: {month_income:.2f} грн\n"
        f"Витрати: {month_expense:.2f} грн\n"
        f"Баланс: {month_balance:.2f} грн"
    )
    await update.message.reply_text(text, reply_markup=main_keyboard())
    return CHOOSING


async def total_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    total_income, total_expense = get_total_stats(user_id)
    total_balance = total_income - total_expense

    text = (
        f"📊 Загальна статистика:\n"
        f"Доходи: {total_income:.2f} грн\n"
        f"Витрати: {total_expense:.2f} грн\n"
        f"Баланс: {total_balance:.2f} грн"
    )
    await update.message.reply_text(text, reply_markup=main_keyboard())
    return CHOOSING


async def category_breakdown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = get_month_category_breakdown(user_id)
    text = build_category_breakdown_text(rows)
    await update.message.reply_text(text, reply_markup=main_keyboard())
    return CHOOSING


async def recent_records(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = get_recent_transactions(user_id)

    if not rows:
        await update.message.reply_text("Записів поки немає.", reply_markup=main_keyboard())
        return CHOOSING

    lines = ["🕘 Останні записи:\n"]

    for tx_type, scope, category, amount, comment, created_at in rows:
        label = "Дохід" if tx_type == "income" else "Витрата"
        sign = "+" if tx_type == "income" else "-"
        date_str = created_at[:16].replace("T", " ")

        parts = [f"{date_str} | {label} | {sign}{amount:.2f} грн | {category}"]

        if scope:
            parts.append(scope)

        if comment:
            parts.append(comment)

        lines.append(" | ".join(parts))

    await update.message.reply_text("\n".join(lines), reply_markup=main_keyboard())
    return CHOOSING


async def delete_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    row = delete_last_transaction(user_id)

    if not row:
        await update.message.reply_text("Немає записів для видалення.", reply_markup=main_keyboard())
        return CHOOSING

    _, tx_type, amount, category, comment, scope, created_at = row
    label = "Дохід" if tx_type == "income" else "Витрата"

    text = (
        f"🗑 Видалено останній запис:\n"
        f"Дата: {created_at[:16].replace('T', ' ')}\n"
        f"Тип: {label}\n"
        f"Категорія: {category}\n"
        f"Сума: {amount:.2f} грн\n"
        f"Позначення: {scope if scope else '—'}\n"
        f"Коментар: {comment if comment else '—'}"
    )

    await update.message.reply_text(text, reply_markup=main_keyboard())
    return CHOOSING


async def send_monthly_reports(context: ContextTypes.DEFAULT_TYPE):
    users = get_all_users()
    (_, month_key) = prev_month_range()

    for user_id, chat_id, last_monthly_report_sent in users:
        if last_monthly_report_sent == month_key:
            continue

        report_text = build_monthly_report_text(user_id)

        try:
            await context.bot.send_message(chat_id=chat_id, text=report_text, reply_markup=main_keyboard())
            update_last_monthly_report_sent(user_id, month_key)
        except Exception:
            pass


async def manual_monthly_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = build_monthly_report_text(user_id)
    await update.message.reply_text(text, reply_markup=main_keyboard())
    return CHOOSING


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "Додати дохід":
        return await start_income(update, context)

    if text == "Додати витрату":
        return await start_expense(update, context)

    if text == "Сьогодні":
        return await today_stats(update, context)

    if text == "Статистика за місяць":
        return await month_stats(update, context)

    if text == "Загальна статистика":
        return await total_stats(update, context)

    if text == "По категоріях за місяць":
        return await category_breakdown(update, context)

    if text == "Останні записи":
        return await recent_records(update, context)

    if text == "Видалити останній запис":
        return await delete_last(update, context)

    if text == "Скасувати":
        return await cancel(update, context)

    await update.message.reply_text("Використай кнопки меню.", reply_markup=main_keyboard())
    return CHOOSING

from flask import Flask
import threading

app_flask = Flask(__name__)

@app_flask.route("/")
def home():
    return "Bot is running"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host="0.0.0.0", port=port)

# запускаємо веб-сервер в окремому потоці
threading.Thread(target=run_web).start()

def main():
    if not TOKEN:
        raise ValueError("Не знайдено BOT_TOKEN. Додай змінну середовища BOT_TOKEN.")

    init_db()

    app = ApplicationBuilder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu),
            ],
            INCOME_SCOPE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, income_scope),
            ],
            INCOME_CATEGORY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, income_category),
            ],
            INCOME_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, income_amount),
            ],
            INCOME_COMMENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, income_comment),
            ],
            INCOME_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, income_confirm),
            ],
            EXPENSE_CATEGORY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, expense_category),
            ],
            EXPENSE_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, expense_amount),
            ],
            EXPENSE_COMMENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, expense_comment),
            ],
            EXPENSE_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, expense_confirm),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("report", manual_monthly_report),
        ],
        per_chat=True,
        per_user=True,
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("report", manual_monthly_report))

    app.job_queue.run_repeating(
        send_monthly_reports,
        interval=86400,
        first=10,
        name="monthly_reports_checker",
    )

    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
