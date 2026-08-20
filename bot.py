import os
import sqlite3
import time

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


# =========================================================
# TELEGRAM COMMANDS
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "🏸 Baddy Buddies bot is online!"
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
    
async def balance_command(
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
        await update.message.reply_text(
            "✅ You have no outstanding payments!"
        )
        return

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

        # Normal player
        if charge["entry_name"] == charge["owner_name"]:
            lines.append(
                f"• {charge['date']} — "
                f"${amount:.2f}"
            )

        # +1
        else:
            lines.append(
                f"• {charge['date']} "
                f"({charge['entry_name']}) — "
                f"${amount:.2f}"
            )

    lines.extend(
        [
            "",
            f"Total: ${total:.2f}",
        ]
    )

    await update.message.reply_text(
        "\n".join(lines)
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

    conn = get_db()

    for player in players:
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
                player["owner_id"],
                player["owner_name"],
                player["name"],
                price,
            ),
        )

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

    total = price * len(players)

    await query.answer()

    await query.edit_message_text(
        f"✅ Game finished!\n\n"
        f"📅 {game['date']}\n"
        f"👥 {len(players)} players\n"
        f"💰 ${price:.2f}/pax\n"
        f"🧾 ${total:.2f} total charges created"
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

    conn = get_db()

    charges = conn.execute(
        """
        SELECT
            charges.*,
            games.date
        FROM charges
        JOIN games
            ON games.id = charges.game_id
        WHERE charges.status != 'paid'
        ORDER BY
            charges.owner_name,
            games.id,
            charges.id
        """
    ).fetchall()

    conn.close()

    if not charges:
        await update.message.reply_text(
            "✅ Everyone has paid!"
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
        "💰 Outstanding Payments",
        "",
    ]

    for person in people.values():
        lines.append(
            f"{person['name']}:"
        )

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
                f"• {description} — "
                f"${amount:.2f}"
            )

        lines.append(
            f"Total: ${total:.2f}"
        )
        lines.append("")

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=make_debts_keyboard(people),
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
    

    print("🏸 Baddy Buddies is running...")

    app.run_polling()


if __name__ == "__main__":
    main()
