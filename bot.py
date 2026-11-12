# bot.py

import io
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from openpyxl import Workbook

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    ReplyKeyboardMarkup,
)
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

TOKEN = os.getenv("BOT_TOKEN")

DB_NAME = os.getenv("DB_PATH", "/data/finance.db")
DB_DIR = os.path.dirname(DB_NAME) or "."
BACKUP_DIR = os.path.join(DB_DIR, "backups")

# Часовий пояс, у якому рахуються "сьогодні" та "місяць"
TZ = ZoneInfo(os.getenv("BOT_TZ", "Europe/Kyiv"))

ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

# Хто може користуватись ботом: список user id через кому.
# Якщо не задано - бот працює тільки в чаті ADMIN_CHAT_ID.
# Якщо не задано ні те, ні інше - бот відкритий для всіх.
ALLOWED_USER_IDS = [
    int(x)
    for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",")
    if x
]

# =========================
# Категорії
# =========================

EXPENSE_CATEGORIES = [
    "Їжа",
    "Кава",
    "Алкоголь",
    "Оренда",
    "Одяг",
    "Транспорт",
    "Реклама",
    "Підписки",
    "Техніка",
    "Побут",
    "Розваги",
    "Ліки",
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

BACK_BUTTON = "Назад"
CANCEL_BUTTON = "Скасувати"
CONFIRM_BUTTON = "Підтвердити"
ADD_COMMENT_BUTTON = "Додати коментар"
EDIT_COMMENT_BUTTON = "Змінити коментар"

COMMENT_MAX_LENGTH = 200

NO_SCOPE = "Без позначки"

MAIN_MENU = [
    ["Додати дохід", "Додати витрату"],
    ["Сьогодні", "Статистика за місяць"],
    ["Загальна статистика", "По категоріях"],
    ["Останні записи", "Видалити останній запис"],
]

MONTH_NAMES = [
    "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
    "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень",
]

# =========================
# States
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

    COMMENT,

) = range(9)

# =========================
# UI
# =========================

def main_keyboard():
    return ReplyKeyboardMarkup(
        MAIN_MENU,
        resize_keyboard=True,
        is_persistent=True
    )


def vertical_keyboard(items, back=False):
    rows = [[i] for i in items]

    nav = [BACK_BUTTON] if back else []
    nav.append(CANCEL_BUTTON)

    rows.append(nav)

    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        one_time_keyboard=True
    )


def confirm_keyboard(has_comment=False):
    comment_button = (
        EDIT_COMMENT_BUTTON if has_comment else ADD_COMMENT_BUTTON
    )

    return ReplyKeyboardMarkup(
        [
            [CONFIRM_BUTTON],
            [comment_button],
            [BACK_BUTTON, CANCEL_BUTTON],
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )


def money(value):
    return f"{value:,.2f}".replace(",", " ") + " грн"

# =========================
# Час
# =========================
# У базі час зберігається як локальний час сервера (так було
# від початку). Тут він переводиться в часовий пояс TZ і назад,
# щоб "сьогодні" починалось опівночі за Києвом, а не за UTC.

def to_local(iso):
    return datetime.fromisoformat(iso).astimezone(TZ)


def to_db(dt):
    return dt.astimezone().replace(tzinfo=None).isoformat()


def today_range():
    start = datetime.now(TZ).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    return start, start + timedelta(days=1)


def month_start(year, month):
    return datetime(year, month, 1, tzinfo=TZ)


def shift_month(start, delta):
    """Перше число місяця, зсунутого на delta місяців."""
    index = start.year * 12 + (start.month - 1) + delta

    return month_start(index // 12, index % 12 + 1)


def current_month_start():
    now = datetime.now(TZ)

    return month_start(now.year, now.month)

# =========================
# DB
# =========================

def db_conn():
    return sqlite3.connect(DB_NAME)


def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)

    with closing(db_conn()) as conn, conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tx_type TEXT,
            scope TEXT,
            category TEXT,
            amount REAL,
            created_at TEXT
        )
        """)

        columns = [
            row[1]
            for row in conn.execute("PRAGMA table_info(transactions)")
        ]

        if "comment" not in columns:
            conn.execute(
                "ALTER TABLE transactions ADD COLUMN comment TEXT"
            )

        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_transactions_created_at
        ON transactions (created_at)
        """)


def add_transaction(tx_type, scope, category, amount, comment=None):
    with closing(db_conn()) as conn, conn:
        conn.execute(
            """
            INSERT INTO transactions
            (tx_type, scope, category, amount, created_at, comment)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                tx_type,
                scope,
                category,
                amount,
                datetime.now().isoformat(),
                comment,
            )
        )


def delete_last_transaction():
    """Видаляє останній запис і повертає його (або None)."""
    with closing(db_conn()) as conn, conn:
        row = conn.execute("""
        SELECT id, tx_type, scope, category, amount, created_at, comment
        FROM transactions
        ORDER BY id DESC
        LIMIT 1
        """).fetchone()

        if row:
            conn.execute(
                "DELETE FROM transactions WHERE id = ?",
                (row[0],)
            )

    return row[1:] if row else None


def get_transactions(limit=None):
    query = """
    SELECT tx_type, scope, category, amount, created_at, comment
    FROM transactions
    ORDER BY created_at DESC, id DESC
    """

    params = ()

    if limit:
        query += " LIMIT ?"
        params = (limit,)

    with closing(db_conn()) as conn:
        return conn.execute(query, params).fetchall()


def get_stats(start=None, end=None):
    """
    Статистика за період (без start/end - за весь час):
      income   - сума доходів
      expense  - сума витрат
      scopes   - доходи по позначках [(ФОП/Особисті, сума), ...]
      expenses - витрати по категоріях [(категорія, сума), ...]
    """
    where = ""
    params = ()

    if start and end:
        where = "WHERE created_at >= ? AND created_at < ?"
        params = (to_db(start), to_db(end))

    with closing(db_conn()) as conn:
        rows = conn.execute(
            f"""
            SELECT tx_type, scope, category, SUM(amount)
            FROM transactions
            {where}
            GROUP BY tx_type, scope, category
            """,
            params
        ).fetchall()

    scopes = {}
    expenses = {}

    for tx_type, scope, category, amount in rows:
        if tx_type == "income":
            key = scope or NO_SCOPE
            scopes[key] = scopes.get(key, 0) + amount

        else:
            expenses[category] = expenses.get(category, 0) + amount

    def by_amount(data):
        return sorted(data.items(), key=lambda item: -item[1])

    return {
        "income": sum(scopes.values()),
        "expense": sum(expenses.values()),
        "scopes": by_amount(scopes),
        "expenses": by_amount(expenses),
    }

# =========================
# Helpers
# =========================

def parse_amount(text):
    text = (
        text
        .replace(" ", "")
        .replace(" ", "")
        .replace(",", ".")
    )

    try:
        value = Decimal(text)

    except (InvalidOperation, ValueError):
        return None

    if not value.is_finite() or value <= 0:
        return None

    value = round(float(value), 2)

    return value if value > 0 else None


def format_categories(expenses, total):
    lines = []

    for category, amount in expenses:
        percent = amount / total * 100 if total else 0

        lines.append(
            f"{category}: {money(amount)} ({percent:.0f}%)"
        )

    return "\n".join(lines)


def format_report(title, stats, with_categories=True):
    income = stats["income"]
    expense = stats["expense"]

    lines = [title, "", f"Доходи: {money(income)}"]

    for scope, amount in stats["scopes"]:
        lines.append(f"   • {scope}: {money(amount)}")

    lines.append(f"Витрати: {money(expense)}")
    lines.append(f"Баланс: {money(income - expense)}")

    text = "\n".join(lines)

    if with_categories and stats["expenses"]:
        text += (
            "\n\n📂 Витрати по категоріях:\n"
            + format_categories(stats["expenses"], expense)
        )

    return text


def format_delta(label, current, previous):
    diff = current - previous

    sign = "+" if diff >= 0 else "−"

    text = f"{label}: {sign}{money(abs(diff))}"

    if previous:
        text += f" ({sign}{abs(diff) / previous * 100:.0f}%)"

    return text


def format_transaction(
    tx_type, scope, category, amount, created_at, comment=None
):
    sign = "+" if tx_type == "income" else "-"

    text = (
        f"{to_local(created_at):%d.%m.%Y %H:%M}\n"
        f"{sign}{money(amount)} | {category}"
    )

    if scope:
        text += f"\n{scope}"

    if comment:
        text += f"\n💬 {comment}"

    return text


def month_report(start):
    """Текст і кнопки навігації для місяця, що починається в start."""
    end = shift_month(start, 1)
    prev_start = shift_month(start, -1)
    current = current_month_start()

    prev_name = MONTH_NAMES[prev_start.month - 1]

    stats = get_stats(start, end)

    text = format_report(
        f"📅 {MONTH_NAMES[start.month - 1]} {start.year}:",
        stats
    )

    # Поточний місяць ще не закінчився, тому порівнюємо його
    # з таким самим відрізком попереднього (з 1-го по сьогоднішнє число)
    if start == current:
        days = datetime.now(TZ).day

        prev_end = min(prev_start + timedelta(days=days), start)

        days = (prev_end - prev_start).days

        label = (
            f"Порівняно з тим самим періодом попереднього місяця "
            f"({prev_name}, 1–{days} число)"
        )

    else:
        prev_end = start

        label = f"Порівняно з попереднім місяцем ({prev_name})"

    prev = get_stats(prev_start, prev_end)

    if prev["income"] or prev["expense"]:
        text += (
            f"\n\n📈 {label}:\n"
            + format_delta("Доходи", stats["income"], prev["income"])
            + "\n"
            + format_delta("Витрати", stats["expense"], prev["expense"])
        )

    buttons = [
        InlineKeyboardButton(
            f"◀ {prev_name}",
            callback_data=f"month:{prev_start:%Y-%m}"
        )
    ]

    if start < current:
        buttons.append(
            InlineKeyboardButton(
                f"{MONTH_NAMES[end.month - 1]} ▶",
                callback_data=f"month:{end:%Y-%m}"
            )
        )

    return text, InlineKeyboardMarkup([buttons])

# =========================
# Backup
# =========================

def create_backup():
    if not os.path.exists(DB_NAME):
        return None

    os.makedirs(BACKUP_DIR, exist_ok=True)

    timestamp = datetime.now(TZ).strftime("%Y-%m-%d_%H-%M")

    backup_path = os.path.join(
        BACKUP_DIR,
        f"finance_backup_{timestamp}.db"
    )

    # sqlite backup API - безпечна копія навіть під час запису
    with closing(db_conn()) as src, \
            closing(sqlite3.connect(backup_path)) as dst:
        src.backup(dst)

    return backup_path


async def auto_backup(context: ContextTypes.DEFAULT_TYPE):
    try:
        backup_path = create_backup()

        if not backup_path or not ADMIN_CHAT_ID:
            return

        with open(backup_path, "rb") as file:
            await context.bot.send_document(
                chat_id=ADMIN_CHAT_ID,
                document=InputFile(
                    file,
                    filename=os.path.basename(backup_path)
                ),
                caption="Backup БД"
            )

    except Exception as e:
        print("Backup error:", e)

# =========================
# Universal
# =========================

async def reply(update, text, keyboard=None):
    await update.message.reply_text(
        text,
        reply_markup=keyboard or main_keyboard()
    )


async def cancel(update, context):
    context.user_data.clear()

    await reply(update, "Дію скасовано")

    return CHOOSING


async def handle_redirect(update, context):
    """Скасування або кнопка головного меню посеред діалогу."""
    text = update.message.text

    if text == CANCEL_BUTTON:
        return await cancel(update, context)

    if text in MENU_ACTIONS:
        context.user_data.clear()

        return await handle_menu(update, context)

    return None

# =========================
# Start
# =========================

async def start(update, context):
    context.user_data.clear()

    await reply(update, "Обери дію:")

    return CHOOSING

# =========================
# Спільні кроки діалогу
# =========================

async def ask_income_category(update):
    await reply(
        update,
        "Категорія доходу:",
        vertical_keyboard(INCOME_CATEGORIES, back=True)
    )

    return INCOME_CATEGORY


async def ask_expense_category(update):
    await reply(
        update,
        "Категорія витрати:",
        vertical_keyboard(EXPENSE_CATEGORIES)
    )

    return EXPENSE_CATEGORY


async def ask_amount(update, state):
    await reply(
        update,
        "Введи суму:",
        vertical_keyboard([], back=True)
    )

    return state

async def show_confirm(update, context):
    data = context.user_data

    is_income = data["type"] == "income"

    lines = [
        "Підтвердити дохід?" if is_income else "Підтвердити витрату?",
        "",
    ]

    if is_income:
        lines.append(data["scope"])

    lines.append(data["category"])
    lines.append(money(data["amount"]))

    if data.get("comment"):
        lines.append(f"💬 {data['comment']}")

    await reply(
        update,
        "\n".join(lines),
        confirm_keyboard(bool(data.get("comment")))
    )

    return INCOME_CONFIRM if is_income else EXPENSE_CONFIRM


async def ask_comment(update):
    await reply(
        update,
        "Напиши коментар:",
        vertical_keyboard([], back=True)
    )

    return COMMENT


async def comment_entered(update, context):
    redirected = await handle_redirect(update, context)

    if redirected is not None:
        return redirected

    text = update.message.text.strip()

    if text != BACK_BUTTON and text:
        context.user_data["comment"] = text[:COMMENT_MAX_LENGTH]

    return await show_confirm(update, context)

# =========================
# Income
# =========================

async def start_income(update, context):
    context.user_data.clear()
    context.user_data["type"] = "income"

    await reply(
        update,
        "ФОП чи Особисті?",
        vertical_keyboard(INCOME_SCOPES)
    )

    return INCOME_SCOPE


async def income_scope(update, context):
    redirected = await handle_redirect(update, context)

    if redirected is not None:
        return redirected

    text = update.message.text

    if text == BACK_BUTTON:
        return await cancel(update, context)

    if text not in INCOME_SCOPES:
        await reply(
            update,
            "Використай кнопки",
            vertical_keyboard(INCOME_SCOPES)
        )

        return INCOME_SCOPE

    context.user_data["scope"] = text

    return await ask_income_category(update)


async def income_category(update, context):
    redirected = await handle_redirect(update, context)

    if redirected is not None:
        return redirected

    text = update.message.text

    if text == BACK_BUTTON:
        return await start_income(update, context)

    context.user_data["category"] = text

    return await ask_amount(update, INCOME_AMOUNT)


async def income_amount(update, context):
    redirected = await handle_redirect(update, context)

    if redirected is not None:
        return redirected

    text = update.message.text

    if text == BACK_BUTTON:
        return await ask_income_category(update)

    amount = parse_amount(text)

    if amount is None:
        await reply(
            update,
            "Некоректна сума. Введи число, наприклад 250 або 99.50",
            vertical_keyboard([], back=True)
        )

        return INCOME_AMOUNT

    context.user_data["amount"] = amount

    return await show_confirm(update, context)


async def income_confirm(update, context):
    redirected = await handle_redirect(update, context)

    if redirected is not None:
        return redirected

    text = update.message.text

    if text == BACK_BUTTON:
        return await ask_amount(update, INCOME_AMOUNT)

    if text in (ADD_COMMENT_BUTTON, EDIT_COMMENT_BUTTON):
        return await ask_comment(update)

    if text != CONFIRM_BUTTON:
        return await show_confirm(update, context)

    data = context.user_data

    add_transaction(
        "income",
        data["scope"],
        data["category"],
        data["amount"],
        data.get("comment"),
    )

    await reply(
        update,
        f"✅ Дохід додано: {money(data['amount'])} | "
        f"{data['category']} ({data['scope']})"
    )

    context.user_data.clear()

    return CHOOSING

# =========================
# Expense
# =========================

async def start_expense(update, context):
    context.user_data.clear()
    context.user_data["type"] = "expense"

    return await ask_expense_category(update)


async def expense_category(update, context):
    redirected = await handle_redirect(update, context)

    if redirected is not None:
        return redirected

    text = update.message.text

    if text == BACK_BUTTON:
        return await cancel(update, context)

    context.user_data["category"] = text

    return await ask_amount(update, EXPENSE_AMOUNT)


async def expense_amount(update, context):
    redirected = await handle_redirect(update, context)

    if redirected is not None:
        return redirected

    text = update.message.text

    if text == BACK_BUTTON:
        return await ask_expense_category(update)

    amount = parse_amount(text)

    if amount is None:
        await reply(
            update,
            "Некоректна сума. Введи число, наприклад 250 або 99.50",
            vertical_keyboard([], back=True)
        )

        return EXPENSE_AMOUNT

    context.user_data["amount"] = amount

    return await show_confirm(update, context)


async def expense_confirm(update, context):
    redirected = await handle_redirect(update, context)

    if redirected is not None:
        return redirected

    text = update.message.text

    if text == BACK_BUTTON:
        return await ask_amount(update, EXPENSE_AMOUNT)

    if text in (ADD_COMMENT_BUTTON, EDIT_COMMENT_BUTTON):
        return await ask_comment(update)

    if text != CONFIRM_BUTTON:
        return await show_confirm(update, context)

    data = context.user_data

    add_transaction(
        "expense",
        None,
        data["category"],
        data["amount"],
        data.get("comment"),
    )

    # Одразу показуємо, скільки вже витрачено сьогодні
    spent_today = get_stats(*today_range())["expense"]

    await reply(
        update,
        f"✅ Витрату додано: {money(data['amount'])} | "
        f"{data['category']}\n"
        f"Сьогодні витрачено: {money(spent_today)}"
    )

    context.user_data.clear()

    return CHOOSING

# =========================
# Menu
# =========================

async def today(update, context):
    start, end = today_range()

    await reply(
        update,
        format_report(
            f"📅 Сьогодні ({start:%d.%m}):",
            get_stats(start, end)
        )
    )

    return CHOOSING


async def monthly(update, context):
    text, keyboard = month_report(current_month_start())

    await reply(update, text, keyboard)

    return CHOOSING


async def month_navigation(update, context):
    """Кнопки ◀ ▶ під статистикою за місяць."""
    query = update.callback_query

    if not is_allowed(update):
        await query.answer()

        return

    try:
        year, month = map(int, query.data.split(":")[1].split("-"))

        start = min(month_start(year, month), current_month_start())

    except (ValueError, IndexError):
        await query.answer()

        return

    text, keyboard = month_report(start)

    try:
        await query.edit_message_text(text, reply_markup=keyboard)

    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise

    await query.answer()


async def total(update, context):
    await reply(
        update,
        format_report(
            "📊 Загалом:",
            get_stats(),
            with_categories=False
        )
    )

    return CHOOSING


async def categories(update, context):
    stats = get_stats()

    if not stats["expenses"]:
        text = "Немає витрат"

    else:
        text = (
            "📂 Категорії витрат (за весь час):\n\n"
            + format_categories(stats["expenses"], stats["expense"])
        )

    await reply(update, text)

    return CHOOSING


async def recent(update, context):
    rows = get_transactions(limit=10)

    if not rows:
        text = "Записів немає"

    else:
        text = "🕘 Останні записи:\n\n" + "\n\n".join(
            format_transaction(*row) for row in rows
        )

    await reply(update, text)

    return CHOOSING


async def delete_last(update, context):
    row = delete_last_transaction()

    if not row:
        text = "Записів немає"

    else:
        text = (
            "🗑 Останній запис видалено:\n\n"
            + format_transaction(*row)
        )

    await reply(update, text)

    return CHOOSING

# =========================
# Export DB
# =========================

async def export_db(update, context):
    if not os.path.exists(DB_NAME):
        await update.message.reply_text("База даних не знайдена")

        return

    with open(DB_NAME, "rb") as file:
        await update.message.reply_document(
            document=InputFile(file, filename="finance.db"),
            caption="SQLite база"
        )

# =========================
# Export Excel
# =========================

async def export_excel(update, context):
    wb = Workbook()

    ws = wb.active

    ws.title = "Фінанси"

    ws.append([
        "Тип", "Позначення", "Категорія", "Сума", "Дата", "Коментар"
    ])

    for row in get_transactions():
        tx_type, scope, category, amount, created_at, comment = row

        ws.append([
            "Дохід" if tx_type == "income" else "Витрата",
            scope,
            category,
            amount,
            to_local(created_at).strftime("%Y-%m-%d %H:%M"),
            comment,
        ])

    buffer = io.BytesIO()

    wb.save(buffer)

    buffer.seek(0)

    await update.message.reply_document(
        document=InputFile(buffer, filename="finance_export.xlsx"),
        caption="Excel експорт"
    )

# =========================
# Router
# =========================

MENU_ACTIONS = {
    "Додати дохід": start_income,
    "Додати витрату": start_expense,
    "Сьогодні": today,
    "Статистика за місяць": monthly,
    "Загальна статистика": total,
    "По категоріях": categories,
    "Останні записи": recent,
    "Видалити останній запис": delete_last,
    CANCEL_BUTTON: cancel,
}


async def handle_menu(update, context):
    action = MENU_ACTIONS.get(update.message.text)

    if action:
        return await action(update, context)

    await reply(update, "Використай кнопки")

    return CHOOSING

# =========================
# Main
# =========================

def is_allowed(update):
    """Та сама перевірка доступу, що й access_filter, для inline-кнопок."""
    if ALLOWED_USER_IDS:
        user = update.effective_user

        return bool(user) and user.id in ALLOWED_USER_IDS

    if ADMIN_CHAT_ID:
        chat = update.effective_chat

        return bool(chat) and chat.id == int(ADMIN_CHAT_ID)

    return True


def access_filter():
    if ALLOWED_USER_IDS:
        return filters.User(user_id=ALLOWED_USER_IDS)

    if ADMIN_CHAT_ID:
        return filters.Chat(chat_id=int(ADMIN_CHAT_ID))

    print(
        "WARNING: ALLOWED_USER_IDS / ADMIN_CHAT_ID не задані - "
        "бот доступний усім"
    )

    return filters.ALL


def main():
    if not TOKEN:
        raise SystemExit("Не задано змінну середовища BOT_TOKEN")

    init_db()

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )

    allowed = access_filter()

    text_filter = allowed & filters.TEXT & ~filters.COMMAND

    def on_text(callback):
        return [MessageHandler(text_filter, callback)]

    conv_handler = ConversationHandler(
        # Кнопки меню теж є точкою входу: після перезапуску бота
        # не потрібно заново натискати /start
        entry_points=[
            CommandHandler("start", start, filters=allowed),
            MessageHandler(text_filter, handle_menu),
        ],

        states={
            CHOOSING: on_text(handle_menu),

            INCOME_SCOPE: on_text(income_scope),
            INCOME_CATEGORY: on_text(income_category),
            INCOME_AMOUNT: on_text(income_amount),
            INCOME_CONFIRM: on_text(income_confirm),

            EXPENSE_CATEGORY: on_text(expense_category),
            EXPENSE_AMOUNT: on_text(expense_amount),
            EXPENSE_CONFIRM: on_text(expense_confirm),

            COMMENT: on_text(comment_entered),
        },

        fallbacks=[
            CommandHandler("start", start, filters=allowed),
            CommandHandler("cancel", cancel, filters=allowed),
        ],
    )

    app.add_handler(conv_handler)

    app.add_handler(
        CallbackQueryHandler(month_navigation, pattern=r"^month:")
    )

    app.add_handler(
        CommandHandler("exportdb", export_db, filters=allowed)
    )

    app.add_handler(
        CommandHandler("exportexcel", export_excel, filters=allowed)
    )

    # backup кожні 14 днів
    app.job_queue.run_repeating(
        auto_backup,
        interval=60 * 60 * 24 * 14,
        first=60,
    )

    print("Bot started")

    app.run_polling()


if __name__ == "__main__":
    main()
