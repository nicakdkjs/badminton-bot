import os
import sqlite3

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
            message_id INTEGER
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
                    "➖ Remove +1",
                    callback_data=f"remove_guest:{game_id}",
                ),
            ],
        ]
    )


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
            "Removed your name."
        )

    # -----------------------------------------------------
    # REMOVE +1
    # -----------------------------------------------------

    elif action == "remove_guest":
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
            "Removed one +1."
        )

    # -----------------------------------------------------
    # UPDATE GAME MESSAGE
    # -----------------------------------------------------

    game = get_game(game_id)
    players = get_players(game_id)
    waitlist = get_waitlist(game_id)

    await query.edit_message_text(
        make_game_text(
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
            button_handler,
            pattern=(
                r"^(add|guest|remove_me|remove_guest):\d+$"
            ),
        )
    )

    print("🏸 Baddy Buddies is running...")

    app.run_polling()


if __name__ == "__main__":
    main()
