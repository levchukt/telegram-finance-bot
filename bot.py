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
DB_NAME = "/data/finance.db"

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


def add_transaction(user_id, tx_type, amount, category, comment, scope):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO transactions (user_id, tx_type, scope, category, amount, comment, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_id, tx_type, scope, category, amount, comment, datetime.now().isoformat()))

    conn.commit()
    conn.close()


def parse_amount(text):
    text = text.replace(",", ".").strip()
    try:
        value = Decimal(text)
        if value <= 0:
            return None
        return float(value)
    except:
        return None


# ===== Статистика =====
def get_stats(user_id):
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
    return income, expense


def format_stats(user_id):
    income, expense = get_stats(user_id)
    balance = income - expense

    return (
        f"Доходи: {income:.2f} грн\n"
        f"Витрати: {expense:.2f} грн\n"
        f"Баланс: {balance:.2f} грн"
    )


# ===== Команди =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    register_user(user_id, chat_id)

    await update.message.reply_text(
        "Привіт. Обери дію:",
        reply_markup=main_keyboard()
    )
    return CHOOSING


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Скасовано", reply_markup=main_keyboard())
    return CHOOSING


# ===== Дохід =====
async def start_income(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["type"] = "income"

    await update.message.reply_text(
        "ФОП чи Особисті?",
        reply_markup=keyboard_from_list(INCOME_SCOPES)
    )
    return INCOME_SCOPE


async def income_scope(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["scope"] = update.message.text

    await update.message.reply_text(
        "Категорія:",
        reply_markup=keyboard_from_list(INCOME_CATEGORIES)
    )
    return INCOME_CATEGORY


async def income_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["category"] = update.message.text

    await update.message.reply_text("Сума:", reply_markup=ReplyKeyboardRemove())
    return INCOME_AMOUNT


async def income_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount = parse_amount(update.message.text)

    if not amount:
        await update.message.reply_text("Некоректна сума")
        return INCOME_AMOUNT

    context.user_data["amount"] = amount

    await update.message.reply_text("Коментар або -")
    return INCOME_COMMENT


async def income_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comment = update.message.text
    if comment == "-":
        comment = None

    context.user_data["comment"] = comment

    add_transaction(
        update.effective_user.id,
        "income",
        context.user_data["amount"],
        context.user_data["category"],
        comment,
        context.user_data["scope"]
    )

    await update.message.reply_text(
        "Дохід додано\n\n" + format_stats(update.effective_user.id),
        reply_markup=main_keyboard()
    )
    return CHOOSING


# ===== Витрати =====
async def start_expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["type"] = "expense"

    await update.message.reply_text(
        "Категорія:",
        reply_markup=keyboard_from_list(EXPENSE_CATEGORIES)
    )
    return EXPENSE_CATEGORY


async def expense_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["category"] = update.message.text

    await update.message.reply_text("Сума:", reply_markup=ReplyKeyboardRemove())
    return EXPENSE_AMOUNT


async def expense_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount = parse_amount(update.message.text)

    if not amount:
        await update.message.reply_text("Некоректна сума")
        return EXPENSE_AMOUNT

    context.user_data["amount"] = amount

    await update.message.reply_text("Коментар або -")
    return EXPENSE_COMMENT


async def expense_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comment = update.message.text
    if comment == "-":
        comment = None

    add_transaction(
        update.effective_user.id,
        "expense",
        context.user_data["amount"],
        context.user_data["category"],
        comment,
        None
    )

    await update.message.reply_text(
        "Витрата додана\n\n" + format_stats(update.effective_user.id),
        reply_markup=main_keyboard()
    )
    return CHOOSING


# ===== Меню =====
async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "Додати дохід":
        return await start_income(update, context)

    if text == "Додати витрату":
        return await start_expense(update, context)

    await update.message.reply_text("Використай кнопки", reply_markup=main_keyboard())
    return CHOOSING


# ===== MAIN =====
def main():
    if not TOKEN:
        raise ValueError("BOT_TOKEN не заданий")

    init_db()

    app = ApplicationBuilder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu)],
            INCOME_SCOPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, income_scope)],
            INCOME_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, income_category)],
            INCOME_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, income_amount)],
            INCOME_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, income_comment)],
            EXPENSE_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, expense_category)],
            EXPENSE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, expense_amount)],
            EXPENSE_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, expense_comment)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)

    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
