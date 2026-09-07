import os
import sqlite3
import time
from telegram.error import BadRequest

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest


TOKEN = os.environ["BOT_TOKEN"]
DB_PATH = "badminton.db"
pending_removals = {}
REMOVE_CONFIRM_SECONDS = 5

ADMIN_IDS = {
    230080320,   # you
    307215246,   # admin 1
}

GAME_CHAT_ID = -1003789443207

CREATE_GAME_STEPS = [
    "max_players",
    "date",
    "time",
    "location",
    "courts",
    "price",
    "level",
    "shuttle",
]

def is_bot_admin(user_id):
    return user_id in ADMIN_IDS
# =========================================================
# DATABASE
# =========================================================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def setup_database():
    conn = get_db()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            location TEXT NOT NULL,
            courts TEXT NOT NULL,
            price TEXT NOT NULL,
            level TEXT NOT NULL,
            shuttle TEXT NOT NULL,
            max_players INTEGER NOT NULL,
            chat_id INTEGER,
            message_id INTEGER,
            finished INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER NOT NULL,
            owner_id INTEGER NOT NULL,
            owner_name TEXT NOT NULL,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            guest_number INTEGER,
            status TEXT NOT NULL,
            position INTEGER NOT NULL,

            FOREIGN KEY (game_id)
                REFERENCES games(id)
                ON DELETE CASCADE
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS charges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER NOT NULL,
            owner_id INTEGER NOT NULL,
            owner_name TEXT NOT NULL,
            entry_name TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'unpaid',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            paid_at TEXT,

            FOREIGN KEY (game_id)
                REFERENCES games(id)
                ON DELETE CASCADE
        )
        """
    )
    
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS credit_accounts (
            owner_id INTEGER PRIMARY KEY,
            owner_name TEXT NOT NULL,
            balance_cents INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS credit_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            amount_cents INTEGER NOT NULL,
            transaction_type TEXT NOT NULL,
            game_id INTEGER,
            description TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS topup_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            owner_name TEXT NOT NULL,
            amount_cents INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT NOT NULL,
            username TEXT,
            first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
)

    conn.commit()
    conn.close()

def register_bot_user(user_id, full_name, username=None):
    conn = get_db()

    conn.execute(
        """
        INSERT INTO bot_users (
            user_id,
            full_name,
            username
        )
        VALUES (?, ?, ?)

        ON CONFLICT(user_id)
        DO UPDATE SET
            full_name = excluded.full_name,
            username = excluded.username,
            last_seen_at = CURRENT_TIMESTAMP
        """,
        (
            user_id,
            full_name,
            username,
        ),
    )

    conn.commit()
    conn.close()
    
def import_existing_players():
    conn = get_db()

    users = conn.execute(
        """
        SELECT DISTINCT
            owner_id,
            owner_name
        FROM entries
        """
    ).fetchall()

    for user in users:
        conn.execute(
            """
            INSERT OR IGNORE INTO bot_users (
                user_id,
                full_name
            )
            VALUES (?, ?)
            """,
            (
                user["owner_id"],
                user["owner_name"],
            ),
        )

    conn.commit()
    conn.close()
    
def get_game(game_id):
    conn = get_db()

    game = conn.execute(
        """
        SELECT *
        FROM games
        WHERE id = ?
        """,
        (game_id,),
    ).fetchone()

    conn.close()

    return game


def get_entries(game_id, status):
    conn = get_db()

    rows = conn.execute(
        """
        SELECT *
        FROM entries
        WHERE game_id = ?
          AND status = ?
        ORDER BY position ASC, id ASC
        """,
        (game_id, status),
    ).fetchall()

    conn.close()

    return rows


def get_players(game_id):
    return get_entries(game_id, "player")


def get_waitlist(game_id):
    return get_entries(game_id, "waitlist")


def get_next_position(game_id, status):
    conn = get_db()

    row = conn.execute(
        """
        SELECT COALESCE(MAX(position), 0) AS max_position
        FROM entries
        WHERE game_id = ?
          AND status = ?
        """,
        (game_id, status),
    ).fetchone()

    conn.close()

    return row["max_position"] + 1


def compact_positions(game_id, status):
    conn = get_db()

    rows = conn.execute(
        """
        SELECT id
        FROM entries
        WHERE game_id = ?
          AND status = ?
        ORDER BY position ASC, id ASC
        """,
        (game_id, status),
    ).fetchall()

    for position, row in enumerate(rows, start=1):
        conn.execute(
            """
            UPDATE entries
            SET position = ?
            WHERE id = ?
            """,
            (position, row["id"]),
        )

    conn.commit()
    conn.close()

LOW_CREDIT_THRESHOLD_CENTS = 2000  # $20


def get_credit_balance(owner_id):
    conn = get_db()

    row = conn.execute(
        """
        SELECT balance_cents
        FROM credit_accounts
        WHERE owner_id = ?
        """,
        (owner_id,),
    ).fetchone()

    conn.close()

    if row is None:
        return 0

    return row["balance_cents"]


def add_credit(
    owner_id,
    owner_name,
    amount_cents,
    description="Top up",
):
    conn = get_db()

    conn.execute(
        """
        INSERT INTO credit_accounts (
            owner_id,
            owner_name,
            balance_cents
        )
        VALUES (?, ?, ?)

        ON CONFLICT(owner_id)
        DO UPDATE SET
            owner_name = excluded.owner_name,
            balance_cents =
                credit_accounts.balance_cents
                + excluded.balance_cents
        """,
        (
            owner_id,
            owner_name,
            amount_cents,
        ),
    )

    conn.execute(
        """
        INSERT INTO credit_transactions (
            owner_id,
            amount_cents,
            transaction_type,
            description
        )
        VALUES (?, ?, 'topup', ?)
        """,
        (
            owner_id,
            amount_cents,
            description,
        ),
    )

    conn.commit()
    conn.close()

def apply_topup_to_debt(
    owner_id,
    owner_name,
    amount_cents,
):
    conn = get_db()

    remaining_cents = amount_cents
    debt_paid_cents = 0

    charges = conn.execute(
        """
        SELECT
            id,
            game_id,
            amount
        FROM charges
        WHERE owner_id = ?
          AND status = 'unpaid'
        ORDER BY created_at ASC, id ASC
        """,
        (owner_id,),
    ).fetchall()

    for charge in charges:
        if remaining_cents <= 0:
            break

        charge_cents = round(
            charge["amount"] * 100
        )

        # Enough top-up to completely clear this debt
        if remaining_cents >= charge_cents:
            conn.execute(
                """
                UPDATE charges
                SET status = 'paid',
                    paid_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (charge["id"],),
            )

            remaining_cents -= charge_cents
            debt_paid_cents += charge_cents

        else:
            # Only partially pay this charge
            amount_left_cents = (
                charge_cents - remaining_cents
            )

            conn.execute(
                """
                UPDATE charges
                SET amount = ?
                WHERE id = ?
                """,
                (
                    amount_left_cents / 100,
                    charge["id"],
                ),
            )

            debt_paid_cents += remaining_cents
            remaining_cents = 0

    # Whatever is left becomes usable credit
    if remaining_cents > 0:
        conn.execute(
            """
            INSERT INTO credit_accounts (
                owner_id,
                owner_name,
                balance_cents
            )
            VALUES (?, ?, ?)

            ON CONFLICT(owner_id)
            DO UPDATE SET
                owner_name = excluded.owner_name,
                balance_cents =
                    credit_accounts.balance_cents
                    + excluded.balance_cents
            """,
            (
                owner_id,
                owner_name,
                remaining_cents,
            ),
        )

    # Record the original top-up
    conn.execute(
        """
        INSERT INTO credit_transactions (
            owner_id,
            amount_cents,
            transaction_type,
            description
        )
        VALUES (?, ?, 'topup', ?)
        """,
        (
            owner_id,
            amount_cents,
            (
                f"Top up: "
                f"${debt_paid_cents / 100:.2f} "
                f"applied to outstanding balance, "
                f"${remaining_cents / 100:.2f} "
                f"added to credit"
            ),
        ),
    )

    conn.commit()
    conn.close()

    return debt_paid_cents, remaining_cents
    

def use_credit(
    owner_id,
    amount_cents,
    game_id,
):
    balance = get_credit_balance(owner_id)

    used = min(
        balance,
        amount_cents,
    )

    if used <= 0:
        return 0

    conn = get_db()

    conn.execute(
        """
        UPDATE credit_accounts
        SET balance_cents =
            balance_cents - ?
        WHERE owner_id = ?
        """,
        (
            used,
            owner_id,
        ),
    )

    conn.execute(
        """
        INSERT INTO credit_transactions (
            owner_id,
            amount_cents,
            transaction_type,
            game_id,
            description
        )
        VALUES (?, ?, 'game', ?, ?)
        """,
        (
            owner_id,
            -used,
            game_id,
            f"Game #{game_id}",
        ),
    )

    conn.commit()
    conn.close()

    return used
    
# =========================================================
# GAME DISPLAY
# =========================================================

def make_keyboard(game_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "➕ Add me",
                    callback_data=f"add:{game_id}",
                ),
                InlineKeyboardButton(
                    "👥 Add +1",
                    callback_data=f"guest:{game_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "➖ Remove me",
                    callback_data=f"remove_me:{game_id}",
                ),
                InlineKeyboardButton(
                    "👥 Remove +1",
                    callback_data=f"remove_guest:{game_id}",
                ),
            ],
        ]
    )
    
    
def make_main_menu(user_id):
    buttons = [
        [
            InlineKeyboardButton(
                "💰 View my balance",
                callback_data="menu_balance",
            )
        ],
        [
            InlineKeyboardButton(
                "🏸 Upcoming games",
                callback_data="menu_games",
            )
        ],
        [
            InlineKeyboardButton(
                "💳 My credits",
                callback_data="menu_credit",
            )
        ],
    ]

    if is_bot_admin(user_id):
        buttons.extend(
            [
                [
                    InlineKeyboardButton(
                        "🧾 View all debts",
                        callback_data="menu_debts",
                    ),
                    InlineKeyboardButton(
                        "💳 Manage credits",
                        callback_data="admin_manage_credits",
                    ),
                ],
                [
                    InlineKeyboardButton(
		        "➕ Create game",
		        callback_data="admin_create_game",
		    ),
                    InlineKeyboardButton(
                        "✅ Finish game",
                        callback_data="admin_finish_game",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "✏️ Edit game",
                        callback_data="admin_edit_game",
                    ),
                    InlineKeyboardButton(
                        "❌ Cancel game",
                        callback_data="admin_cancel_game",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "♻️ Repost game",
                         callback_data="admin_repost_game",
                    )
                ],
            ]
        )

    return InlineKeyboardMarkup(buttons)

def make_upcoming_games_keyboard(games):
    buttons = []

    for game in games:
        buttons.append(
            [
                InlineKeyboardButton(
                    (
                        f"🏸 {game['date']} • "
                        f"{game['time']} • "
                        f"${game['price']}"
                    ),
                    callback_data=f"viewgame:{game['id']}",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data="menu_home",
            )
        ]
    )

    return InlineKeyboardMarkup(buttons)


def get_game_message_url(game):
    chat_id = str(game["chat_id"])
    message_id = game["message_id"]

    # Telegram private supergroup message links use:
    # https://t.me/c/<chat id without -100>/<message id>
    if chat_id.startswith("-100") and message_id is not None:
        internal_chat_id = chat_id[4:]
        return f"https://t.me/c/{internal_chat_id}/{message_id}"

    return None


def make_finish_game_keyboard(games):
    buttons = []

    for game in games:
        label = (
            f"🏸 {game['date']} • "
            f"{game['time']} • "
            f"${game['price']}"
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=(
                        f"finish_confirm:{game['id']}"
                    ),
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "❌ Cancel",
                callback_data="finish_cancel",
            )
        ]
    )

    return InlineKeyboardMarkup(buttons)

def make_game_text(game, players, waitlist):
    if players:
        player_text = "\n".join(
            f"{i}. {player['name']}"
            for i, player in enumerate(
                players,
                start=1,
            )
        )
    else:
        player_text = "No players yet"

    if waitlist:
        waitlist_text = "\n".join(
            f"{i}. {player['name']}"
            for i, player in enumerate(
                waitlist,
                start=1,
            )
        )
    else:
        waitlist_text = "No waitlist"

    return (
        f"📅 {game['date']}\n"
        f"⏰ {game['time']}\n"
        f"Location: {game['location']}\n"
        f"{game['courts']}\n"
        f"${game['price']}/pax\n\n"
        f"Level: {game['level']}\n"
        f"{game['shuttle']}\n\n"
        f"Players:\n"
        f"{player_text}\n\n"
        f"{len(players)} / {game['max_players']} players\n\n"
        f"Waitlist:\n"
        f"{waitlist_text}"
    )

def make_debts_keyboard(people):
    buttons = []

    for owner_id, person in people.items():
        total = sum(
            charge["amount"]
            for charge in person["charges"]
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    f"✅ Pay all — {person['name']} (${total:.2f})",
                    callback_data=f"payall:{owner_id}",
                )
            ]
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    f"📅 Pay specific game — {person['name']}",
                    callback_data=f"paychoose:{owner_id}",
                )
            ]
        )

    # IMPORTANT: outside the for-loop
    buttons.append(
        [
            InlineKeyboardButton(
                "🔔 Remind all unpaid players",
                callback_data="remind_all",
            )
        ]
    )

    return InlineKeyboardMarkup(buttons)
    
def make_specific_payment_keyboard(owner_id, charges):
    by_game = {}

    for charge in charges:
        game_id = charge["game_id"]

        if game_id not in by_game:
            by_game[game_id] = {
                "date": charge["date"],
                "total": 0,
            }

        by_game[game_id]["total"] += charge["amount"]

    buttons = []

    for game_id, item in by_game.items():
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{item['date']} — ${item['total']:.2f}",
                    callback_data=(
                        f"paygame:{owner_id}:{game_id}"
                    ),
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "❌ Cancel",
                callback_data="pay_cancel",
            )
        ]
    )

    return InlineKeyboardMarkup(buttons)

def make_credit_players_keyboard(accounts):
    buttons = []

    for account in accounts:
        buttons.append(
            [
                InlineKeyboardButton(
                    (
                        f"{account['owner_name']} — "
                        f"${account['balance_cents'] / 100:.2f}"
                    ),
                    callback_data=f"creditplayer:{account['owner_id']}",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data="menu_home",
            )
        ]
    )

    return InlineKeyboardMarkup(buttons)


def make_credit_deduct_keyboard(owner_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "-$11",
                    callback_data=f"creditdeduct:{owner_id}:1100",
                ),
                InlineKeyboardButton(
                    "-$16",
                    callback_data=f"creditdeduct:{owner_id}:1600",
                ),
            ],
            [
                InlineKeyboardButton(
                    "-$17",
                    callback_data=f"creditdeduct:{owner_id}:1700",
                ),
                InlineKeyboardButton(
                    "✏️ Custom",
                    callback_data=f"creditdeductcustom:{owner_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data="admin_manage_credits",
                )
            ],
        ]
    )

async def admin_manage_credits_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised.",
            show_alert=True,
        )
        return

    conn = get_db()

    accounts = conn.execute(
        """
        SELECT
            owner_id,
            owner_name,
            balance_cents
        FROM credit_accounts
        WHERE balance_cents > 0
        ORDER BY balance_cents DESC, owner_name ASC
        """
    ).fetchall()

    conn.close()

    await query.answer()

    if not accounts:
        await query.edit_message_text(
            "💳 No players currently have credits.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Back",
                            callback_data="menu_home",
                        )
                    ]
                ]
            ),
        )
        return

    total_cents = sum(
        account["balance_cents"]
        for account in accounts
    )

    await query.edit_message_text(
        (
            "💳 Player Credits\n\n"
            f"Players with credit: {len(accounts)}\n"
            f"Total outstanding credit: "
            f"${total_cents / 100:.2f}\n\n"
            "Choose a player:"
        ),
        reply_markup=make_credit_players_keyboard(
            accounts
        ),
    )

async def credit_player_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    context.user_data.pop(
        "custom_credit_deduction",
        None,
    )

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised.",
            show_alert=True,
        )
        return

    _, owner_id_text = query.data.split(":")
    owner_id = int(owner_id_text)

    conn = get_db()

    account = conn.execute(
        """
        SELECT
            owner_id,
            owner_name,
            balance_cents
        FROM credit_accounts
        WHERE owner_id = ?
        """,
        (owner_id,),
    ).fetchone()

    conn.close()

    if account is None:
        await query.answer(
            "Credit account not found.",
            show_alert=True,
        )
        return

    await query.answer()

    await query.edit_message_text(
        (
            "💳 Manage Credit\n\n"
            f"Player: {account['owner_name']}\n"
            f"Current balance: "
            f"${account['balance_cents'] / 100:.2f}\n\n"
            "How much would you like to deduct?"
        ),
        reply_markup=make_credit_deduct_keyboard(
            owner_id
        ),
    )
    
async def credit_deduct_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised.",
            show_alert=True,
        )
        return

    _, owner_id_text, amount_text = (
        query.data.split(":")
    )

    owner_id = int(owner_id_text)
    amount_cents = int(amount_text)

    conn = get_db()

    account = conn.execute(
        """
        SELECT
            owner_name,
            balance_cents
        FROM credit_accounts
        WHERE owner_id = ?
        """,
        (owner_id,),
    ).fetchone()

    if account is None:
        conn.close()

        await query.answer(
            "Credit account not found.",
            show_alert=True,
        )
        return

    if amount_cents > account["balance_cents"]:
        conn.close()

        await query.answer(
            "❌ Cannot deduct more than the current balance.",
            show_alert=True,
        )
        return

    conn.execute(
        """
        UPDATE credit_accounts
        SET balance_cents =
            balance_cents - ?
        WHERE owner_id = ?
        """,
        (
            amount_cents,
            owner_id,
        ),
    )

    conn.execute(
        """
        INSERT INTO credit_transactions (
            owner_id,
            amount_cents,
            transaction_type,
            description
        )
        VALUES (?, ?, 'manual_deduction', ?)
        """,
        (
            owner_id,
            -amount_cents,
            (
                f"Manual credit deduction"
            ),
        ),
    )

    conn.commit()
    conn.close()

    new_balance = get_credit_balance(
        owner_id
    )

    await query.answer(
        "✅ Credit deducted."
    )

    await query.edit_message_text(
        (
            "✅ Credit deducted\n\n"
            f"Player: {account['owner_name']}\n"
            f"Deducted: ${amount_cents / 100:.2f}\n"
            f"New balance: ${new_balance / 100:.2f}"
        ),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "💳 Back to credits",
                        callback_data="admin_manage_credits",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🏠 Main menu",
                        callback_data="menu_home",
                    )
                ],
            ]
        ),
    )
    
async def credit_deduct_custom_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised.",
            show_alert=True,
        )
        return

    _, owner_id_text = query.data.split(":")
    owner_id = int(owner_id_text)

    # Clear other text-input workflows
    context.user_data.pop("custom_topup", None)

    context.user_data.pop("creating_game", None)
    context.user_data.pop("create_game_step", None)
    context.user_data.pop("create_game_data", None)

    context.user_data.pop("editing_game", None)
    context.user_data.pop("edit_game_id", None)
    context.user_data.pop("edit_field", None)

    context.user_data["custom_credit_deduction"] = owner_id


    await query.answer()

    await query.edit_message_text(
        (
            "✏️ Custom Credit Deduction\n\n"
            "Enter the amount to deduct.\n\n"
        ),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data=f"creditplayer:{owner_id}",
                    )
                ]
            ]
        ),
    )

async def remind_all_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    # Security check
    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised to send payment reminders.",
            show_alert=True,
        )
        return

    conn = get_db()

    charges = conn.execute(
        """
        SELECT
            charges.*,
            games.date
        FROM charges
        JOIN games
            ON games.id = charges.game_id
        WHERE charges.status = 'unpaid'
        ORDER BY
            charges.owner_name,
            games.id,
            charges.id
        """
    ).fetchall()

    conn.close()

    if not charges:
        await query.answer(
            "✅ Everyone has already paid!",
            show_alert=True,
        )
        return

    # Group charges by person
    people = {}

    for charge in charges:
        owner_id = charge["owner_id"]

        if owner_id not in people:
            people[owner_id] = {
                "name": charge["owner_name"],
                "charges": [],
            }

        people[owner_id]["charges"].append(
            charge
        )

    sent = 0
    failed = 0

    for owner_id, person in people.items():

        lines = [
            f"Hi {person['name']}!",
            "",
            "You currently have the following "
            "outstanding payments:",
            "",
        ]

        total = 0

        for charge in person["charges"]:
            amount = charge["amount"]
            total += amount

            if (
                charge["entry_name"]
                == charge["owner_name"]
            ):
                description = charge["date"]
            else:
                description = (
                    f"{charge['date']} "
                    f"({charge['entry_name']})"
                )

            lines.append(
                f"• {description} — ${amount:.2f}"
            )

        lines.extend(
            [
                "",
                f"💰 Total outstanding: ${total:.2f}",
                "",
                "Please make payment when convenient. "
                "Thank you! 🙏",
            ]
        )

        try:
            await context.bot.send_message(
                chat_id=owner_id,
                text="\n".join(lines),
            )

            sent += 1

        except Exception as e:
            print(
                f"Could not remind "
                f"{person['name']} ({owner_id}): {e}"
            )

            failed += 1

    await query.answer(
        f"🔔 Sent: {sent} | Unable to DM: {failed}",
        show_alert=True,
    )
 
def make_cancel_game_keyboard(games):
    buttons = []

    for game in games:
        buttons.append(
            [
                InlineKeyboardButton(
                    f"❌ {game['date']} • {game['time']}",
                    callback_data=f"cancelgame:{game['id']}",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data="menu_home",
            )
        ]
    )

    return InlineKeyboardMarkup(buttons)
       
def make_create_game_confirm_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Create game",
                    callback_data="create_game_confirm",
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="create_game_cancel",
                )
            ],
        ]
    )
    
# =========================================================
# WAITLIST
# =========================================================

def promote_waitlist(game_id):
    game = get_game(game_id)

    if game is None:
        return []

    promoted = []

    while True:
        players = get_players(game_id)

        if len(players) >= game["max_players"]:
            break

        waitlist = get_waitlist(game_id)

        if not waitlist:
            break

        next_entry = waitlist[0]

        new_position = get_next_position(
            game_id,
            "player",
        )

        conn = get_db()

        conn.execute(
            """
            UPDATE entries
            SET status = 'player',
                position = ?
            WHERE id = ?
            """,
            (
                new_position,
                next_entry["id"],
            ),
        )

        conn.commit()
        conn.close()

        promoted.append(dict(next_entry))

        compact_positions(
            game_id,
            "waitlist",
        )

    return promoted


# =========================================================
# GUEST NUMBERING
# =========================================================

def get_next_guest_number(game_id, owner_id):
    conn = get_db()

    row = conn.execute(
        """
        SELECT COALESCE(MAX(guest_number), 0) AS max_guest
        FROM entries
        WHERE game_id = ?
          AND owner_id = ?
          AND type = 'guest'
        """,
        (
            game_id,
            owner_id,
        ),
    ).fetchone()

    conn.close()

    return row["max_guest"] + 1


def renumber_guests(
    game_id,
    owner_id,
    owner_name,
):
    conn = get_db()

    rows = conn.execute(
        """
        SELECT id
        FROM entries
        WHERE game_id = ?
          AND owner_id = ?
          AND type = 'guest'
        ORDER BY
            CASE
                WHEN status = 'player' THEN 0
                ELSE 1
            END,
            position ASC,
            id ASC
        """,
        (
            game_id,
            owner_id,
        ),
    ).fetchall()

    for number, row in enumerate(
        rows,
        start=1,
    ):
        conn.execute(
            """
            UPDATE entries
            SET guest_number = ?,
                name = ?
            WHERE id = ?
            """,
            (
                number,
                f"{owner_name} +{number}",
                row["id"],
            ),
        )

    conn.commit()
    conn.close()

def make_edit_game_keyboard(games):
    buttons = []

    for game in games:
        buttons.append(
            [
                InlineKeyboardButton(
                    f"✏️ {game['date']} • {game['time']}",
                    callback_data=f"editgame:{game['id']}",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data="menu_home",
            )
        ]
    )

    return InlineKeyboardMarkup(buttons)

def make_repost_game_keyboard(games):
    buttons = []

    for game in games:
        buttons.append(
            [
                InlineKeyboardButton(
                    (
                        f"♻️ {game['date']} • "
                        f"{game['time']} • "
                        f"${game['price']}"
                    ),
                    callback_data=f"repostgame:{game['id']}",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data="menu_home",
            )
        ]
    )

    return InlineKeyboardMarkup(buttons)

def make_edit_field_keyboard(game_id):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📅 Date",
                    callback_data=f"editfield:{game_id}:date",
                ),
                InlineKeyboardButton(
                    "⏰ Time",
                    callback_data=f"editfield:{game_id}:time",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📍 Location",
                    callback_data=f"editfield:{game_id}:location",
                ),
                InlineKeyboardButton(
                    "🏸 Courts",
                    callback_data=f"editfield:{game_id}:courts",
                ),
            ],
            [
                InlineKeyboardButton(
                    "💰 Price",
                    callback_data=f"editfield:{game_id}:price",
                ),
                InlineKeyboardButton(
                    "👥 Max players",
                    callback_data=f"editfield:{game_id}:max_players",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🎯 Level",
                    callback_data=f"editfield:{game_id}:level",
                ),
                InlineKeyboardButton(
                    "🪶 Shuttle",
                    callback_data=f"editfield:{game_id}:shuttle",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data="admin_edit_game",
                )
            ],
        ]
    )
    
def make_topup_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "$20",
                    callback_data="topup:2000",
                ),
                InlineKeyboardButton(
                    "$50",
                    callback_data="topup:5000",
                ),
            ],
            [
                InlineKeyboardButton(
                    "$100",
                    callback_data="topup:10000",
                ),
                InlineKeyboardButton(
                    "✏️ Custom amount",
                    callback_data="topup_custom",
                ),
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="topup_cancel",
                )
            ],
        ]
    )
    
def make_debt_view_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📅 By game",
                    callback_data="debts_by_game",
                )
            ],
            [
                InlineKeyboardButton(
                    "👤 By player",
                    callback_data="debts_by_player",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data="menu_home",
                )
            ],
        ]
    )
    
    
def make_debt_games_keyboard(games):
    buttons = []

    for game in games:
        buttons.append(
            [
                InlineKeyboardButton(
                    (
                        f"📅 {game['date']} • "
                        f"{game['time']} • "
                        f"${game['total']:.2f}"
                    ),
                    callback_data=(
                        f"debtgame:{game['id']}"
                    ),
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data="menu_debts",
            )
        ]
    )

    return InlineKeyboardMarkup(buttons)
    
def make_game_debt_keyboard(
    game_id,
    charges,
    selected,
):
    buttons = []

    for charge in charges:
        charge_id = charge["id"]

        if charge_id in selected:
            icon = "☑️"
        else:
            icon = "⬜"

        if (
            charge["entry_name"]
            == charge["owner_name"]
        ):
            name = charge["owner_name"]
        else:
            name = charge["entry_name"]

        buttons.append(
            [
                InlineKeyboardButton(
                    (
                        f"{icon} {name} — "
                        f"${charge['amount']:.2f}"
                    ),
                    callback_data=(
                        f"debttoggle:"
                        f"{game_id}:"
                        f"{charge_id}"
                    ),
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "☑️ Select all",
                callback_data=(
                    f"debtselectall:{game_id}"
                ),
            )
        ]
    )

    if selected:
        selected_total = sum(
            charge["amount"]
            for charge in charges
            if charge["id"] in selected
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    (
                        "✅ Log selected as paid "
                        f"(${selected_total:.2f})"
                    ),
                    callback_data=(
                        f"debtpayselected:{game_id}"
                    ),
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ Back to games",
                callback_data="debts_by_game",
            )
        ]
    )

    return InlineKeyboardMarkup(buttons)
    
    
# =========================================================
# TELEGRAM COMMANDS
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    print(
        "CHAT ID:",
        update.effective_chat.id,
        flush=True,
    )

    if update.effective_chat.type == "private":

        user = update.effective_user

        register_bot_user(
            user.id,
            user.full_name,
            user.username,
        )

        await update.message.reply_text(
            "🏸 Welcome to Baddy Buddies Bot\n\n"
            "What would you like to do?",
            reply_markup=make_main_menu(
                user.id
            ),
        )

async def create_game(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = update.message.text.partition(" ")[2]

    parts = [
        x.strip()
        for x in text.split("|")
    ]

    if len(parts) != 8:
        await update.message.reply_text(
            "Use:\n\n"
            "/game MAX | DATE | TIME | LOCATION | "
            "COURTS | PRICE | LEVEL | SHUTTLE\n\n"
            "Example:\n"
            "/game 8 | 26th Aug Wednesday | 9-11 PM | "
            "The Sports Arena (Jalan Kayu) | "
            "2 courts (C3,4) | 16 | HB-LI | RSL Ultimate"
        )
        return

    try:
        maximum = int(parts[0])
    except ValueError:
        await update.message.reply_text(
            "Maximum players must be a number."
        )
        return

    conn = get_db()

    cursor = conn.execute(
        """
        INSERT INTO games (
            date,
            time,
            location,
            courts,
            price,
            level,
            shuttle,
            max_players
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            parts[1],
            parts[2],
            parts[3],
            parts[4],
            parts[5],
            parts[6],
            parts[7],
            maximum,
        ),
    )

    game_id = cursor.lastrowid

    conn.commit()
    conn.close()

    game = get_game(game_id)

    sent_message = await update.message.reply_text(
        make_game_text(
            game,
            [],
            [],
        ),
        reply_markup=make_keyboard(game_id),
    )

    conn = get_db()

    conn.execute(
        """
        UPDATE games
        SET chat_id = ?,
            message_id = ?
        WHERE id = ?
        """,
        (
            sent_message.chat_id,
            sent_message.message_id,
            game_id,
        ),
    )

    conn.commit()
    conn.close()
    
async def create_game_cancel_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised.",
            show_alert=True,
        )
        return

    context.user_data.pop(
        "creating_game",
        None,
    )
    context.user_data.pop(
        "create_game_step",
        None,
    )
    context.user_data.pop(
        "create_game_data",
        None,
    )

    await query.answer()

    await query.edit_message_text(
        "❌ Game creation cancelled.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬅️ Back to menu",
                        callback_data="menu_home",
                    )
                ]
            ]
        ),
    )
    
async def admin_cancel_game_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised.",
            show_alert=True,
        )
        return

    conn = get_db()

    games = conn.execute(
        """
        SELECT *
        FROM games
        WHERE finished = 0
        ORDER BY id DESC
        """
    ).fetchall()

    conn.close()

    await query.answer()

    if not games:
        await query.edit_message_text(
            "There are no active games.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Back",
                            callback_data="menu_home",
                        )
                    ]
                ]
            ),
        )
        return

    await query.edit_message_text(
        "❌ Choose a game to cancel:",
        reply_markup=make_cancel_game_keyboard(games),
    )
    
async def cancel_game_select_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised.",
            show_alert=True,
        )
        return

    _, game_id_text = query.data.split(":")
    game_id = int(game_id_text)

    game = get_game(game_id)

    if game is None or game["finished"]:
        await query.answer(
            "This game is no longer active.",
            show_alert=True,
        )
        return

    await query.answer()

    await query.edit_message_text(
        (
            f"⚠️ Cancel this game?\n\n"
            f"📅 {game['date']}\n"
            f"⏰ {game['time']}\n"
            f"📍 {game['location']}"
        ),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Yes, cancel game",
                        callback_data=f"cancelconfirm:{game_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="admin_cancel_game",
                    )
                ],
            ]
        ),
    )
    
async def cancel_game_confirm_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised.",
            show_alert=True,
        )
        return

    _, game_id_text = query.data.split(":")
    game_id = int(game_id_text)

    game = get_game(game_id)

    if game is None or game["finished"]:
        await query.answer(
            "This game is no longer active.",
            show_alert=True,
        )
        return

    conn = get_db()

    conn.execute(
        """
        UPDATE games
        SET finished = 1
        WHERE id = ?
        """,
        (game_id,),
    )

    conn.commit()
    conn.close()

    try:
        await context.bot.edit_message_text(
            chat_id=game["chat_id"],
            message_id=game["message_id"],
            text=(
                f"❌ GAME CANCELLED\n\n"
                f"📅 {game['date']}\n"
                f"⏰ {game['time']}\n"
                f"📍 {game['location']}"
            ),
        )
    except Exception:
        pass

    await query.answer()

    await query.edit_message_text(
        "✅ Game cancelled.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬅️ Back to menu",
                        callback_data="menu_home",
                    )
                ]
            ]
        ),
    )
    
async def finish_game_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_bot_admin(update.effective_user.id):
        await update.message.reply_text(
            "❌ You are not authorised to use /finishgame."
        )
        return

    conn = get_db()
    
    games = conn.execute(
        """
        SELECT *
        FROM games
        WHERE finished = 0
        ORDER BY id DESC
        """
    ).fetchall()

    conn.close()

    if not games:
        await update.message.reply_text(
            "There are no unfinished games."
        )
        return

    await update.message.reply_text(
        "🏸 Choose a game to finish:",
        reply_markup=make_finish_game_keyboard(
            games
        ),
    )
    
async def show_balance(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    conn = get_db()

    charges = conn.execute(
        """
        SELECT
            charges.*,
            games.date
        FROM charges
        JOIN games
            ON games.id = charges.game_id
        WHERE charges.owner_id = ?
          AND charges.status != 'paid'
        ORDER BY games.id ASC, charges.id ASC
        """,
        (user.id,),
    ).fetchall()

    conn.close()

    if not charges:
        text = "✅ You have no outstanding payments!"
    else:
        lines = [
            f"💰 {user.full_name}",
            "",
            "Outstanding:",
            "",
        ]

        total = 0

        for charge in charges:
            amount = charge["amount"]
            total += amount

            if charge["entry_name"] == charge["owner_name"]:
                lines.append(
                    f"• {charge['date']} — ${amount:.2f}"
                )
            else:
                lines.append(
                    f"• {charge['date']} "
                    f"({charge['entry_name']}) — ${amount:.2f}"
                )

        lines.extend(
            [
                "",
                f"Total: ${total:.2f}",
            ]
        )

        text = "\n".join(lines)

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Back",
                            callback_data="menu_home",
                        )
                    ]
                ]
            ),
        )
    else:
        await update.message.reply_text(text)


async def balance_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await show_balance(update, context)


async def games_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    conn = get_db()

    games = conn.execute(
        """
        SELECT *
        FROM games
        WHERE finished = 0
        ORDER BY id ASC
        """
    ).fetchall()

    conn.close()

    if not games:
        await update.message.reply_text(
            "🏸 There are no upcoming games."
        )
        return

    await update.message.reply_text(
        "🏸 Upcoming Games\n\nChoose a game:",
        reply_markup=make_upcoming_games_keyboard(games),
    )


async def menu_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if query.data == "menu_home":
        await query.answer()
        await query.edit_message_text(
            "🏸 Baddy Buddies\n\n"
            "What would you like to do?",
            reply_markup=make_main_menu(
                query.from_user.id
            )
        )
        return

    if query.data == "menu_balance":
        await show_balance(update, context)
        return

    if query.data == "menu_games":
        conn = get_db()

        games = conn.execute(
            """
            SELECT *
            FROM games
            WHERE finished = 0
            ORDER BY id ASC
            """
        ).fetchall()

        conn.close()

        await query.answer()

        if not games:
            await query.edit_message_text(
                "🏸 There are no upcoming games.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "⬅️ Back",
                                callback_data="menu_home",
                            )
                        ]
                    ]
                ),
            )
            return

        await query.edit_message_text(
            "🏸 Upcoming Games\n\nChoose a game:",
            reply_markup=make_upcoming_games_keyboard(games),
        )
        
        
    if query.data == "menu_credit":
        balance = get_credit_balance(
            query.from_user.id
        )

        await query.answer()

        await query.edit_message_text(
            (
                "💳 Baddy Buddies Credit\n\n"
                f"Available credit: "
                f"${balance / 100:.2f}"
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "➕ Top up",
                            callback_data="credit_topup",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "📜 Credit history",
                            callback_data="credit_history",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ Back",
                            callback_data="menu_home",
                        )
                    ],
                ]
            ),
        )

        return
    
    if query.data == "menu_debts":
        if not is_bot_admin(query.from_user.id):
            await query.answer(
                "❌ You are not authorised to manage payments.",
                show_alert=True,
            )
            return

        # Clear any old multi-selection
        context.user_data.pop(
            "debt_selected_charges",
            None,
        )

        await query.answer()

        await query.edit_message_text(
            (
                "🧾 Manage Debts\n\n"
                "How would you like to view "
                "outstanding payments?"
            ),
            reply_markup=make_debt_view_keyboard(),
        )

        return

async def debts_by_player_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised.",
            show_alert=True,
        )
        return

    conn = get_db()

    charges = conn.execute(
        """
        SELECT
            charges.*,
            games.date
        FROM charges
        JOIN games
            ON games.id = charges.game_id
        WHERE charges.status = 'unpaid'
        ORDER BY
            charges.owner_name,
            games.id,
            charges.id
        """
    ).fetchall()

    conn.close()

    if not charges:
        await query.answer()

        await query.edit_message_text(
            "✅ Everyone has paid!",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Back",
                            callback_data="menu_debts",
                        )
                    ]
                ]
            ),
        )
        return

    people = {}

    for charge in charges:
        owner_id = charge["owner_id"]

        if owner_id not in people:
            people[owner_id] = {
                "name": charge["owner_name"],
                "charges": [],
            }

        people[owner_id]["charges"].append(
            charge
        )

    lines = [
        "👤 Outstanding by Player",
        "",
    ]

    for person in people.values():
        total = sum(
            charge["amount"]
            for charge in person["charges"]
        )

        lines.append(
            f"{person['name']} — ${total:.2f}"
        )

    buttons = list(
        make_debts_keyboard(
            people
        ).inline_keyboard
    )

    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data="menu_debts",
            )
        ]
    )

    await query.answer()

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            buttons
        ),
    )
    
async def debts_by_game_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised.",
            show_alert=True,
        )
        return

    conn = get_db()

    games = conn.execute(
        """
        SELECT
            games.id,
            games.date,
            games.time,
            games.location,
            SUM(charges.amount) AS total
        FROM charges
        JOIN games
            ON games.id = charges.game_id
        WHERE charges.status = 'unpaid'
        GROUP BY
            games.id,
            games.date,
            games.time,
            games.location
        ORDER BY games.id DESC
        """
    ).fetchall()

    conn.close()

    await query.answer()

    if not games:
        await query.edit_message_text(
            "✅ There are no outstanding payments.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Back",
                            callback_data="menu_debts",
                        )
                    ]
                ]
            ),
        )
        return

    await query.edit_message_text(
        "📅 Choose a game:",
        reply_markup=make_debt_games_keyboard(
            games
        ),
    )
    
async def debt_game_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised.",
            show_alert=True,
        )
        return

    _, game_id_text = query.data.split(":")
    game_id = int(game_id_text)

    game = get_game(game_id)

    if game is None:
        await query.answer(
            "Game not found.",
            show_alert=True,
        )
        return

    conn = get_db()

    charges = conn.execute(
        """
        SELECT *
        FROM charges
        WHERE game_id = ?
          AND status = 'unpaid'
        ORDER BY id ASC
        """,
        (game_id,),
    ).fetchall()

    conn.close()

    context.user_data[
        "debt_selected_charges"
    ] = set()

    await query.answer()

    if not charges:
        await query.edit_message_text(
            "✅ This game has no outstanding payments.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Back to games",
                            callback_data="debts_by_game",
                        )
                    ]
                ]
            ),
        )
        return

    await query.edit_message_text(
        (
            "📅 Log Payments\n\n"
            f"{game['date']}\n"
            f"⏰ {game['time']}\n"
            f"📍 {game['location']}\n\n"
            "Select everyone who has paid:"
        ),
        reply_markup=make_game_debt_keyboard(
            game_id,
            charges,
            set(),
        ),
    )
    
async def debt_toggle_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised.",
            show_alert=True,
        )
        return

    _, game_id_text, charge_id_text = (
        query.data.split(":")
    )

    game_id = int(game_id_text)
    charge_id = int(charge_id_text)

    selected = context.user_data.setdefault(
        "debt_selected_charges",
        set(),
    )

    if charge_id in selected:
        selected.remove(charge_id)
    else:
        selected.add(charge_id)

    conn = get_db()

    charges = conn.execute(
        """
        SELECT *
        FROM charges
        WHERE game_id = ?
          AND status = 'unpaid'
        ORDER BY id ASC
        """,
        (game_id,),
    ).fetchall()

    conn.close()

    # Remove anything that is no longer unpaid
    valid_ids = {
        charge["id"]
        for charge in charges
    }

    selected.intersection_update(
        valid_ids
    )

    await query.answer()

    await query.edit_message_reply_markup(
        reply_markup=make_game_debt_keyboard(
            game_id,
            charges,
            selected,
        )
    )

async def debt_select_all_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised.",
            show_alert=True,
        )
        return

    _, game_id_text = query.data.split(":")
    game_id = int(game_id_text)

    conn = get_db()

    charges = conn.execute(
        """
        SELECT *
        FROM charges
        WHERE game_id = ?
          AND status = 'unpaid'
        ORDER BY id ASC
        """,
        (game_id,),
    ).fetchall()

    conn.close()

    selected = {
        charge["id"]
        for charge in charges
    }

    context.user_data[
        "debt_selected_charges"
    ] = selected

    await query.answer(
        "All selected."
    )

    await query.edit_message_reply_markup(
        reply_markup=make_game_debt_keyboard(
            game_id,
            charges,
            selected,
        )
    )
    
async def debt_pay_selected_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised.",
            show_alert=True,
        )
        return

    _, game_id_text = query.data.split(":")
    game_id = int(game_id_text)

    selected = context.user_data.get(
        "debt_selected_charges",
        set(),
    )

    if not selected:
        await query.answer(
            "Select at least one payment.",
            show_alert=True,
        )
        return

    conn = get_db()

    placeholders = ",".join(
        "?"
        for _ in selected
    )

    charges = conn.execute(
        f"""
        SELECT *
        FROM charges
        WHERE game_id = ?
          AND status = 'unpaid'
          AND id IN ({placeholders})
        """,
        (
            game_id,
            *selected,
        ),
    ).fetchall()

    if not charges:
        conn.close()

        await query.answer(
            "These payments have already been recorded.",
            show_alert=True,
        )
        return

    charge_ids = [
        charge["id"]
        for charge in charges
    ]

    placeholders = ",".join(
        "?"
        for _ in charge_ids
    )

    conn.execute(
        f"""
        UPDATE charges
        SET status = 'paid',
            paid_at = CURRENT_TIMESTAMP
        WHERE id IN ({placeholders})
          AND status = 'unpaid'
        """,
        charge_ids,
    )

    conn.commit()
    conn.close()

    total = sum(
        charge["amount"]
        for charge in charges
    )

    names = []

    for charge in charges:
        if (
            charge["entry_name"]
            == charge["owner_name"]
        ):
            names.append(
                charge["owner_name"]
            )
        else:
            names.append(
                charge["entry_name"]
            )

    context.user_data.pop(
        "debt_selected_charges",
        None,
    )

    game = get_game(game_id)

    await query.answer(
        "✅ Payments recorded."
    )

    await query.edit_message_text(
        (
            "✅ Payments recorded\n\n"
            f"📅 {game['date']}\n"
            f"⏰ {game['time']}\n\n"
            + "\n".join(
                f"• {name}"
                for name in names
            )
            + f"\n\n💰 Total: ${total:.2f}"
        ),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📅 Back to games",
                        callback_data="debts_by_game",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🏠 Main menu",
                        callback_data="menu_home",
                    )
                ],
            ]
        ),
    )
    
async def view_game_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    _, game_id_text = query.data.split(":")
    game_id = int(game_id_text)

    game = get_game(game_id)

    if game is None or game["finished"]:
        await query.answer(
            "This game is no longer available.",
            show_alert=True,
        )
        return

    players = get_players(game_id)
    waitlist = get_waitlist(game_id)
    url = get_game_message_url(game)

    buttons = [
        [
            InlineKeyboardButton(
                "➕ Add me",
                callback_data=f"add:{game_id}",
            ),
            InlineKeyboardButton(
                "👥 Add +1",
                callback_data=f"guest:{game_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "➖ Remove me",
                callback_data=f"remove_me:{game_id}",
            ),
            InlineKeyboardButton(
                "👥 Remove +1",
                callback_data=f"remove_guest:{game_id}",
            ),
        ],
]

    if url:
        buttons.append(
            [
                InlineKeyboardButton(
                    "📍 Open game message",
                    url=url,
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ Back to games",
                callback_data="menu_games",
            )
        ]
    )

    await query.answer()

    try:
        await query.edit_message_text(
            make_game_text(game, players, waitlist),
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise

async def admin_finish_game_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised.",
            show_alert=True,
        )
        return

    conn = get_db()

    games = conn.execute(
        """
        SELECT *
        FROM games
        WHERE finished = 0
        ORDER BY id DESC
        """
    ).fetchall()

    conn.close()

    await query.answer()

    if not games:
        await query.edit_message_text(
            "There are no unfinished games.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Back",
                            callback_data="menu_home",
                        )
                    ]
                ]
            ),
        )
        return

    await query.edit_message_text(
        "✅ Choose a game to finish:",
        reply_markup=make_finish_game_keyboard(games),
    )
    
async def finish_game_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised to finish games.",
            show_alert=True,
        )
        return

    if query.data == "finish_cancel":
        await query.answer()

        await query.edit_message_text(
            "❌ Finish game cancelled."
        )
        return

    _, game_id_text = query.data.split(":")
    game_id = int(game_id_text)

    game = get_game(game_id)

    if game is None:
        await query.answer(
            "Game not found.",
            show_alert=True,
        )
        return

    if game["finished"]:
        await query.answer(
            "This game has already been finished.",
            show_alert=True,
        )
        return

    players = get_players(game_id)

    if not players:
        await query.answer(
            "This game has no players.",
            show_alert=True,
        )
        return

    try:
        price = float(game["price"])
    except ValueError:
        await query.answer(
            "This game's price is invalid.",
            show_alert=True,
        )
        return

    price_cents = round(price * 100)

    for player in players:

        # Admin themselves play free.
        # Their +1s are still charged.
        if (
            is_bot_admin(player["owner_id"])
            and player["type"] == "self"
        ):
            continue

        owner_id = player["owner_id"]
    
        old_balance = get_credit_balance(
            owner_id
        )
    
        credit_used = use_credit(
            owner_id,
            price_cents,
            game_id,
        )    

        amount_left_cents = (
            price_cents - credit_used
        )

        # Anything credits didn't cover
        # becomes normal outstanding debt
        if amount_left_cents > 0:
        
            conn = get_db()
            
            conn.execute(
                """
                INSERT INTO charges (
                    game_id,
                    owner_id,
                    owner_name,
                    entry_name,    
                    amount,
                    status
                )
                VALUES (?, ?, ?, ?, ?, 'unpaid')
                """,
                (
                    game_id,
                    owner_id,
                    player["owner_name"],    
                    player["name"],
                    amount_left_cents / 100,
                ),
            )
            conn.commit()
            conn.close()

        new_balance = get_credit_balance(
            owner_id
        )

        # Notify only when they CROSS the
        # low-credit threshold
        if (
            old_balance > LOW_CREDIT_THRESHOLD_CENTS
            and new_balance <= LOW_CREDIT_THRESHOLD_CENTS
        ):
            try:
                await context.bot.send_message(
                    chat_id=owner_id,
                    text=(
                        "⚠️ Your Baddy Buddies credit "
                        "is running low.\n\n"
                        f"Current credit: "
                        f"${new_balance / 100:.2f}\n\n"
                        "You may want to top up "
                        "before your next game."
                    ),
                )
            except Exception:
                pass        

    conn = get_db()
    conn.execute(
        """
        UPDATE games
        SET finished = 1
        WHERE id = ?
        """,
        (game_id,),
    )

    conn.commit()
    conn.close()

    chargeable_players = [
        player
        for player in players
        if not (
            is_bot_admin(player["owner_id"])
            and player["type"] == "self"
        )   
    ]

    total = price * len(chargeable_players)

    await query.answer()

    await query.edit_message_text(
        f"✅ Game finished!\n\n"
        f"📅 {game['date']}\n"
        f"👥 {len(players)} players\n"
        f"💰 ${price:.2f}/pax\n"
        f"🧾 {len(chargeable_players)} charge(s) created\n"
	f"💵 ${total:.2f} total"
    )
    
async def debts_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_bot_admin(update.effective_user.id):
        await update.message.reply_text(
            "❌ You are not authorised to manage payments."
        )
        return

    context.user_data.pop(
        "debt_selected_charges",
        None,
    )

    await update.message.reply_text(
        (
            "🧾 Manage Debts\n\n"
            "How would you like to view "
            "outstanding payments?"
        ),
        reply_markup=make_debt_view_keyboard(),
    )
    
async def pay_all_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised to manage payments.",
            show_alert=True,
        )
        return

    _, owner_id_text = query.data.split(":")
    owner_id = int(owner_id_text)

    conn = get_db()

    person = conn.execute(
        """
        SELECT
            owner_name,
            COALESCE(SUM(amount), 0) AS total
        FROM charges
        WHERE owner_id = ?
          AND status = 'unpaid'
        """,
        (owner_id,),
    ).fetchone()

    if (
        person is None
        or person["total"] == 0
    ):
        conn.close()

        await query.answer(
            "No outstanding balance.",
            show_alert=True,
        )
        return

    name = person["owner_name"]
    total = person["total"]

    conn.execute(
        """
        UPDATE charges
        SET status = 'paid',
            paid_at = CURRENT_TIMESTAMP
        WHERE owner_id = ?
          AND status = 'unpaid'
        """,
        (owner_id,),
    )

    conn.commit()
    conn.close()

    await query.answer()

    await query.edit_message_text(
        f"✅ Payment recorded\n\n"
        f"{name}\n"
        f"Paid all outstanding: ${total:.2f}"
    )
    
async def pay_choose_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised to manage payments.",
            show_alert=True,
        )
        return

    _, owner_id_text = query.data.split(":")
    owner_id = int(owner_id_text)

    conn = get_db()

    charges = conn.execute(
        """
        SELECT
            charges.*,
            games.date
        FROM charges
        JOIN games
            ON games.id = charges.game_id
        WHERE charges.owner_id = ?
          AND charges.status = 'unpaid'
        ORDER BY games.id ASC
        """,
        (owner_id,),
    ).fetchall()

    conn.close()

    if not charges:
        await query.answer(
            "No outstanding games.",
            show_alert=True,
        )
        return

    name = charges[0]["owner_name"]

    await query.answer()

    await query.edit_message_text(
        f"📅 Choose which game {name} paid:",
        reply_markup=make_specific_payment_keyboard(
            owner_id,
            charges,
        ),
    )
    
async def pay_game_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised to manage payments.",
            show_alert=True,
        )
        return

    _, owner_id_text, game_id_text = query.data.split(":")

    owner_id = int(owner_id_text)
    game_id = int(game_id_text)

    conn = get_db()

    info = conn.execute(
        """
        SELECT
            charges.owner_name,
            games.date,
            COALESCE(SUM(charges.amount), 0) AS total
        FROM charges
        JOIN games
            ON games.id = charges.game_id
        WHERE charges.owner_id = ?
          AND charges.game_id = ?
          AND charges.status = 'unpaid'
        """,
        (
            owner_id,
            game_id,
        ),
    ).fetchone()

    if (
        info is None
        or info["total"] == 0
    ):
        conn.close()

        await query.answer(
            "This payment is already settled.",
            show_alert=True,
        )
        return

    conn.execute(
        """
        UPDATE charges
        SET status = 'paid',
            paid_at = CURRENT_TIMESTAMP
        WHERE owner_id = ?
          AND game_id = ?
          AND status = 'unpaid'
        """,
        (
            owner_id,
            game_id,
        ),
    )

    conn.commit()
    conn.close()

    await query.answer()

    await query.edit_message_text(
        f"✅ Payment recorded\n\n"
        f"{info['owner_name']}\n"
        f"{info['date']} — ${info['total']:.2f}"
    )
    
async def pay_cancel_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised to manage payments.",
            show_alert=True,
        )
        return

    await query.answer()

    await query.edit_message_text(
        "❌ Payment update cancelled."
    )
   
async def admin_create_game_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised.",
            show_alert=True,
        )
        return

    context.user_data["creating_game"] = True
    context.user_data["create_game_step"] = 0
    context.user_data["create_game_data"] = {}

    await query.answer()

    await query.edit_message_text(
        "➕ Create Game\n\n"
        "How many players maximum?\n\n"
        "Example: 8"
    )
    
async def create_game_message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if context.user_data.get(
        "custom_credit_deduction"
    ) is not None:
        if not is_bot_admin(
            update.effective_user.id
        ):
            return

        owner_id = context.user_data[
            "custom_credit_deduction"
        ]

        text = (
            update.message.text
            .replace("$", "")
            .strip()
        )

        try:
            amount = float(text)

            if amount <= 0:
                raise ValueError

            amount_cents = round(
                amount * 100
            )

        except ValueError:
            await update.message.reply_text(
                "❌ Please enter a valid amount."
            )
            return

        conn = get_db()

        account = conn.execute(
            """
            SELECT
                owner_name,
                balance_cents
            FROM credit_accounts
            WHERE owner_id = ?
            """,
            (owner_id,),
        ).fetchone()

        if account is None:
            conn.close()

            context.user_data.pop(
                "custom_credit_deduction",
                None,
            )

            await update.message.reply_text(
                "❌ Credit account not found."
            )
            return

        if amount_cents > account["balance_cents"]:
            conn.close()

            await update.message.reply_text(
                (
                    "❌ That is more than the "
                    "player's current credit.\n\n"
                   f"Current balance: "
                    f"${account['balance_cents'] / 100:.2f}"
                )
            )
            return
    
        conn.execute(
            """
            UPDATE credit_accounts
            SET balance_cents =
                balance_cents - ?
            WHERE owner_id = ?
            """,
            (
                amount_cents,
                owner_id,
            ),
        )

        conn.execute(
            """
            INSERT INTO credit_transactions (
                owner_id,
                amount_cents,
                transaction_type,
                description
            )
            VALUES (?, ?, 'manual_deduction', ?)
            """,
            (
                owner_id,
                -amount_cents,
                (
                    f"Manual credit deduction "
                ),
            ),
        )

        conn.commit()
        conn.close()

        context.user_data.pop(
            "custom_credit_deduction",
            None,
        )

        new_balance = get_credit_balance(
            owner_id
        )

        await update.message.reply_text(
            (
                "✅ Credit deducted\n\n"
                f"Player: {account['owner_name']}\n"
                f"Deducted: ${amount_cents / 100:.2f}\n"
                f"New balance: ${new_balance / 100:.2f}"
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                           "💳 Back to credits",
                           callback_data="admin_manage_credits",
                        )
                    ]
                ]
            ),
        )

        return
    
    
    # =====================================================
    # CUSTOM CREDIT TOP-UP
    # =====================================================

    if context.user_data.get("custom_topup"):
        text = update.message.text.strip()

        # Allow either:
        # 35
        # $35
        # 35.50
        text = text.replace("$", "").strip()

        try:
            amount = float(text)

            if amount <= 0:
                raise ValueError

            amount_cents = round(
                amount * 100
            )

        except ValueError:
            await update.message.reply_text(
                (
                    "❌ Please enter a valid amount.\n\n"
                    "Examples:\n"
                    "20\n"
                    "35.50\n"
                    "$50"
                )
            )
            return

        # Optional sensible limit
        if amount_cents > 100000:
            await update.message.reply_text(
                "❌ Maximum top-up is $1,000."
            )
            return

        context.user_data.pop(
            "custom_topup",
            None,
        )

        await create_topup_request(
            context,
            update.effective_user,
            amount_cents,
        )

        await update.message.reply_text(
            (
                "✅ Top-up request submitted!\n\n"
                f"Amount: "
                f"${amount_cents / 100:.2f}\n\n"
                "Your credits will be added "
                "after an admin confirms "
                "your payment."
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Back to menu",
                            callback_data="menu_home",
                        )
                    ]
                ]
            ),
        )

        return

    if context.user_data.get("editing_game"):
        if not is_bot_admin(update.effective_user.id):
            return

        game_id = context.user_data.get("edit_game_id")
        field = context.user_data.get("edit_field")
        new_value = update.message.text.strip()

        if not game_id or not field:
            return

        if field == "max_players":
            try:
                new_value = int(new_value)
    
                if new_value <= 0:
                    raise ValueError

            except ValueError:
                await update.message.reply_text(
                    "❌ Please enter a valid number."
                )
                return

            current_players = len(get_players(game_id))

            if new_value < current_players:
                await update.message.reply_text(
                    f"❌ There are already {current_players} players.\n"
                    f"Maximum players cannot be below that."
                )
                return

        if field == "price":
            try:
                float(new_value)
            except ValueError:
                await update.message.reply_text(
                    "❌ Please enter a valid price.\n"
                    "Example: 16"
                )
                return

        allowed_fields = {
            "date",
            "time",
            "location",
            "courts",
            "price",
            "max_players",
            "level",
            "shuttle",
        }

        if field not in allowed_fields:
            return

        conn = get_db()

        conn.execute(
            f"""
            UPDATE games
            SET {field} = ?
            WHERE id = ?
            """,
            (
                new_value,
                game_id,
            ),
        )

        conn.commit()
        conn.close()

        game = get_game(game_id)

        if field == "max_players":
            promoted = promote_waitlist(game_id)
        else:
            promoted = []

        players = get_players(game_id)
        waitlist = get_waitlist(game_id)

        try:
            await context.bot.edit_message_text(
                chat_id=game["chat_id"],
                message_id=game["message_id"],
                text=make_game_text(
                    game,
                    players,
                    waitlist,
                ),
                reply_markup=make_keyboard(game_id),
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
    
        context.user_data.pop("editing_game", None)
        context.user_data.pop("edit_game_id", None)
        context.user_data.pop("edit_field", None)
    
        await update.message.reply_text(
            (
                "✅ Game updated!\n\n"
                f"📅 {game['date']}\n"
                f"⏰ {game['time']}\n"
                f"📍 {game['location']}\n"
                f"💰 ${game['price']}/pax"
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✏️ Edit another field",
                            callback_data=f"editgame:{game_id}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ Back to menu",
                            callback_data="menu_home",
                        )
                    ],
                ]
            ),
        )
        
        for player in promoted:
            try:
                await context.bot.send_message(
                    chat_id=player["owner_id"],
                    text=(
                        f"🏸 A slot opened up for "
                        f"{game['date']} and you've been "
                        f"moved from the waitlist into the game!"
                    ),
                )
            except Exception:
                pass
    
        return
        
        		
    if not context.user_data.get("creating_game"):
        return

    if not is_bot_admin(update.effective_user.id):
        return

    step_index = context.user_data.get(
        "create_game_step",
        0,
    )

    data = context.user_data.get(
        "create_game_data",
        {},
    )

    text = update.message.text.strip()

    step = CREATE_GAME_STEPS[step_index]

    # Validate maximum players
    if step == "max_players":
        try:
            maximum = int(text)

            if maximum <= 0:
                raise ValueError

        except ValueError:
            await update.message.reply_text(
                "❌ Please enter a valid number.\n\n"
                "Example: 8"
            )
            return

        data["max_players"] = maximum

    else:
        data[step] = text

    step_index += 1

    context.user_data["create_game_step"] = (
        step_index
    )
    context.user_data["create_game_data"] = (
        data
    )

    # Finished collecting everything
    if step_index >= len(CREATE_GAME_STEPS):
        preview = (
            "🏸 Game Preview\n\n"
            f"📅 {data['date']}\n"
            f"⏰ {data['time']}\n"
            f"Location: {data['location']}\n"
            f"{data['courts']}\n"
            f"${data['price']}/pax\n\n"
            f"Level: {data['level']}\n"
            f"{data['shuttle']}\n\n"
            f"Maximum players: "
            f"{data['max_players']}"
        )

        await update.message.reply_text(
            preview,
            reply_markup=(
                make_create_game_confirm_keyboard()
            ),
        )

        return

    next_step = CREATE_GAME_STEPS[
        step_index
    ]

    prompts = {
        "date": (
            "📅 What is the date?\n\n"
            "Example: 26th Aug Wednesday"
        ),
        "time": (
            "⏰ What time is the game?\n\n"
            "Example: 9-11 PM"
        ),
        "location": (
            "📍 Where is the game?\n\n"
            "Example: The Sports Arena "
            "(Jalan Kayu)"
        ),
        "courts": (
            "🏸 Enter the court information.\n\n"
            "Example: 2 courts (C3,4)"
        ),
        "price": (
            "💰 What is the price per pax?\n\n"
            "Enter just the amount.\n"
            "Example: 16"
        ),
        "level": (
            "🎯 What is the level?\n\n"
            "Example: HB-LI"
        ),
        "shuttle": (
            "🪶 What shuttle will be used?\n\n"
            "Example: RSL Ultimate"
        ),
    }

    await update.message.reply_text(
        prompts[next_step]
    )
    
async def create_game_confirm_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised.",
            show_alert=True,
        )
        return

    if not context.user_data.get("creating_game"):
        await query.answer(
            "Create-game session expired.",
            show_alert=True,
        )
        return

    data = context.user_data.get("create_game_data")

    if not data:
        await query.answer(
            "No game data found.",
            show_alert=True,
        )
        return

    # Create game in database
    conn = get_db()

    cursor = conn.execute(
        """
        INSERT INTO games (
            date,
            time,
            location,
            courts,
            price,
            level,
            shuttle,
            max_players
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data["date"],
            data["time"],
            data["location"],
            data["courts"],
            data["price"],
            data["level"],
            data["shuttle"],
            data["max_players"],
        ),
    )

    game_id = cursor.lastrowid

    conn.commit()
    conn.close()

    game = get_game(game_id)

    # Post game into badminton group
    try:
        sent_message = await context.bot.send_message(
            chat_id=GAME_CHAT_ID,
            text=make_game_text(
                game,
                [],
                [],
            ),
            reply_markup=make_keyboard(game_id),
        )

    except Exception as e:
        # If Telegram posting fails, remove the game
        # so we don't leave a broken database entry.
        conn = get_db()

        conn.execute(
            """
            DELETE FROM games
            WHERE id = ?
            """,
            (game_id,),
        )

        conn.commit()
        conn.close()

        await query.answer(
            "❌ Could not post game.",
            show_alert=True,
        )

        print(
            f"Failed to post game {game_id}: {e}"
        )

        return

    # Save Telegram message location
    conn = get_db()

    conn.execute(
        """
        UPDATE games
        SET chat_id = ?,
            message_id = ?
        WHERE id = ?
        """,
        (
            sent_message.chat_id,
            sent_message.message_id,
            game_id,
        ),
    )

    conn.commit()
    conn.close()

    # Clear creation session
    context.user_data.pop(
        "creating_game",
        None,
    )
    context.user_data.pop(
        "create_game_step",
        None,
    )
    context.user_data.pop(
        "create_game_data",
        None,
    )

    await query.answer(
        "🏸 Game created!"
    )

    await query.edit_message_text(
        (
            "✅ Game created and posted!\n\n"
            f"📅 {game['date']}\n"
            f"⏰ {game['time']}\n"
            f"📍 {game['location']}\n"
            f"💰 ${game['price']}/pax"
        ),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬅️ Back to menu",
                        callback_data="menu_home",
                    )
                ]
            ]
        ),
    )
    
async def admin_edit_game_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised.",
            show_alert=True,
        )
        return

    conn = get_db()

    games = conn.execute(
        """
        SELECT *
        FROM games
        WHERE finished = 0
        ORDER BY id DESC
        """
    ).fetchall()

    conn.close()

    await query.answer()

    if not games:
        await query.edit_message_text(
            "There are no active games to edit.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Back",
                            callback_data="menu_home",
                        )
                    ]
                ]
            ),
        )
        return

    await query.edit_message_text(
        "✏️ Choose a game to edit:",
        reply_markup=make_edit_game_keyboard(games),
    )
    
async def admin_repost_game_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised.",
            show_alert=True,
        )
        return

    conn = get_db()

    games = conn.execute(
        """
        SELECT *
        FROM games
        WHERE finished = 0
        ORDER BY id DESC
        """
    ).fetchall()

    conn.close()

    await query.answer()

    if not games:
        await query.edit_message_text(
            "There are no active games to repost.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Back",
                            callback_data="menu_home",
                        )
                    ]
                ]
            ),
        )
        return

    await query.edit_message_text(
        (
            "♻️ Repost Game\n\n"
            "Choose the game whose original "
            "group message was deleted:"
        ),
        reply_markup=make_repost_game_keyboard(games),
    )
    
async def repost_game_select_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised.",
            show_alert=True,
        )
        return

    _, game_id_text = query.data.split(":")
    game_id = int(game_id_text)

    game = get_game(game_id)

    if game is None or game["finished"]:
        await query.answer(
            "This game is no longer active.",
            show_alert=True,
        )
        return

    players = get_players(game_id)
    waitlist = get_waitlist(game_id)

    await query.answer()

    await query.edit_message_text(
        (
            "⚠️ Repost this game to the group?\n\n"
            f"{make_game_text(game, players, waitlist)}\n\n"
            "This should only be used if the "
            "original group message was deleted."
        ),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Repost game",
                        callback_data=f"repostconfirm:{game_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="admin_repost_game",
                    )
                ],
            ]
        ),
    )
    
    
async def repost_game_confirm_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised.",
            show_alert=True,
        )
        return

    _, game_id_text = query.data.split(":")
    game_id = int(game_id_text)

    game = get_game(game_id)

    if game is None or game["finished"]:
        await query.answer(
            "This game is no longer active.",
            show_alert=True,
        )
        return

    players = get_players(game_id)
    waitlist = get_waitlist(game_id)

    try:
        sent_message = await context.bot.send_message(
            chat_id=GAME_CHAT_ID,
            text=make_game_text(
                game,
                players,
                waitlist,
            ),
            reply_markup=make_keyboard(game_id),
        )

    except Exception as e:
        print(
            f"Failed to repost game {game_id}: {e}",
            flush=True,
        )

        await query.answer(
            "❌ Could not repost game.",
            show_alert=True,
        )
        return

    # Point the existing game at the new Telegram message.
    conn = get_db()

    conn.execute(
        """
        UPDATE games
        SET chat_id = ?,
            message_id = ?
        WHERE id = ?
        """,
        (
            sent_message.chat_id,
            sent_message.message_id,
            game_id,
        ),
    )

    conn.commit()
    conn.close()

    await query.answer(
        "✅ Game reposted!"
    )

    await query.edit_message_text(
        (
            "✅ Game reposted successfully!\n\n"
            f"📅 {game['date']}\n"
            f"⏰ {game['time']}\n"
            f"📍 {game['location']}\n\n"
            f"👥 {len(players)} player(s)\n"
            f"⏳ {len(waitlist)} waitlisted\n\n"
            "The existing signups were preserved."
        ),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬅️ Back to menu",
                        callback_data="menu_home",
                    )
                ]
            ]
        ),
    )
    
    
async def edit_game_select_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised.",
            show_alert=True,
        )
        return

    _, game_id_text = query.data.split(":")
    game_id = int(game_id_text)

    game = get_game(game_id)

    if game is None or game["finished"]:
        await query.answer(
            "This game is no longer active.",
            show_alert=True,
        )
        return

    await query.answer()

    await query.edit_message_text(
        (
            "✏️ Edit Game\n\n"
            f"📅 {game['date']}\n"
            f"⏰ {game['time']}\n"
            f"📍 {game['location']}\n"
            f"🏸 {game['courts']}\n"
            f"💰 ${game['price']}/pax\n"
            f"👥 Max: {game['max_players']}\n"
            f"🎯 {game['level']}\n"
            f"🪶 {game['shuttle']}\n\n"
            "What would you like to edit?"
        ),
        reply_markup=make_edit_field_keyboard(game_id),
    )
    
async def edit_field_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised.",
            show_alert=True,
        )
        return

    _, game_id_text, field = query.data.split(":")
    game_id = int(game_id_text)

    allowed_fields = {
        "date",
        "time",
        "location",
        "courts",
        "price",
        "max_players",
        "level",
        "shuttle",
    }

    if field not in allowed_fields:
        await query.answer(
            "Invalid field.",
            show_alert=True,
        )
        return

    game = get_game(game_id)

    if game is None or game["finished"]:
        await query.answer(
            "This game is no longer active.",
            show_alert=True,
        )
        return

    context.user_data["editing_game"] = True
    context.user_data["edit_game_id"] = game_id
    context.user_data["edit_field"] = field

    labels = {
        "date": "date",
        "time": "time",
        "location": "location",
        "courts": "court information",
        "price": "price per pax",
        "max_players": "maximum number of players",
        "level": "level",
        "shuttle": "shuttle",
    }

    await query.answer()

    await query.edit_message_text(
        (
            f"✏️ Editing {labels[field]}\n\n"
            f"Current value:\n"
            f"{game[field]}\n\n"
            "Send the new value:"
        )
    )
    
async def credit_topup_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    await query.answer()

    await query.edit_message_text(
        (
            "💳 Top Up Credits\n\n"
            "Choose how much you want to top up:"
        ),
        reply_markup=make_topup_keyboard(),
    )
    
async def topup_custom_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    # Clear other text-input workflows
    context.user_data.pop(
        "creating_game",
        None,
    )
    context.user_data.pop(
        "create_game_step",
        None,
    )
    context.user_data.pop(
        "create_game_data",
        None,
    )

    context.user_data.pop(
        "editing_game",
        None,
    )
    context.user_data.pop(
        "edit_game_id",
        None,
    )
    context.user_data.pop(
        "edit_field",
        None,
    )
    
    context.user_data["custom_topup"] = True

    await query.answer()

    await query.edit_message_text(
        (
            "💳 Custom Top Up\n\n"
            "Enter the amount you want to top up.\n\n"
            "Examples:\n"
            "35\n"
            "35.50\n"
            "$50"
        ),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data="topup_cancel",
                    )
                ]
            ]
        ),
    )
    
async def credit_history_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    user_id = query.from_user.id

    conn = get_db()

    transactions = conn.execute(
        """
        SELECT
            credit_transactions.amount_cents,
            credit_transactions.transaction_type,
            credit_transactions.game_id,
            credit_transactions.description,
            credit_transactions.created_at,
            games.date AS game_date,
            games.time AS game_time
        FROM credit_transactions

        LEFT JOIN games
            ON games.id =
               credit_transactions.game_id

        WHERE credit_transactions.owner_id = ?

        ORDER BY credit_transactions.id DESC
        LIMIT 30
        """,
        (user_id,),
    ).fetchall()

    conn.close()

    balance = get_credit_balance(
        user_id
    )

    await query.answer()

    if not transactions:
        await query.edit_message_text(
            (
                "📜 Credit History\n\n"
                "You do not have any credit "
                "transactions yet.\n\n"
                f"Current balance: "
                f"${balance / 100:.2f}"
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Back to credits",
                            callback_data="menu_credit",
                        )
                    ]
                ]
            ),
        )
        return

    lines = [
        "📜 Credit History",
        "",
        f"Current balance: ${balance / 100:.2f}",
        "",
    ]

    for transaction in transactions:
        amount_cents = transaction[
            "amount_cents"
        ]

        transaction_type = transaction[
            "transaction_type"
        ]

        created_at = transaction[
            "created_at"
        ]

        description = transaction[
            "description"
        ]

        if transaction_type == "topup":
            icon = "➕"
            label = "Top up"

        elif transaction_type == "game":
            icon = "🏸"

            if transaction["game_date"]:
                label = (
                    f"{transaction['game_date']} "
                    f"• {transaction['game_time']}"
                )
            else:
                label = "Game"

        elif transaction_type == "manual_deduction":
            icon = "➖"
            label = "Manual deduction"

        else:
            icon = "💳"
            label = transaction_type.replace(
                "_",
                " ",
            ).title()

        if amount_cents >= 0:
            amount_text = (
                f"+${amount_cents / 100:.2f}"
            )
        else:
            amount_text = (
                f"-${abs(amount_cents) / 100:.2f}"
            )

        lines.append(
            f"{icon} {label} • {amount_text}"
        )

        if (
            description
            and transaction_type != "game"
        ):
            lines.append(
                f"   {description}"
            )

        lines.append(
            f"   {created_at}"
        )

        lines.append("")

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬅️ Back to credits",
                        callback_data="menu_credit",
                    )
                ]
            ]
        ),
    )
    
       
async def create_topup_request(
    context,
    user,
    amount_cents,
):
    conn = get_db()

    cursor = conn.execute(
        """
        INSERT INTO topup_requests (
            owner_id,
            owner_name,
            amount_cents,
            status
        )
        VALUES (?, ?, ?, 'pending')
        """,
        (
            user.id,
            user.full_name,
            amount_cents,
        ),
    )

    request_id = cursor.lastrowid

    conn.commit()
    conn.close()

    amount = amount_cents / 100

    # Notify all admins
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    "💳 Credit Top-up Request\n\n"
                    f"Player: {user.full_name}\n"
                    f"Amount: ${amount:.2f}\n\n"
                    "Confirm only after payment "
                    "has been received."
                ),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "✅ Approve",
                                callback_data=(
                                    f"topupapprove:{request_id}"
                                ),
                            ),
                            InlineKeyboardButton(
                                "❌ Reject",
                                callback_data=(
                                    f"topupreject:{request_id}"
                                ),
                            ),
                        ]
                    ]
                ),
            )

        except Exception as e:
            print(
                f"Could not send top-up request "
                f"to admin {admin_id}: {e}"
            )

    return request_id
    
async def topup_amount_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    _, amount_text = query.data.split(":")
    amount_cents = int(amount_text)

    user = query.from_user

    await create_topup_request(
        context,
        user,
        amount_cents,
    )

    await query.answer()

    await query.edit_message_text(
        (
            "✅ Top-up request submitted!\n\n"
            f"Amount: ${amount_cents / 100:.2f}\n\n"
            "Your credits will be added after "
            "an admin confirms your payment."
        ),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬅️ Back to menu",
                        callback_data="menu_home",
                    )
                ]
            ]
        ),
    )
            
async def topup_approve_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised.",
            show_alert=True,
        )
        return

    _, request_id_text = query.data.split(":")
    request_id = int(request_id_text)

    conn = get_db()

    request = conn.execute(
        """
        SELECT *
        FROM topup_requests
        WHERE id = ?
        """,
        (request_id,),
    ).fetchone()

    if request is None:
        conn.close()

        await query.answer(
            "Top-up request not found.",
            show_alert=True,
        )
        return

    if request["status"] != "pending":
        conn.close()

        await query.answer(
            "This request has already been handled.",
            show_alert=True,
        )
        return

    cursor = conn.execute(
        """
        UPDATE topup_requests
        SET status = 'approved'
        WHERE id = ?
          AND status = 'pending'
        """,
        (request_id,),
    )

    conn.commit()
    conn.close()

    if cursor.rowcount == 0:
        await query.answer(
            "This request has already been handled.",
            show_alert=True,
        )
        return
        
    debt_paid_cents, credit_added_cents = (
        apply_topup_to_debt(
            request["owner_id"],
            request["owner_name"],
            request["amount_cents"],
        )
    )

    new_balance = get_credit_balance(
        request["owner_id"]
    )

    amount = request["amount_cents"] / 100

    await query.answer(
        "✅ Top-up approved."
    )

    await query.edit_message_text(
        (
            "✅ Top-up approved\n\n"
            f"{request['owner_name']}\n"
            f"Top-up: ${amount:.2f}\n"
            f"🧾 Debt paid: "
            f"${debt_paid_cents / 100:.2f}\n"
            f"💳 Credit added: "
            f"${credit_added_cents / 100:.2f}\n"
            f"💰 New credit balance: "
            f"${new_balance / 100:.2f}"
        )
    )

    try:
        await context.bot.send_message(
            chat_id=request["owner_id"],
            text=(
                "✅ Your Baddy Buddies top-up "
                "has been approved!\n\n"
                f"Top-up: ${amount:.2f}\n"
                f"🧾 Applied to outstanding balance: "
                f"${debt_paid_cents / 100:.2f}\n"
                f"💳 Added to credit: "
                f"${credit_added_cents / 100:.2f}\n"
                f"💰 Credit balance: "
                f"${new_balance / 100:.2f}"
            ),
        )
    except Exception:
        pass
        
async def topup_reject_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_bot_admin(query.from_user.id):
        await query.answer(
            "❌ You are not authorised.",
            show_alert=True,
        )
        return

    _, request_id_text = query.data.split(":")
    request_id = int(request_id_text)

    conn = get_db()

    request = conn.execute(
        """
        SELECT *
        FROM topup_requests
        WHERE id = ?
        """,
        (request_id,),
    ).fetchone()

    if request is None:
        conn.close()

        await query.answer(
            "Top-up request not found.",
            show_alert=True,
        )
        return

    if request["status"] != "pending":
        conn.close()

        await query.answer(
            "This request has already been handled.",
            show_alert=True,
        )
        return

    conn.execute(
        """
        UPDATE topup_requests
        SET status = 'rejected'
        WHERE id = ?
        """,
        (request_id,),
    )

    conn.commit()
    conn.close()

    amount = request["amount_cents"] / 100

    await query.answer(
        "Top-up rejected."
    )

    await query.edit_message_text(
        (
            "❌ Top-up rejected\n\n"
            f"{request['owner_name']}\n"
            f"${amount:.2f}"
        )
    )

    try:
        await context.bot.send_message(
            chat_id=request["owner_id"],
            text=(
                "❌ Your Baddy Buddies top-up "
                "request was not approved.\n\n"
                f"Amount: ${amount:.2f}"
            ),
        )
    except Exception:
        pass
        
async def topup_cancel_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    context.user_data.pop(
        "custom_topup",
        None,
    )
    query = update.callback_query

    await query.answer()

    balance = get_credit_balance(
        query.from_user.id
    )

    await query.edit_message_text(
        (
            "💳 Baddy Buddies Credit\n\n"
            f"Available credit: "
            f"${balance / 100:.2f}"
        ),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "➕ Top up",
                        callback_data="credit_topup",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📜 Credit history",
                        callback_data="credit_history",
                    )
       	        ],
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="menu_home",
                    )
                ],
            ]
        ),
    )
       
async def broadcast_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_bot_admin(
        update.effective_user.id
    ):
        await update.message.reply_text(
            "❌ You are not authorised to broadcast messages."
        )
        return

    message = (
        update.message.text
        .partition(" ")[2]
        .strip()
    )

    if not message:
        await update.message.reply_text(
            (
                "📢 Broadcast\n\n"
                "Use:\n"
                "/broadcast Your message here"
            )
        )
        return

    conn = get_db()

    users = conn.execute(
        """
        SELECT
            user_id,
            full_name
        FROM bot_users
        ORDER BY full_name
        """
    ).fetchall()

    conn.close()

    if not users:
        await update.message.reply_text(
            "❌ There are no registered bot users."
        )
        return

    sent = 0
    failed = 0

    for user in users:
        try:
            await context.bot.send_message(
                chat_id=user["user_id"],
                text=message,
            )

            sent += 1

        except Exception as e:
            print(
                f"Broadcast failed for "
                f"{user['full_name']} "
                f"({user['user_id']}): {e}",
                flush=True,
            )

            failed += 1

    await update.message.reply_text(
        (
            "📢 Broadcast complete!\n\n"
            f"✅ Sent: {sent}\n"
            f"❌ Failed: {failed}"
        )
    )
    
async def notify_admins_of_pullout(
    context,
    game,
    removed_name,
    removed_type,
    old_status,
    removed_by,
):
    if old_status == "player":
        status_text = "Confirmed player"
    else:
        status_text = "Waitlist"

    if removed_type == "self":
        type_text = "Own spot"
    else:
        type_text = "+1"

    text = (
        "🚨 Player pulled out\n\n"
        f"👤 Account: {removed_by}\n"
        f"🏸 Removed: {removed_name}\n"
        f"👥 Type: {type_text}\n"
        f"📋 Was: {status_text}\n\n"
        f"📅 {game['date']}\n"
        f"⏰ {game['time']}\n"
        f"📍 {game['location']}"
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=text,
            )
        except Exception as e:
            print(
                f"Could not notify admin "
                f"{admin_id} about pullout: {e}",
                flush=True,
            ) 
# =========================================================
# BUTTON HANDLER
# =========================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    action, game_id_text = query.data.split(":")
    game_id = int(game_id_text)

    game = get_game(game_id)

    if game is None:
        await query.answer(
            "This game no longer exists.",
            show_alert=True,
        )
        return

    if game["finished"]:
        await query.answer(
            "🔒 This game has already finished.",
            show_alert=True,
        )
        return

    user = query.from_user
    user_id = user.id
    user_name = user.full_name
    
    register_bot_user(
        user.id,
        user.full_name,
        user.username,
    )

    promoted = []

    # -----------------------------------------------------
    # ADD ME
    # -----------------------------------------------------

    if action == "add":
        conn = get_db()

        existing = conn.execute(
            """
            SELECT id
            FROM entries
            WHERE game_id = ?
              AND owner_id = ?
              AND type = 'self'
            """,
            (
                game_id,
                user_id,
            ),
        ).fetchone()

        conn.close()

        if existing:
            await query.answer(
                "You're already signed up!",
                show_alert=True,
            )
            return

        players = get_players(game_id)

        if len(players) < game["max_players"]:
            status = "player"
            message = "You're in! 🏸"
        else:
            status = "waitlist"

            waitlist_position = (
                len(get_waitlist(game_id)) + 1
            )

            message = (
                f"Game is full. "
                f"You're waitlist #{waitlist_position}."
            )

        position = get_next_position(
            game_id,
            status,
        )

        conn = get_db()

        conn.execute(
            """
            INSERT INTO entries (
                game_id,
                owner_id,
                owner_name,
                name,
                type,
                guest_number,
                status,
                position
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                game_id,
                user_id,
                user_name,
                user_name,
                "self",
                None,
                status,
                position,
            ),
        )

        conn.commit()
        conn.close()

        await query.answer(message)

    # -----------------------------------------------------
    # ADD +1
    # -----------------------------------------------------

    elif action == "guest":
        guest_number = get_next_guest_number(
            game_id,
            user_id,
        )

        guest_name = (
            f"{user_name} +{guest_number}"
        )

        players = get_players(game_id)

        if len(players) < game["max_players"]:
            status = "player"

            message = (
                f"Added {guest_name}"
            )
        else:
            status = "waitlist"

            waitlist_position = (
                len(get_waitlist(game_id)) + 1
            )

            message = (
                f"{guest_name} is waitlist "
                f"#{waitlist_position}."
            )

        position = get_next_position(
            game_id,
            status,
        )

        conn = get_db()

        conn.execute(
            """
            INSERT INTO entries (
                game_id,
                owner_id,
                owner_name,
                name,
                type,
                guest_number,
                status,
                position
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                game_id,
                user_id,
                user_name,
                guest_name,
                "guest",
                guest_number,
                status,
                position,
            ),
        )

        conn.commit()
        conn.close()

        await query.answer(message)

    # -----------------------------------------------------
    # REMOVE ME
    # -----------------------------------------------------

    elif action == "remove_me":

        confirm_key = (
            user_id,
            game_id,
            "remove_me",
        )

        now = time.time()
        previous = pending_removals.get(confirm_key)

        if (
            previous is None
            or now - previous > REMOVE_CONFIRM_SECONDS
        ):
            pending_removals[confirm_key] = now

            await query.answer(
                "⚠️ Tap Remove me again within "
                "5 seconds to confirm.",
                show_alert=True,
            )
            return

        # Second tap confirmed
        pending_removals.pop(
            confirm_key,
            None,
        )

        conn = get_db()

        entry = conn.execute(
            """
            SELECT *
            FROM entries
            WHERE game_id = ?
              AND owner_id = ?
              AND type = 'self'
            ORDER BY
                CASE
                    WHEN status = 'player' THEN 0
                    ELSE 1
                END,
                position ASC
            LIMIT 1
            """,
            (
                game_id,
                user_id,
            ),
        ).fetchone()

        if entry is None:
            conn.close()

            await query.answer(
                "Your own name isn't signed up.",
                show_alert=True,
            )
            return

        old_status = entry["status"]

        conn.execute(
            """
            DELETE FROM entries
            WHERE id = ?
            """,
            (entry["id"],),
        )

        conn.commit()
        conn.close()

        compact_positions(
            game_id,
            old_status,
        )

        if old_status == "player":
            promoted = promote_waitlist(
                game_id,
            )

        await notify_admins_of_pullout(
            context=context,
            game=game,
            removed_name=entry["name"],
            removed_type="self",
            old_status=old_status,
            removed_by=user_name,
        )

        await query.answer(
            "✅ You have been removed."
        )
    # -----------------------------------------------------
    # REMOVE +1
    # -----------------------------------------------------

    elif action == "remove_guest":

        confirm_key = (
            user_id,
            game_id,
            "remove_guest",
        )

        now = time.time()
        previous = pending_removals.get(confirm_key)

        if (
            previous is None
            or now - previous > REMOVE_CONFIRM_SECONDS
        ):
            pending_removals[confirm_key] = now

            await query.answer(
                "⚠️ Tap Remove +1 again within "
                "5 seconds to confirm.",
                show_alert=True,
            )
            return

        # Second tap confirmed
        pending_removals.pop(
            confirm_key,
            None,
        )

        conn = get_db()

        guest = conn.execute(
            """
            SELECT *
            FROM entries
            WHERE game_id = ?
              AND owner_id = ?
              AND type = 'guest'
            ORDER BY guest_number DESC
            LIMIT 1
            """,
            (
                game_id,
                user_id,
            ),
        ).fetchone()

        if guest is None:
            conn.close()

            await query.answer(
                "You don't have any +1s.",
                show_alert=True,
            )
            return

        old_status = guest["status"]

        conn.execute(
            """
            DELETE FROM entries
            WHERE id = ?
            """,
            (guest["id"],),
        )

        conn.commit()
        conn.close()

        compact_positions(
            game_id,
            old_status,
        )

        if old_status == "player":
            promoted = promote_waitlist(
                game_id,
            )

        await notify_admins_of_pullout(
            context=context,
            game=game,
            removed_name=guest["name"],
            removed_type="guest",
            old_status=old_status,
            removed_by=user_name,
        )

        renumber_guests(
            game_id,
            user_id,
            user_name,
        )

        await query.answer(
            "✅ Your latest +1 has been removed."
        )

    # -----------------------------------------------------
    # UPDATE GAME MESSAGE
    # -----------------------------------------------------

    game = get_game(game_id)
    players = get_players(game_id)
    waitlist = get_waitlist(game_id)

    # Update the original group game message
    await context.bot.edit_message_text(
        chat_id=game["chat_id"],
        message_id=game["message_id"],
        text=make_game_text(
            game,
            players,
            waitlist,
        ),
        reply_markup=make_keyboard(game_id),
    )

    # Also update the current PM/game-view message
    if query.message.chat_id != game["chat_id"]:
        try:
            await query.edit_message_text(
                make_game_text(
                    game,
                    players,
                    waitlist,
                ),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "➕ Add me",
                                callback_data=f"add:{game_id}",
                            ),
                            InlineKeyboardButton(
                                "👥 Add +1",
                                callback_data=f"guest:{game_id}",
                            ),
                        ],
                        [
                            InlineKeyboardButton(
                                "➖ Remove me",
                                callback_data=f"remove_me:{game_id}",
                            ),
                            InlineKeyboardButton(
                                "👥 Remove +1",
                                callback_data=f"remove_guest:{game_id}",
                            ),
                        ],
                        [
                            InlineKeyboardButton(
                                "⬅️ Back to games",
                                callback_data="menu_games",
                            )
                        ],
                    ]
                ),
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                raise
            
    # -----------------------------------------------------
    # PING PROMOTED PLAYERS
    # -----------------------------------------------------

    for player in promoted:
        mention = (
            f'<a href="tg://user?id={player["owner_id"]}">'
            f'{player["name"]}</a>'
        )

        await query.message.reply_text(
            f"🏸 {mention}, a slot opened up and "
            f"you've been moved from the waitlist "
            f"into the game!",
            parse_mode="HTML",
        )

# =========================================================
# MAIN
# =========================================================

def main():
    setup_database()
    import_existing_players()

    request = HTTPXRequest(
        connect_timeout=20,
        read_timeout=20,
        write_timeout=20,
        pool_timeout=20,
    )

    app = (
        Application.builder()
        .token(TOKEN)
        .request(request)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        CommandHandler(
            "game",
            create_game,
        )
    )
	
    app.add_handler(
        CallbackQueryHandler(
            finish_game_button,
            pattern=r"^(finish_confirm:\d+|finish_cancel)$",
        )
    )
    
    app.add_handler(
        CallbackQueryHandler(
            menu_button,
            pattern=(
                r"^(menu_home|menu_balance|menu_credit|menu_games|menu_debts)$"
            )
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            view_game_button,
            pattern=r"^viewgame:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            button_handler,
            pattern=(
                r"^(add|guest|remove_me|remove_guest):\d+$"
            ),
        )
    )
    
    app.add_handler(
        CommandHandler(
            "finishgame",
            finish_game_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "balance",
            balance_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "balances",
            balance_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "games",
            games_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "debts",
            debts_command,
        )
    )
    
    app.add_handler(
        CallbackQueryHandler(
            pay_all_button,
            pattern=r"^payall:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            pay_choose_button,
            pattern=r"^paychoose:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            pay_game_button,
            pattern=r"^paygame:\d+:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            pay_cancel_button,
            pattern=r"^pay_cancel$",
        )
    )
    
    app.add_handler(
        CallbackQueryHandler(
            remind_all_button,
            pattern=r"^remind_all$",
        )
    )
    
    app.add_handler(
    CallbackQueryHandler(
        admin_finish_game_button,
        pattern=r"^admin_finish_game$",
    )
)

    app.add_handler(
        CallbackQueryHandler(
            admin_cancel_game_button,
            pattern=r"^admin_cancel_game$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            cancel_game_select_button,
            pattern=r"^cancelgame:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            cancel_game_confirm_button,
            pattern=r"^cancelconfirm:\d+$",
        )
    )
    
    app.add_handler(
        CallbackQueryHandler(
            admin_create_game_button,
            pattern=r"^admin_create_game$",
        )
    )    

    app.add_handler(
        CallbackQueryHandler(
            create_game_confirm_button,
            pattern=r"^create_game_confirm$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            create_game_cancel_button,
            pattern=r"^create_game_cancel$",
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            create_game_message_handler,
        )
    )
    
    app.add_handler(
        CallbackQueryHandler(
            admin_edit_game_button,
            pattern=r"^admin_edit_game$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            edit_game_select_button,
            pattern=r"^editgame:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            edit_field_button,
            pattern=(
                r"^editfield:\d+:"
                r"(date|time|location|courts|price|max_players|level|shuttle)$"
            ),
        )
    )
    
    app.add_handler(
        CallbackQueryHandler(
            credit_topup_button,
            pattern=r"^credit_topup$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            topup_amount_button,
            pattern=r"^topup:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            topup_approve_button,
            pattern=r"^topupapprove:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            topup_reject_button,
            pattern=r"^topupreject:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            topup_cancel_button,
            pattern=r"^topup_cancel$",
        )
    )
    
    app.add_handler(
        CallbackQueryHandler(
            topup_custom_button,
            pattern=r"^topup_custom$",
        )
    )
    
    app.add_handler(
        CommandHandler(
            "broadcast",
            broadcast_command,
        )
    )    
    
    app.add_handler(
        CallbackQueryHandler(
            admin_repost_game_button,
            pattern=r"^admin_repost_game$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            repost_game_select_button,
            pattern=r"^repostgame:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            repost_game_confirm_button,
            pattern=r"^repostconfirm:\d+$",
        )
    )
    
    app.add_handler(
        CallbackQueryHandler(
            debts_by_player_button,
            pattern=r"^debts_by_player$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            debts_by_game_button,
            pattern=r"^debts_by_game$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            debt_game_button,
            pattern=r"^debtgame:\d+$",
        )
    )    

    app.add_handler(
        CallbackQueryHandler(
            debt_toggle_button,
            pattern=r"^debttoggle:\d+:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            debt_select_all_button,
            pattern=r"^debtselectall:\d+$",
        )
    )    

    app.add_handler(
        CallbackQueryHandler(
            debt_pay_selected_button,
            pattern=r"^debtpayselected:\d+$",
        )
    )
    
    app.add_handler(
    CallbackQueryHandler(
        admin_manage_credits_button,
        pattern=r"^admin_manage_credits$",
        )
    )
	
    app.add_handler(
        CallbackQueryHandler(
            credit_history_button,
            pattern=r"^credit_history$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            credit_player_button,
            pattern=r"^creditplayer:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            credit_deduct_button,
            pattern=r"^creditdeduct:\d+:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            credit_deduct_custom_button,
            pattern=r"^creditdeductcustom:\d+$",
        )
    )

    print("🏸 Baddy Buddies is running...")

    app.run_polling()


if __name__ == "__main__":
    main()
