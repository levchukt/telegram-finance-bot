
import csv
import os
import shutil
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from telegram import (
    InputFile,
    ReplyKeyboardMarkup,
    Update,
)

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

TOKEN = os.getenv("BOT_TOKEN")

DEFAULT_DB_PATH = "/data/finance.db"
DB_NAME = os.getenv("DB_PATH", DEFAULT_DB_PATH)

BACKUP_DIR = "/data/backups"

# =========================
# Категорії
# =========================

EXPENSE_CATEGORIES = [
    "Їжа",
    "Транспорт",
    "Реклама",
    "Оренда",
    "Підписки",
    "Техніка",
    "Побут",
    "Розваги",
    "Алкоголь",
    "Кава",
    "Ліки",
    "Одяг",
    "Інше",
]

INCOME_CATEGORIES = [
    "Клієнт",
    "Продаж",
    "Зарплата",
    "Повернення",
    "Інше",
]

INCOME_SCOPES = [
    "ФОП",
    "Особисті"
]

BACK_BUTTON = "Назад"
CANCEL_BUTTON = "Скасувати"

MAIN_MENU = [
    ["Додати дохід", "Додати витрату"],
    ["Сьогодні", "Статистика за місяць"],
    ["Загальна статистика", "По категоріях"],
    ["Останні записи", "Видалити останній запис"],
]

MAIN_BUTTONS = {
    "Додати дохід",
    "Додати витрату",
    "Сьогодні",
    "Статистика за місяць",
    "Загальна статистика",
    "По категоріях",
    "Останні записи",
    "Видалити останній запис",
}

# =========================
# Стани
# =========================

(
    CHOOSING,
    INCOME_SCOPE,
    INCOME_CATEGORY,
    INCOME_AMOUNT,
    INCOME_CONFIRM,
    EXPENSE_CATEGORY,
    EXPENSE_AMOUNT,
    EXPENSE_CONFIRM,
) = range(8)

# =========================
# UI
# =========================

def main_keyboard():
    return ReplyKeyboardMarkup(
        MAIN_MENU,
        resize_keyboard=True,
        is_persistent=True,
    )


def vertical_keyboard(items, back=False):
    rows = [[i] for i in items]

    nav = []

    if back:
        nav.append(BACK_BUTTON)

    nav.append(CANCEL_BUTTON)

    rows.append(nav)

    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def confirm_keyboard():
    return ReplyKeyboardMarkup(
        [["Підтвердити"], [BACK_BUTTON, CANCEL_BUTTON]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


# =========================
# DB
# =========================

def db_conn():
    os.makedirs("/data", exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)

    return sqlite3.connect(DB_NAME)


def init_db():
    conn = db_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tx_type TEXT,
        scope TEXT,
        category TEXT,
        amount REAL,
        created_at TEXT
    )
    """)

    conn.commit()
    conn.close()


def add_transaction(tx_type, scope, category, amount):
    conn = db_conn()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO transactions
    (tx_type, scope, category, amount, created_at)
    VALUES (?, ?, ?, ?, ?)
    """, (
        tx_type,
        scope,
        category,
        amount,
        datetime.now().isoformat()
    ))

    conn.commit()
    conn.close()


def delete_last_transaction():
    conn = db_conn()
    cur = conn.cursor()

    cur.execute("""
    SELECT id
    FROM transactions
    ORDER BY id DESC
    LIMIT 1
    """)

    row = cur.fetchone()

    if row:
        cur.execute(
            "DELETE FROM transactions WHERE id = ?",
            (row[0],)
        )

        conn.commit()

    conn.close()


def get_all_transactions():
    conn = db_conn()
    cur = conn.cursor()

    cur.execute("""
    SELECT
        tx_type,
        scope,
        category,
        amount,
        created_at
    FROM transactions
    ORDER BY created_at DESC
    """)

    rows = cur.fetchall()

    conn.close()

    return rows


# =========================
# Helpers
# =========================

def parse_amount(text):
    text = text.replace(",", ".")

    try:
        value = Decimal(text)

        if value <= 0:
            return None

        return float(value)

    except (InvalidOperation, ValueError):
        return None


def month_stats():
    now = datetime.now()

    start = datetime(
        now.year,
        now.month,
        1
    ).isoformat()

    conn = db_conn()
    cur = conn.cursor()

    cur.execute("""
    SELECT
        COALESCE(SUM(CASE WHEN tx_type='income' THEN amount END),0),
        COALESCE(SUM(CASE WHEN tx_type='expense' THEN amount END),0)
    FROM transactions
    WHERE created_at >= ?
    """, (start,))

    income, expense = cur.fetchone()

    conn.close()

    return income, expense


def total_stats():
    conn = db_conn()
    cur = conn.cursor()

    cur.execute("""
    SELECT
        COALESCE(SUM(CASE WHEN tx_type='income' THEN amount END),0),
        COALESCE(SUM(CASE WHEN tx_type='expense' THEN amount END),0)
    FROM transactions
    """)

    income, expense = cur.fetchone()

    conn.close()

    return income, expense


def format_stats():
    month_income, month_expense = month_stats()

    total_income, total_expense = total_stats()

    return (
        f"📅 Місяць:\n"
        f"Доходи: {month_income:.2f} грн\n"
        f"Витрати: {month_expense:.2f} грн\n"
        f"Баланс: {(month_income - month_expense):.2f} грн\n\n"
        f"📊 Загалом:\n"
        f"Доходи: {total_income:.2f} грн\n"
        f"Витрати: {total_expense:.2f} грн\n"
        f"Баланс: {(total_income - total_expense):.2f} грн"
    )


# =========================
# Backup
# =========================

def create_backup():
    if not os.path.exists(DB_NAME):
        return None

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")

    backup_name = f"finance_backup_{timestamp}.db"

    backup_path = os.path.join(
        BACKUP_DIR,
        backup_name
    )

    shutil.copy2(DB_NAME, backup_path)

    return backup_path


async def auto_backup(context: ContextTypes.DEFAULT_TYPE):
    backup_path = create_backup()

    if not backup_path:
        return

    admin_chat_id = os.getenv("ADMIN_CHAT_ID")

    if not admin_chat_id:
        return

    try:
        with open(backup_path, "rb") as file:
            await context.bot.send_document(
                chat_id=admin_chat_id,
                document=InputFile(
                    file,
                    filename=os.path.basename(backup_path)
                ),
                caption="Автоматичний backup БД"
            )

    except Exception as e:
        print("Backup error:", e)


# =========================
# Universal
# =========================

async def cancel(update, context):
    context.user_data.clear()

    await update.message.reply_text(
        "Дію скасовано",
        reply_markup=main_keyboard()
    )

    return CHOOSING


async def handle_redirect(update, context):
    text = update.message.text

    if text == CANCEL_BUTTON:
        return await cancel(update, context)

    if text in MAIN_BUTTONS:
        context.user_data.clear()

        return await handle_menu(update, context)

    return None


# =========================
# Start
# =========================

async def start(update, context):
    await update.message.reply_text(
        "Обери дію:",
        reply_markup=main_keyboard()
    )

    return CHOOSING


# =========================
# Income
# =========================

async def start_income(update, context):
    context.user_data.clear()

    await update.message.reply_text(
        "ФОП чи Особисті?",
        reply_markup=vertical_keyboard(INCOME_SCOPES)
    )

    return INCOME_SCOPE


async def income_scope(update, context):
    redirected = await handle_redirect(update, context)

    if redirected is not None:
        return redirected

    text = update.message.text

    if text == BACK_BUTTON:
        return await cancel(update, context)

    context.user_data["scope"] = text

    await update.message.reply_text(
        "Категорія доходу:",
        reply_markup=vertical_keyboard(
            INCOME_CATEGORIES,
            back=True
        )
    )

    return INCOME_CATEGORY


async def income_category(update, context):
    redirected = await handle_redirect(update, context)

    if redirected is not None:
        return redirected

    text = update.message.text

    if text == BACK_BUTTON:
        return await start_income(update, context)

    context.user_data["category"] = text

    await update.message.reply_text(
        "Введи суму:",
        reply_markup=vertical_keyboard([], back=True)
    )

    return INCOME_AMOUNT


async def income_amount(update, context):
    redirected = await handle_redirect(update, context)

    if redirected is not None:
        return redirected

    text = update.message.text

    if text == BACK_BUTTON:
        return await income_scope(update, context)

    amount = parse_amount(text)

    if amount is None:
        await update.message.reply_text(
            "Некоректна сума"
        )

        return INCOME_AMOUNT

    context.user_data["amount"] = amount

    await update.message.reply_text(
        f"Підтвердити дохід?\n\n"
        f"{context.user_data['scope']}\n"
        f"{context.user_data['category']}\n"
        f"{amount:.2f} грн",
        reply_markup=confirm_keyboard()
    )

    return INCOME_CONFIRM


async def income_confirm(update, context):
    redirected = await handle_redirect(update, context)

    if redirected is not None:
        return redirected

    text = update.message.text

    if text == BACK_BUTTON:
        return await income_category(update, context)

    if text != "Підтвердити":
        return INCOME_CONFIRM

    add_transaction(
        "income",
        context.user_data["scope"],
        context.user_data["category"],
        context.user_data["amount"],
    )

    context.user_data.clear()

    await update.message.reply_text(
        "✅ Дохід додано\n\n" + format_stats(),
        reply_markup=main_keyboard()
    )

    return CHOOSING


# =========================
# Expense
# =========================

async def start_expense(update, context):
    context.user_data.clear()

    await update.message.reply_text(
        "Категорія витрати:",
        reply_markup=vertical_keyboard(EXPENSE_CATEGORIES)
    )

    return EXPENSE_CATEGORY


async def expense_category(update, context):
    redirected = await handle_redirect(update, context)

    if redirected is not None:
        return redirected

    text = update.message.text

    if text == BACK_BUTTON:
        return await cancel(update, context)

    context.user_data["category"] = text

    await update.message.reply_text(
        "Введи суму:",
        reply_markup=vertical_keyboard([], back=True)
    )

    return EXPENSE_AMOUNT


async def expense_amount(update, context):
    redirected = await handle_redirect(update, context)

    if redirected is not None:
        return redirected

    text = update.message.text

    if text == BACK_BUTTON:
        return await expense_category(update, context)

    amount = parse_amount(text)

    if amount is None:
        await update.message.reply_text(
            "Некоректна сума"
        )

        return EXPENSE_AMOUNT

    context.user_data["amount"] = amount

    await update.message.reply_text(
        f"Підтвердити витрату?\n\n"
        f"{context.user_data['category']}\n"
        f"{amount:.2f} грн",
        reply_markup=confirm_keyboard()
    )

    return EXPENSE_CONFIRM


async def expense_confirm(update, context):
    redirected = await handle_redirect(update, context)

    if redirected is not None:
        return redirected

    text = update.message.text

    if text == BACK_BUTTON:
        return await expense_category(update, context)

    if text != "Підтвердити":
        return EXPENSE_CONFIRM

    add_transaction(
        "expense",
        None,
        context.user_data["category"],
        context.user_data["amount"],
    )

    context.user_data.clear()

    await update.message.reply_text(
        "✅ Витрату додано\n\n" + format_stats(),
        reply_markup=main_keyboard()
    )

    return CHOOSING


# =========================
# Menu
# =========================

async def today(update, context):
    await update.message.reply_text(
        format_stats(),
        reply_markup=main_keyboard()
    )

    return CHOOSING


async def monthly(update, context):
    income, expense = month_stats()

    await update.message.reply_text(
        f"📅 За місяць:\n\n"
        f"Доходи: {income:.2f} грн\n"
        f"Витрати: {expense:.2f} грн\n"
        f"Баланс: {(income-expense):.2f} грн",
        reply_markup=main_keyboard()
    )

    return CHOOSING


async def total(update, context):
    income, expense = total_stats()

    await update.message.reply_text(
        f"📊 Загалом:\n\n"
        f"Доходи: {income:.2f} грн\n"
        f"Витрати: {expense:.2f} грн\n"
        f"Баланс: {(income-expense):.2f} грн",
        reply_markup=main_keyboard()
    )

    return CHOOSING


async def categories(update, context):
    conn = db_conn()
    cur = conn.cursor()

    cur.execute("""
    SELECT category, SUM(amount)
    FROM transactions
    WHERE tx_type='expense'
    GROUP BY category
    ORDER BY SUM(amount) DESC
    """)

    rows = cur.fetchall()

    conn.close()

    if not rows:
        text = "Немає витрат"

    else:
        text = "📂 Категорії витрат:\n\n"

        for category, amount in rows:
            text += f"{category}: {amount:.2f} грн\n"

    await update.message.reply_text(
        text,
        reply_markup=main_keyboard()
    )

    return CHOOSING


async def recent(update, context):
    rows = get_all_transactions()[:10]

    if not rows:
        await update.message.reply_text(
            "Записів немає",
            reply_markup=main_keyboard()
        )

        return CHOOSING

    text = "🕘 Останні записи:\n\n"

    for row in rows:
        tx_type, scope, category, amount, created_at = row

        sign = "+" if tx_type == "income" else "-"

        text += (
            f"{created_at[:16]}\n"
            f"{sign}{amount:.2f} грн | {category}\n"
        )

        if scope:
            text += f"{scope}\n"

        text += "\n"

    await update.message.reply_text(
        text,
        reply_markup=main_keyboard()
    )

    return CHOOSING


async def delete_last(update, context):
    delete_last_transaction()

    await update.message.reply_text(
        "🗑 Останній запис видалено",
        reply_markup=main_keyboard()
    )

    return CHOOSING


# =========================
# Export DB
# =========================

async def export_db(update, context):
    if not os.path.exists(DB_NAME):
        await update.message.reply_text(
            "База даних не знайдена"
        )

        return

    with open(DB_NAME, "rb") as file:
        await update.message.reply_document(
            document=InputFile(
                file,
                filename="finance.db"
            ),
            caption="SQLite база"
        )


# =========================
# Export Excel CSV
# =========================

async def export_excel(update, context):
    rows = get_all_transactions()

    filename = "finance_export.csv"

    with open(
        filename,
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as f:

        writer = csv.writer(f)

        writer.writerow([
            "Тип",
            "Позначення",
            "Категорія",
            "Сума",
            "Дата"
        ])

        for row in rows:
            writer.writerow(row)

    with open(filename, "rb") as file:
        await update.message.reply_document(
            document=InputFile(
                file,
                filename=filename
            ),
            caption="CSV експорт"
        )

    os.remove(filename)


# =========================
# Router
# =========================

async def handle_menu(update, context):
    text = update.message.text

    if text == "Додати дохід":
        return await start_income(update, context)

    if text == "Додати витрату":
        return await start_expense(update, context)

    if text == "Сьогодні":
        return await today(update, context)

    if text == "Статистика за місяць":
        return await monthly(update, context)

    if text == "Загальна статистика":
        return await total(update, context)

    if text == "По категоріях":
        return await categories(update, context)

    if text == "Останні записи":
        return await recent(update, context)

    if text == "Видалити останній запис":
        return await delete_last(update, context)

    if text == CANCEL_BUTTON:
        return await cancel(update, context)

    await update.message.reply_text(
        "Використай кнопки",
        reply_markup=main_keyboard()
    )

    return CHOOSING


# =========================
# Main
# =========================

def main():
    init_db()

    app = ApplicationBuilder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start)
        ],

        states={
            CHOOSING: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    handle_menu
                ),
            ],

            INCOME_SCOPE: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    income_scope
                ),
            ],

            INCOME_CATEGORY: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    income_category
                ),
            ],

            INCOME_AMOUNT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    income_amount
                ),
            ],

            INCOME_CONFIRM: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    income_confirm
                ),
            ],

            EXPENSE_CATEGORY: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    expense_category
                ),
            ],

            EXPENSE_AMOUNT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    expense_amount
                ),
            ],

            EXPENSE_CONFIRM: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    expense_confirm
                ),
            ],
        },

        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("cancel", cancel),
        ],
    )

    app.add_handler(conv_handler)

    app.add_handler(
        CommandHandler(
            "exportdb",
            export_db
        )
    )

    app.add_handler(
        CommandHandler(
            "exportexcel",
            export_excel
        )
    )

    # backup раз в 14 днів
    app.job_queue.run_repeating(
        auto_backup,
        interval=60 * 60 * 24 * 14,
        first=60,
    )

    print("Bot started")

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()

