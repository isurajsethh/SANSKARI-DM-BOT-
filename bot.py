import asyncio
import html
import io
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from flask import Flask
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ================= CONFIGURATION =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "8283878643").strip())
PORT = int(os.getenv("PORT", "10000").strip())
DB_PATH = os.getenv("DB_PATH", "dm_bot.db").strip()

IST = timezone(timedelta(hours=5, minutes=30))
START_TIME = time.time()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ================= FLASK KEEP-ALIVE FOR RENDER =================
flask_app = Flask(__name__)


@flask_app.route("/")
def health_root():
    return "Sanskari DM Bot is running 24x7!", 200


@flask_app.route("/health")
def health_check():
    return {"status": "ok", "uptime_sec": int(time.time() - START_TIME)}, 200


def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


# ================= DATABASE HELPERS =================
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            username TEXT,
            is_banned INTEGER DEFAULT 0,
            ban_reason TEXT DEFAULT '',
            is_starred INTEGER DEFAULT 0,
            dm_count INTEGER DEFAULT 0,
            reply_count INTEGER DEFAULT 0,
            pending_reply INTEGER DEFAULT 0,
            first_seen TEXT,
            last_active TEXT,
            last_active_ts INTEGER DEFAULT 0
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            direction TEXT,
            msg_type TEXT,
            content TEXT,
            file_id TEXT,
            date_str TEXT,
            time_str TEXT,
            ts INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            body TEXT
        )
        """
    )
    defaults = {
        "away_mode": "0",
        "away_msg": "Abhi owner offline/busy hain. Aapka message save ho gaya hai, jaldi hi reply milega.",
        "maint_mode": "0",
        "maint_msg": "Bot abhi maintenance mode par hai. Thodi der baad try karein.",
        "log_channel": "",
        "log_enabled": "0",
        "welcome_hi": "नमस्ते! अपना संदेश यहाँ लिखें, owner जल्दी ही इसका रिप्लाई देगा।",
        "welcome_en": "Hello! Write your message here, the owner will reply to it soon.",
    }
    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    cur.execute("SELECT COUNT(*) as cnt FROM templates")
    if cur.fetchone()["cnt"] == 0:
        cur.executemany(
            "INSERT INTO templates (title, body) VALUES (?, ?)",
            [
                ("Notes Link", "Class 12th ke notes channel par upload kar diye gaye hain."),
                ("Wait Reply", "Aapka message mil gaya hai, thodi der mein check karke reply karta hoon."),
                ("Thanks", "Dhanyavaad! Judne ke liye shukriya."),
            ],
        )
    conn.commit()
    conn.close()


def get_setting(key: str, default: str = "") -> str:
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def now_ist():
    dt = datetime.now(IST)
    return dt.strftime("%d %b %Y"), dt.strftime("%I:%M %p IST"), int(dt.timestamp())


def upsert_user(user):
    date_str, time_str, ts = now_ist()
    stamp = f"{date_str} • {time_str}"
    conn = get_db()
    row = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user.id,)).fetchone()
    if row:
        conn.execute(
            """
            UPDATE users
            SET full_name = ?, username = ?, last_active = ?, last_active_ts = ?
            WHERE user_id = ?
            """,
            (user.full_name, user.username or "", stamp, ts, user.id),
        )
    else:
        conn.execute(
            """
            INSERT INTO users (user_id, full_name, username, first_seen, last_active, last_active_ts)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user.id, user.full_name, user.username or "", stamp, stamp, ts),
        )
    conn.commit()
    conn.close()


def get_user(user_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row


def save_message(user_id: int, direction: str, msg_type: str, content: str, file_id: str = ""):
    date_str, time_str, ts = now_ist()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO messages (user_id, direction, msg_type, content, file_id, date_str, time_str, ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, direction, msg_type, content, file_id, date_str, time_str, ts),
    )
    msg_row_id = cur.lastrowid
    if direction == "in":
        cur.execute(
            "UPDATE users SET dm_count = dm_count + 1, pending_reply = 1 WHERE user_id = ?",
            (user_id,),
        )
    else:
        cur.execute(
            "UPDATE users SET reply_count = reply_count + 1, pending_reply = 0 WHERE user_id = ?",
            (user_id,),
        )
    conn.commit()
    conn.close()
    return msg_row_id, date_str, time_str


# ================= STYLISH UI BUILDERS =================
def esc(text: str) -> str:
    return html.escape(str(text or ""))


def build_user_welcome_text() -> str:
    w_hi = esc(get_setting("welcome_hi"))
    w_en = esc(get_setting("welcome_en"))
    return (
        "╭━━━ ✨ 𝐖𝐄𝐋𝐂𝐎𝐌𝐄 • 𝐌𝐑. 𝐒𝐀𝐍𝐒𝐊𝐀𝐑𝐈 𝐃𝐌 ✨ ━━━╮
"
        f"┃ {w_hi}
"
        "┃
"
        f"┃ {w_en}
"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )


def user_welcome_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✍️ अपना संदेश लिखने के लिए यहाँ क्लिक करें", callback_data="user_compose")]]
    )


def build_owner_reply_header(date_str: str, time_str: str) -> str:
    return (
        "╭━━━ 📩 𝐎𝐖𝐍𝐄𝐑 𝐑𝐄𝐏𝐋𝐘 • 𝐌𝐑. 𝐒𝐀𝐍𝐒𝐊𝐀𝐑𝐈 ━━━╮
"
        f"┃ 📅 {date_str}   |   🕒 {time_str}
"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯
"
    )


def build_dm_card(dm_no: int, user_row, date_str: str, time_str: str, body_label: str, body_text: str) -> str:
    u_name = esc(user_row["full_name"])
    u_user = f"@{esc(user_row['username'])}" if user_row["username"] else "None"
    star_badge = " ⭐ VIP" if user_row["is_starred"] else ""
    total_dms = user_row["dm_count"]
    text = (
        f"╭━━━ ✦ 𝐍𝐄𝐖 𝐃𝐌 • #{dm_no:03d}{star_badge} ✦ ━━━╮
"
        f"┃ 👤 𝐍𝐚𝐦𝐞: {u_name}
"
        f"┃ 🔗 𝐔𝐬𝐞𝐫: {u_user}
"
        f"┃ 🆔 𝐈𝐃: <code>{user_row['user_id']}</code>
"
        f"┃ 📅 𝐃𝐚𝐭𝐞: {date_str}
"
        f"┃ 🕒 𝐓𝐢𝐦𝐞: {time_str}
"
        f"┃ 📊 𝐓𝐨𝐭𝐚𝐥: {total_dms} DMs
"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━╯
"
    )
    if body_text:
        text += f"💬 <b>{esc(body_label)}:</b>
{esc(body_text)}"
    else:
        text += f"📎 <b>Type:</b> {esc(body_label)}"
    return text


def dm_card_kb(user_id: int, is_banned: int = 0) -> InlineKeyboardMarkup:
    ban_btn = (
        InlineKeyboardButton("✅ Unban", callback_data=f"unban_{user_id}")
        if is_banned
        else InlineKeyboardButton("🚫 Ban", callback_data=f"ban_{user_id}")
    )
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💬 Reply", callback_data=f"reply_{user_id}"),
                InlineKeyboardButton("📜 All Msgs", callback_data=f"hist_{user_id}_1"),
                ban_btn,
            ]
        ]
    )


def build_settings_panel():
    date_str, time_str, _ = now_ist()
    conn = get_db()
    total_users = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
    total_dms = conn.execute("SELECT COUNT(*) as c FROM messages WHERE direction = 'in'").fetchone()["c"]
    pending = conn.execute("SELECT COUNT(*) as c FROM users WHERE pending_reply = 1 AND is_banned = 0").fetchone()["c"]
    conn.close()

    maint = get_setting("maint_mode") == "1"
    status_txt = "Paused ⏸️" if maint else "Online 🟢"

    text = (
        "╭━━━ ⚙️ 𝐎𝐖𝐍𝐄𝐑 𝐒𝐄𝐓𝐓𝐈𝐍𝐆𝐒 • 𝐌𝐑. 𝐒𝐀𝐍𝐒𝐊𝐀𝐑𝐈 ━━━╮
"
        f"┃ 👤 𝐀𝐝𝐦𝐢𝐧: Suraj Kumar (<code>{OWNER_ID}</code>)
"
        f"┃ 🤖 𝐒𝐭𝐚𝐭𝐮𝐬: {status_txt}  |  🕒 {time_str}
"
        f"┃ 👥 𝐔𝐬𝐞𝐫𝐬: {total_users}  |  📩 𝐃𝐌𝐬: {total_dms}  |  ⏳ 𝐏𝐞𝐧𝐝𝐢𝐧𝐠: {pending}
"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📋 User List (1/10)", callback_data="list_1"),
                InlineKeyboardButton("⏳ Pending Replies", callback_data="pending_1"),
            ],
            [
                InlineKeyboardButton("📊 Bot Stats", callback_data="set_stats"),
                InlineKeyboardButton("📢 Broadcast", callback_data="set_broadcast"),
            ],
            [
                InlineKeyboardButton("💤 Away Mode", callback_data="set_away"),
                InlineKeyboardButton("⏸️ Maintenance Mode", callback_data="set_maint"),
            ],
            [
                InlineKeyboardButton("🚫 Banned Users", callback_data="set_banned"),
                InlineKeyboardButton("⭐ Starred Users", callback_data="set_starred"),
            ],
            [
                InlineKeyboardButton("⚡ Quick Templates", callback_data="set_tpl"),
                InlineKeyboardButton("✏️ Welcome Editor", callback_data="set_welcome"),
            ],
            [
                InlineKeyboardButton("📡 Log Channel", callback_data="set_log"),
                InlineKeyboardButton("🗂️ Backup / Ping", callback_data="set_backup"),
            ],
            [InlineKeyboardButton("❌ Close Panel", callback_data="close_panel")],
        ]
    )
    return text, kb


def build_user_list_page(page: int = 1, only_pending: bool = False):
    per_page = 10
    conn = get_db()
    if only_pending:
        total = conn.execute("SELECT COUNT(*) as c FROM users WHERE pending_reply = 1 AND is_banned = 0").fetchone()["c"]
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * per_page
        rows = conn.execute(
            """
            SELECT * FROM users
            WHERE pending_reply = 1 AND is_banned = 0
            ORDER BY last_active_ts DESC
            LIMIT ? OFFSET ?
            """,
            (per_page, offset),
        ).fetchall()
        title = "⏳ 𝐏𝐄𝐍𝐃𝐈𝐍𝐆 𝐑𝐄𝐏𝐋𝐈𝐄𝐒 • 𝐔𝐒𝐄𝐑 𝐋𝐈𝐒𝐓"
        prefix = "pending"
    else:
        total = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * per_page
        rows = conn.execute(
            """
            SELECT * FROM users
            ORDER BY last_active_ts DESC
            LIMIT ? OFFSET ?
            """,
            (per_page, offset),
        ).fetchall()
        title = "📋 𝐒𝐀𝐍𝐒𝐊𝐀𝐑𝐈 𝐃𝐌 • 𝐔𝐒𝐄𝐑 𝐋𝐈𝐒𝐓"
        prefix = "list"
    conn.close()

    text = (
        f"╭━━━ {title} ━━━╮
"
        f"┃ 👥 𝐓𝐨𝐭𝐚𝐥 𝐔𝐬𝐞𝐫𝐬: {total}   |   📄 𝐏𝐚𝐠𝐞: {page} / {total_pages}
"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯
"
    )
    if not rows:
        text += "
<i>Abhi koi user record nahi hai.</i>"

    buttons = []
    for idx, r in enumerate(rows, start=offset + 1):
        star = "⭐ " if r["is_starred"] else ""
        ban = "🚫 " if r["is_banned"] else ""
        pend = "⏳ " if r["pending_reply"] else ""
        label = f"{idx}. {star}{ban}{pend}{r['full_name'][:18]} • {r['user_id']} ({r['dm_count']})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"hist_{r['user_id']}_{page}")])

    prev_page = page - 1 if page > 1 else 1
    next_page = page + 1 if page < total_pages else total_pages
    buttons.append(
        [
            InlineKeyboardButton("⬅️ Previous", callback_data=f"{prefix}_{prev_page}"),
            InlineKeyboardButton(f"{page}/{total_pages}", callback_data=f"noop_{page}_{total_pages}"),
            InlineKeyboardButton("Next ➡️", callback_data=f"{prefix}_{next_page}"),
        ]
    )
    buttons.append([InlineKeyboardButton("⚙️ Back to Settings", callback_data="open_settings")])
    return text, InlineKeyboardMarkup(buttons)


def build_user_history_view(user_id: int, back_page: int = 1):
    u = get_user(user_id)
    if not u:
        return "⚠️ User nahi mila.", InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back to List", callback_data=f"list_{back_page}")]]
        )

    conn = get_db()
    msgs = conn.execute(
        "SELECT * FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT 15",
        (user_id,),
    ).fetchall()
    conn.close()
    msgs = list(reversed(msgs))

    u_name = esc(u["full_name"])
    u_user = f"@{esc(u['username'])}" if u["username"] else "None"
    star_txt = "⭐ VIP" if u["is_starred"] else "Normal"
    ban_txt = f"🚫 Banned ({esc(u['ban_reason'])})" if u["is_banned"] else "✅ Active"

    header = (
        "╭━━━ 🗂️ 𝐔𝐒𝐄𝐑 𝐇𝐈𝐒𝐓𝐎𝐑𝐘 ━━━╮
"
        f"┃ 👤 𝐍𝐚𝐦𝐞: {u_name}
"
        f"┃ 🔗 𝐔𝐬𝐞𝐫: {u_user}
"
        f"┃ 🆔 𝐈𝐃: <code>{u['user_id']}</code>
"
        f"┃ 📩 𝐌𝐬𝐠𝐬: {u['dm_count']} DMs | {u['reply_count']} Replies
"
        f"┃ 🏷️ 𝐒𝐭𝐚𝐭𝐮𝐬: {star_txt} • {ban_txt}
"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━╯

"
    )

    lines = []
    for i, m in enumerate(msgs, start=1):
        stamp = f"[{m['date_str']} • {m['time_str']}]"
        body = esc(m["content"]) if m["content"] else f"[{esc(m['msg_type'])}]"
        if len(body) > 180:
            body = body[:177] + "..."
        if m["direction"] == "in":
            lines.append(f"<b>{i}.</b> <code>{stamp}</code>
💬 {body}")
        else:
            lines.append(f"<b>{i}.</b> <code>{stamp}</code>
↩️ <b>You:</b> {body}")

    full_text = header + ("

".join(lines) if lines else "<i>Koi message history nahi hai.</i>")
    if len(full_text) > 3900:
        full_text = full_text[-3900:]

    ban_btn = (
        InlineKeyboardButton("✅ Unban", callback_data=f"unban_{user_id}")
        if u["is_banned"]
        else InlineKeyboardButton("🚫 Ban", callback_data=f"ban_{user_id}")
    )
    star_btn = (
        InlineKeyboardButton("☆ Unstar", callback_data=f"unstar_{user_id}")
        if u["is_starred"]
        else InlineKeyboardButton("⭐ Star", callback_data=f"star_{user_id}")
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💬 Reply User", callback_data=f"reply_{user_id}"),
                star_btn,
                ban_btn,
            ],
            [
                InlineKeyboardButton("⚡ Quick Template", callback_data=f"tplpick_{user_id}"),
                InlineKeyboardButton("⬅️ Back to List", callback_data=f"list_{back_page}"),
            ],
        ]
    )
    return full_text, kb


# ================= LOG CHANNEL MIRROR =================
async def send_log_mirror(context: ContextTypes.DEFAULT_TYPE, text: str):
    if get_setting("log_enabled") != "1":
        return
    ch = get_setting("log_channel").strip()
    if not ch:
        return
    try:
        await context.bot.send_message(chat_id=int(ch), text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"Log channel mirror failed: {e}")


# ================= DELIVER OWNER REPLY TO USER =================
async def deliver_reply_to_user(
    context: ContextTypes.DEFAULT_TYPE,
    target_id: int,
    msg=None,
    quick_text: str = "",
):
    date_str, time_str, _ = now_ist()
    header = build_owner_reply_header(date_str, time_str)

    if quick_text:
        sent = await context.bot.send_message(
            chat_id=target_id,
            text=f"{header}💬 {esc(quick_text)}",
            parse_mode=ParseMode.HTML,
        )
        save_message(target_id, "out", "text", quick_text)
        await send_log_mirror(
            context,
            f"📤 <b>Reply to</b> <code>{target_id}</code> ({date_str} • {time_str}):
{esc(quick_text)}",
        )
        return sent

    if msg.text:
        sent = await context.bot.send_message(
            chat_id=target_id,
            text=f"{header}💬 {esc(msg.text)}",
            parse_mode=ParseMode.HTML,
        )
        save_message(target_id, "out", "text", msg.text)
        await send_log_mirror(
            context,
            f"📤 <b>Reply to</b> <code>{target_id}</code> ({date_str} • {time_str}):
{esc(msg.text)}",
        )
        return sent

    if msg.photo:
        cap = f"{header}💬 {esc(msg.caption)}" if msg.caption else header.strip()
        sent = await context.bot.send_photo(
            chat_id=target_id,
            photo=msg.photo[-1].file_id,
            caption=cap,
            parse_mode=ParseMode.HTML,
        )
        save_message(target_id, "out", "photo", msg.caption or "[Photo]", msg.photo[-1].file_id)
        return sent

    if msg.video:
        cap = f"{header}💬 {esc(msg.caption)}" if msg.caption else header.strip()
        sent = await context.bot.send_video(
            chat_id=target_id,
            video=msg.video.file_id,
            caption=cap,
            parse_mode=ParseMode.HTML,
        )
        save_message(target_id, "out", "video", msg.caption or "[Video]", msg.video.file_id)
        return sent

    if msg.document:
        cap = f"{header}💬 {esc(msg.caption)}" if msg.caption else header.strip()
        sent = await context.bot.send_document(
            chat_id=target_id,
            document=msg.document.file_id,
            caption=cap,
            parse_mode=ParseMode.HTML,
        )
        save_message(target_id, "out", "document", msg.caption or "[Document]", msg.document.file_id)
        return sent

    if msg.voice:
        sent = await context.bot.send_voice(
            chat_id=target_id,
            voice=msg.voice.file_id,
            caption=header.strip(),
            parse_mode=ParseMode.HTML,
        )
        save_message(target_id, "out", "voice", "[Voice]", msg.voice.file_id)
        return sent

    if msg.audio:
        cap = f"{header}💬 {esc(msg.caption)}" if msg.caption else header.strip()
        sent = await context.bot.send_audio(
            chat_id=target_id,
            audio=msg.audio.file_id,
            caption=cap,
            parse_mode=ParseMode.HTML,
        )
        save_message(target_id, "out", "audio", msg.caption or "[Audio]", msg.audio.file_id)
        return sent

    if msg.sticker:
        await context.bot.send_message(chat_id=target_id, text=header.strip(), parse_mode=ParseMode.HTML)
        sent = await context.bot.send_sticker(chat_id=target_id, sticker=msg.sticker.file_id)
        save_message(target_id, "out", "sticker", "[Sticker]", msg.sticker.file_id)
        return sent

    await context.bot.send_message(chat_id=target_id, text=header.strip(), parse_mode=ParseMode.HTML)
    sent = await context.bot.copy_message(
        chat_id=target_id, from_chat_id=msg.chat_id, message_id=msg.message_id
    )
    save_message(target_id, "out", "media", "[Media]")
    return sent


# ================= COMMAND HANDLERS =================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    if user.id == OWNER_ID:
        text, kb = build_settings_panel()
        await update.effective_message.reply_text(
            "👋 <b>Welcome back, Mr. Sanskari!</b>
Niche aapka Owner Control Panel hai:

" + text,
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
        return

    upsert_user(user)
    u_row = get_user(user.id)
    if u_row and u_row["is_banned"]:
        await update.effective_message.reply_text("🚫 Aapko owner dwara block kiya gaya hai.")
        return

    context.user_data["compose_ready"] = False
    await update.effective_message.reply_text(
        build_user_welcome_text(),
        reply_markup=user_welcome_kb(),
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    if user.id == OWNER_ID:
        await update.effective_message.reply_text(
            "⚙️ <b>Owner Commands:</b>
"
            "• <code>/setting</code> — Full Owner Control Panel
"
            "• <code>/list</code> — 10-per-page User Directory
"
            "• <code>/pending</code> — Unanswered Users
"
            "• <code>/user &lt;id&gt;</code> — User History &amp; Profile
"
            "• <code>/reply &lt;id&gt; &lt;text&gt;</code> — Direct Reply
"
            "• <code>/ban &lt;id&gt; [reason]</code> &amp; <code>/unban &lt;id&gt;</code>
"
            "• <code>/star &lt;id&gt;</code> — Toggle VIP Star
"
            "• <code>/broadcast &lt;text&gt;</code> — Message All Users
"
            "• <code>/stats</code> &amp; <code>/ping</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.effective_message.reply_text(
        "ℹ️ अपना संदेश भेजने के लिए /start दबाएँ और अपना मैसेज लिखें। Owner जल्दी ही रिप्लाई देगा।

"
        "[Press /start and type your message to contact the owner. You will receive a reply here soon.]"
    )


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user:
        await update.effective_message.reply_text(
            f"🆔 Your Numerical ID: <code>{user.id}</code>", parse_mode=ParseMode.HTML
        )


async def cmd_setting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    text, kb = build_settings_panel()
    await update.effective_message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    text, kb = build_user_list_page(page=1, only_pending=False)
    await update.effective_message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    text, kb = build_user_list_page(page=1, only_pending=True)
    await update.effective_message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def cmd_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: <code>/user 123456789</code>", parse_mode=ParseMode.HTML)
        return
    uid = int(context.args[0])
    text, kb = build_user_history_view(uid, back_page=1)
    await update.effective_message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def cmd_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.effective_message.reply_text(
            "Usage: <code>/reply 123456789 Aapka reply</code>", parse_mode=ParseMode.HTML
        )
        return
    target_id = int(context.args[0])
    reply_txt = " ".join(context.args[1:])
    try:
        sent = await deliver_reply_to_user(context, target_id, quick_text=reply_txt)
        del_kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🗑️ Delete Reply", callback_data=f"delmsg_{target_id}_{sent.message_id}")]]
        )
        await update.effective_message.reply_text(
            f"✅ Delivered to <code>{target_id}</code>", reply_markup=del_kb, parse_mode=ParseMode.HTML
        )
    except Exception as e:
        await update.effective_message.reply_text(
            f"⚠️ Delivery failed (user ne bot block kiya ho sakta hai): <code>{esc(e)}</code>",
            parse_mode=ParseMode.HTML,
        )


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text(
            "Usage: <code>/ban 123456789 [reason]</code>", parse_mode=ParseMode.HTML
        )
        return
    uid = int(context.args[0])
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "Spam / Policy"
    conn = get_db()
    conn.execute("UPDATE users SET is_banned = 1, ban_reason = ? WHERE user_id = ?", (reason, uid))
    conn.commit()
    conn.close()
    await update.effective_message.reply_text(
        f"🚫 User <code>{uid}</code> banned. Reason: <i>{esc(reason)}</i>", parse_mode=ParseMode.HTML
    )


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: <code>/unban 123456789</code>", parse_mode=ParseMode.HTML)
        return
    uid = int(context.args[0])
    conn = get_db()
    conn.execute("UPDATE users SET is_banned = 0, ban_reason = '' WHERE user_id = ?", (uid,))
    conn.commit()
    conn.close()
    await update.effective_message.reply_text(f"✅ User <code>{uid}</code> unbanned.", parse_mode=ParseMode.HTML)


async def cmd_star(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: <code>/star 123456789</code>", parse_mode=ParseMode.HTML)
        return
    uid = int(context.args[0])
    u = get_user(uid)
    if not u:
        await update.effective_message.reply_text("User nahi mila.")
        return
    new_val = 0 if u["is_starred"] else 1
    conn = get_db()
    conn.execute("UPDATE users SET is_starred = ? WHERE user_id = ?", (new_val, uid))
    conn.commit()
    conn.close()
    await update.effective_message.reply_text(
        f"{'⭐ Added to VIP' if new_val else '☆ Removed from VIP'}: <code>{uid}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: <code>/broadcast Sabhi ke liye zaroori suchna...</code>", parse_mode=ParseMode.HTML
        )
        return
    b_text = " ".join(context.args)
    await run_broadcast_text(update.effective_message, context, b_text)


async def run_broadcast_text(target_msg, context: ContextTypes.DEFAULT_TYPE, b_text: str):
    conn = get_db()
    users = conn.execute("SELECT user_id FROM users WHERE is_banned = 0").fetchall()
    conn.close()
    date_str, time_str, _ = now_ist()
    card = (
        "╭━━━ 📢 𝐀𝐍𝐍𝐎𝐔𝐍𝐂𝐄𝐌𝐄𝐍𝐓 • 𝐌𝐑. 𝐒𝐀𝐍𝐒𝐊𝐀𝐑𝐈 ━━━╮
"
        f"┃ 📅 {date_str}   |   🕒 {time_str}
"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯
"
        f"💬 {esc(b_text)}"
    )
    ok_cnt, fail_cnt = 0, 0
    for r in users:
        try:
            await context.bot.send_message(chat_id=r["user_id"], text=card, parse_mode=ParseMode.HTML)
            ok_cnt += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail_cnt += 1
    await target_msg.reply_text(
        f"📢 <b>Broadcast Complete</b>
✅ Sent: <b>{ok_cnt}</b>
⚠️ Failed/Blocked: <b>{fail_cnt}</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    await send_stats_card(update.effective_message, edit=False)


async def send_stats_card(msg_obj, edit: bool = False):
    date_str, time_str, _ = now_ist()
    conn = get_db()
    total_users = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
    banned_users = conn.execute("SELECT COUNT(*) as c FROM users WHERE is_banned = 1").fetchone()["c"]
    starred_users = conn.execute("SELECT COUNT(*) as c FROM users WHERE is_starred = 1").fetchone()["c"]
    pending_users = conn.execute("SELECT COUNT(*) as c FROM users WHERE pending_reply = 1 AND is_banned = 0").fetchone()["c"]
    total_in = conn.execute("SELECT COUNT(*) as c FROM messages WHERE direction = 'in'").fetchone()["c"]
    total_out = conn.execute("SELECT COUNT(*) as c FROM messages WHERE direction = 'out'").fetchone()["c"]
    today_in = conn.execute(
        "SELECT COUNT(*) as c FROM messages WHERE direction = 'in' AND date_str = ?", (date_str,)
    ).fetchone()["c"]
    conn.close()

    text = (
        "╭━━━ 📊 𝐒𝐀𝐍𝐒𝐊𝐀𝐑𝐈 𝐃𝐌 • 𝐁𝐎𝐓 𝐒𝐓𝐀𝐓𝐒 ━━━╮
"
        f"┃ 👥 𝐓𝐨𝐭𝐚𝐥 𝐔𝐬𝐞𝐫𝐬: {total_users}
"
        f"┃ 📩 𝐓𝐨𝐭𝐚𝐥 𝐃𝐌𝐬: {total_in}  (Today: {today_in})
"
        f"┃ 📤 𝐓𝐨𝐭𝐚𝐥 𝐑𝐞𝐩𝐥𝐢𝐞𝐬: {total_out}
"
        f"┃ ⏳ 𝐏𝐞𝐧𝐝𝐢𝐧𝐠 𝐑𝐞𝐩𝐥𝐢𝐞𝐬: {pending_users}
"
        f"┃ ⭐ 𝐒𝐭𝐚𝐫𝐫𝐞𝐝 𝐕𝐈𝐏: {starred_users}  |  🚫 𝐁𝐚𝐧𝐧𝐞𝐝: {banned_users}
"
        f"┃ 🕒 𝐔𝐩𝐝𝐚𝐭𝐞𝐝: {date_str} • {time_str}
"
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ Back to Settings", callback_data="open_settings")]])
    if edit:
        await msg_obj.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await msg_obj.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    t0 = time.time()
    m = await update.effective_message.reply_text("🏓 Checking...")
    ms = int((time.time() - t0) * 1000)
    uptime_s = int(time.time() - START_TIME)
    mins, secs = divmod(uptime_s, 60)
    hrs, mins = divmod(mins, 60)
    await m.edit_text(
        f"🏓 <b>Pong!</b>
⚡ Latency: <code>{ms} ms</code>
⏱️ Uptime: <code>{hrs}h {mins}m {secs}s</code>
💾 DB: <code>{DB_PATH}</code>",
        parse_mode=ParseMode.HTML,
    )


# ================= CALLBACK BUTTON HANDLER =================
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    data = query.data or ""

    # User compose button
    if data == "user_compose":
        context.user_data["compose_ready"] = True
        await query.answer()
        await query.edit_message_text(
            build_user_welcome_text(),
            reply_markup=None,
            parse_mode=ParseMode.HTML,
        )
        await query.message.reply_text("✍️ यहाँ अपना संदेश लिखें")
        return

    # Strictly Owner-Only below this line
    if user.id != OWNER_ID:
        await query.answer("⛔ Owner only!", show_alert=True)
        return

    await query.answer()

    if data == "close_panel":
        await query.message.delete()
        return

    if data == "open_settings":
        context.user_data.pop("owner_state", None)
        text, kb = build_settings_panel()
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if data.startswith("noop_"):
        parts = data.split("_")
        await query.answer(f"Page {parts[1]} of {parts[2]}", show_alert=False)
        return

    if data.startswith("list_"):
        page = int(data.split("_")[1])
        text, kb = build_user_list_page(page=page, only_pending=False)
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if data.startswith("pending_"):
        page = int(data.split("_")[1])
        text, kb = build_user_list_page(page=page, only_pending=True)
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if data.startswith("hist_"):
        parts = data.split("_")
        uid = int(parts[1])
        back_p = int(parts[2]) if len(parts) > 2 else 1
        text, kb = build_user_history_view(uid, back_page=back_p)
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if data.startswith("reply_"):
        uid = int(data.split("_")[1])
        u = get_user(uid)
        uname = esc(u["full_name"]) if u else str(uid)
        context.user_data["owner_state"] = {"mode": "reply", "target_id": uid}
        cancel_kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("⚡ Use Quick Template", callback_data=f"tplpick_{uid}"),
                    InlineKeyboardButton("❌ Cancel Reply", callback_data="cancel_state"),
                ]
            ]
        )
        await query.message.reply_text(
            f"✍️ <b>Reply Mode Active</b>
User: <b>{uname}</b> (<code>{uid}</code>)

"
            "Ab apna text, photo, video, document, voice ya sticker bhejein:",
            reply_markup=cancel_kb,
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "cancel_state":
        context.user_data.pop("owner_state", None)
        await query.edit_message_text("❌ Action cancelled.")
        return

    if data.startswith("delmsg_"):
        parts = data.split("_")
        uid = int(parts[1])
        mid = int(parts[2])
        try:
            await context.bot.delete_message(chat_id=uid, message_id=mid)
            await query.edit_message_text(f"🗑️ Reply deleted from user <code>{uid}</code>.", parse_mode=ParseMode.HTML)
        except Exception as e:
            await query.edit_message_text(f"⚠️ Delete nahi ho paaya: <code>{esc(e)}</code>", parse_mode=ParseMode.HTML)
        return

    if data.startswith("ban_"):
        uid = int(data.split("_")[1])
        context.user_data["owner_state"] = {"mode": "ban_reason", "target_id": uid}
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("⚡ Instant Ban (No Reason)", callback_data=f"bannow_{uid}"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel_state"),
                ]
            ]
        )
        await query.message.reply_text(
            f"🚫 User <code>{uid}</code> ko ban karne ke liye reason likhein ya Instant Ban dabayein:",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("bannow_"):
        uid = int(data.split("_")[1])
        conn = get_db()
        conn.execute("UPDATE users SET is_banned = 1, ban_reason = 'Banned by Owner' WHERE user_id = ?", (uid,))
        conn.commit()
        conn.close()
        context.user_data.pop("owner_state", None)
        await query.edit_message_text(f"🚫 User <code>{uid}</code> banned.", parse_mode=ParseMode.HTML)
        return

    if data.startswith("unban_"):
        uid = int(data.split("_")[1])
        conn = get_db()
        conn.execute("UPDATE users SET is_banned = 0, ban_reason = '' WHERE user_id = ?", (uid,))
        conn.commit()
        conn.close()
        await query.message.reply_text(f"✅ User <code>{uid}</code> unbanned.", parse_mode=ParseMode.HTML)
        return

    if data.startswith("star_"):
        uid = int(data.split("_")[1])
        conn = get_db()
        conn.execute("UPDATE users SET is_starred = 1 WHERE user_id = ?", (uid,))
        conn.commit()
        conn.close()
        text, kb = build_user_history_view(uid)
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if data.startswith("unstar_"):
        uid = int(data.split("_")[1])
        conn = get_db()
        conn.execute("UPDATE users SET is_starred = 0 WHERE user_id = ?", (uid,))
        conn.commit()
        conn.close()
        text, kb = build_user_history_view(uid)
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if data == "set_stats":
        await send_stats_card(query.message, edit=True)
        return

    if data == "set_broadcast":
        context.user_data["owner_state"] = {"mode": "broadcast"}
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Broadcast", callback_data="cancel_state")]])
        await query.message.reply_text(
            "📢 <b>Broadcast Mode</b>
Sabhi active users ko bhejne ke liye apna message likh kar send karein:",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "set_away":
        is_on = get_setting("away_mode") == "1"
        away_msg = esc(get_setting("away_msg"))
        text = (
            "╭━━━ 💤 𝐀𝐖𝐀𝐘 𝐌𝐎𝐃𝐄 𝐒𝐄𝐓𝐓𝐈𝐍𝐆𝐒 ━━━╮
"
            f"┃ 𝐒𝐭𝐚𝐭𝐮𝐬: {'🟢 ON' if is_on else '🔴 OFF'}
"
            "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯
"
            f"💬 <b>Auto Message:</b>
{away_msg}"
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🟢 Turn ON", callback_data="away_on"),
                    InlineKeyboardButton("🔴 Turn OFF", callback_data="away_off"),
                ],
                [InlineKeyboardButton("✏️ Edit Away Message", callback_data="away_edit")],
                [InlineKeyboardButton("⚙️ Back to Settings", callback_data="open_settings")],
            ]
        )
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if data in ("away_on", "away_off"):
        set_setting("away_mode", "1" if data == "away_on" else "0")
        text, kb = build_settings_panel()
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if data == "away_edit":
        context.user_data["owner_state"] = {"mode": "edit_away"}
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_state")]])
        await query.message.reply_text("✏️ Naya Away auto-reply message likh kar bhejein:", reply_markup=kb)
        return

    if data == "set_maint":
        is_on = get_setting("maint_mode") == "1"
        maint_msg = esc(get_setting("maint_msg"))
        text = (
            "╭━━━ ⏸️ 𝐌𝐀𝐈𝐍𝐓𝐄𝐍𝐀𝐍𝐂𝐄 𝐌𝐎𝐃𝐄 ━━━╮
"
            f"┃ 𝐒𝐭𝐚𝐭𝐮𝐬: {'⏸️ PAUSED (ON)' if is_on else '🟢 ACTIVE (OFF)'}
"
            "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯
"
            f"💬 <b>Notice:</b>
{maint_msg}"
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("⏸️ Pause Bot", callback_data="maint_on"),
                    InlineKeyboardButton("🟢 Resume Bot", callback_data="maint_off"),
                ],
                [InlineKeyboardButton("⚙️ Back to Settings", callback_data="open_settings")],
            ]
        )
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if data in ("maint_on", "maint_off"):
        set_setting("maint_mode", "1" if data == "maint_on" else "0")
        text, kb = build_settings_panel()
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if data == "set_banned":
        conn = get_db()
        rows = conn.execute("SELECT * FROM users WHERE is_banned = 1").fetchall()
        conn.close()
        text = "╭━━━ 🚫 𝐁𝐀𝐍𝐍𝐄𝐃 𝐔𝐒𝐄𝐑𝐒 ━━━╮
"
        btns = []
        if not rows:
            text += "┃ Koi user banned nahi hai.
"
        else:
            for r in rows[:20]:
                text += f"┃ • {esc(r['full_name'])} (<code>{r['user_id']}</code>) — {esc(r['ban_reason'])}
"
                btns.append([InlineKeyboardButton(f"✅ Unban {r['full_name'][:18]}", callback_data=f"unban_{r['user_id']}")])
        text += "╰━━━━━━━━━━━━━━━━━━━━━━━━╯"
        btns.append([InlineKeyboardButton("⚙️ Back to Settings", callback_data="open_settings")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(btns), parse_mode=ParseMode.HTML)
        return

    if data == "set_starred":
        conn = get_db()
        rows = conn.execute("SELECT * FROM users WHERE is_starred = 1").fetchall()
        conn.close()
        text = "╭━━━ ⭐ 𝐒𝐓𝐀𝐑𝐑𝐄𝐃 𝐕𝐈𝐏 𝐔𝐒𝐄𝐑𝐒 ━━━╮
"
        btns = []
        if not rows:
            text += "┃ Abhi koi starred user nahi hai.
"
        else:
            for r in rows[:20]:
                btns.append([InlineKeyboardButton(f"⭐ {r['full_name'][:20]} • {r['user_id']}", callback_data=f"hist_{r['user_id']}_1")])
        text += "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯"
        btns.append([InlineKeyboardButton("⚙️ Back to Settings", callback_data="open_settings")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(btns), parse_mode=ParseMode.HTML)
        return

    if data == "set_tpl":
        conn = get_db()
        tpls = conn.execute("SELECT * FROM templates").fetchall()
        conn.close()
        text = "╭━━━ ⚡ 𝐐𝐔𝐈𝐂𝐊 𝐑𝐄𝐏𝐋𝐘 𝐓𝐄𝐌𝐏𝐋𝐀𝐓𝐄𝐒 ━━━╮
"
        btns = []
        for t in tpls:
            text += f"┃ • <b>{esc(t['title'])}:</b> {esc(t['body'][:45])}
"
            btns.append([InlineKeyboardButton(f"🗑️ Delete: {t['title']}", callback_data=f"tpldel_{t['id']}")])
        text += "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯"
        btns.append([InlineKeyboardButton("➕ Add New Template", callback_data="tpl_add")])
        btns.append([InlineKeyboardButton("⚙️ Back to Settings", callback_data="open_settings")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(btns), parse_mode=ParseMode.HTML)
        return

    if data == "tpl_add":
        context.user_data["owner_state"] = {"mode": "add_tpl"}
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_state")]])
        await query.message.reply_text(
            "➕ Naya template is format mein bhejein:
<code>Title | Reply message text</code>",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("tpldel_"):
        tid = int(data.split("_")[1])
        conn = get_db()
        conn.execute("DELETE FROM templates WHERE id = ?", (tid,))
        conn.commit()
        conn.close()
        await query.answer("Template deleted")
        text, kb = build_settings_panel()
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if data.startswith("tplpick_"):
        uid = int(data.split("_")[1])
        conn = get_db()
        tpls = conn.execute("SELECT * FROM templates").fetchall()
        conn.close()
        btns = [
            [InlineKeyboardButton(f"⚡ {t['title']}", callback_data=f"tplsend_{uid}_{t['id']}")]
            for t in tpls
        ]
        btns.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_state")])
        await query.message.reply_text(
            f"User <code>{uid}</code> ko bhejne ke liye template chunein:",
            reply_markup=InlineKeyboardMarkup(btns),
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("tplsend_"):
        parts = data.split("_")
        uid = int(parts[1])
        tid = int(parts[2])
        conn = get_db()
        row = conn.execute("SELECT body FROM templates WHERE id = ?", (tid,)).fetchone()
        conn.close()
        if row:
            try:
                sent = await deliver_reply_to_user(context, uid, quick_text=row["body"])
                context.user_data.pop("owner_state", None)
                del_kb = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🗑️ Delete Reply", callback_data=f"delmsg_{uid}_{sent.message_id}")]]
                )
                await query.edit_message_text(
                    f"✅ Quick Template sent to <code>{uid}</code>!",
                    reply_markup=del_kb,
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                await query.edit_message_text(f"⚠️ Failed: <code>{esc(e)}</code>", parse_mode=ParseMode.HTML)
        return

    if data == "set_welcome":
        context.user_data["owner_state"] = {"mode": "edit_welcome"}
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_state")]])
        await query.message.reply_text(
            "✏️ Naya Welcome message is format mein bhejein (pehli line Hindi, doosri line English):

"
            "<code>Hindi line yahan
English line yahan</code>",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "set_log":
        is_on = get_setting("log_enabled") == "1"
        ch = get_setting("log_channel") or "Not Set"
        text = (
            "╭━━━ 📡 𝐏𝐑𝐈𝐕𝐀𝐓𝐄 𝐋𝐎𝐆 𝐂𝐇𝐀𝐍𝐍𝐄𝐋 ━━━╮
"
            f"┃ 𝐒𝐭𝐚𝐭𝐮𝐬: {'🟢 ON' if is_on else '🔴 OFF'}
"
            f"┃ 𝐂𝐡𝐚𝐧𝐧𝐞𝐥 𝐈𝐃: <code>{esc(ch)}</code>
"
            "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯"
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🟢 Enable", callback_data="log_on"),
                    InlineKeyboardButton("🔴 Disable", callback_data="log_off"),
                ],
                [InlineKeyboardButton("🆔 Set Channel ID", callback_data="log_setid")],
                [InlineKeyboardButton("⚙️ Back to Settings", callback_data="open_settings")],
            ]
        )
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if data in ("log_on", "log_off"):
        set_setting("log_enabled", "1" if data == "log_on" else "0")
        text, kb = build_settings_panel()
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if data == "log_setid":
        context.user_data["owner_state"] = {"mode": "set_log_id"}
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_state")]])
        await query.message.reply_text(
            "📡 Private Log Channel ID bhejein (jaise <code>-1001234567890</code>). Bot ko channel mein admin banayein:",
            reply_markup=kb,
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "set_backup":
        uptime_s = int(time.time() - START_TIME)
        hrs, rem = divmod(uptime_s, 3600)
        mins, secs = divmod(rem, 60)
        text = (
            "╭━━━ 🗂️ 𝐁𝐀𝐂𝐊𝐔𝐏 & 𝐇𝐄𝐀𝐋𝐓𝐇 ━━━╮
"
            f"┃ ⏱️ 𝐔𝐩𝐭𝐢𝐦𝐞: {hrs}h {mins}m {secs}s
"
            f"┃ 💾 𝐃𝐁 𝐅𝐢𝐥𝐞: <code>{DB_PATH}</code>
"
            "╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯"
        )
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("💾 Download DB Backup", callback_data="dl_db")],
                [InlineKeyboardButton("⚙️ Back to Settings", callback_data="open_settings")],
            ]
        )
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if data == "dl_db":
        if os.path.exists(DB_PATH):
            with open(DB_PATH, "rb") as f:
                await query.message.reply_document(
                    document=f,
                    filename=f"sanskari_dm_backup_{int(time.time())}.db",
                    caption="💾 SQLite Database Backup",
                )
        else:
            await query.message.reply_text("⚠️ Database file abhi empty hai.")
        return


# ================= MAIN MESSAGE HANDLER =================
async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    # -------- CASE 1: OWNER MESSAGES --------
    if user.id == OWNER_ID:
        state = context.user_data.get("owner_state")
        if state:
            mode = state.get("mode")
            if mode == "reply":
                target_id = state["target_id"]
                try:
                    sent = await deliver_reply_to_user(context, target_id, msg=msg)
                    context.user_data.pop("owner_state", None)
                    del_kb = InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🗑️ Delete Reply", callback_data=f"delmsg_{target_id}_{sent.message_id}")]]
                    )
                    await msg.reply_text(
                        f"✅ Reply delivered to <code>{target_id}</code>.",
                        reply_markup=del_kb,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as e:
                    await msg.reply_text(
                        f"⚠️ Send failed (user ne bot block kiya ho sakta hai): <code>{esc(e)}</code>",
                        parse_mode=ParseMode.HTML,
                    )
                return

            if mode == "ban_reason":
                target_id = state["target_id"]
                reason = msg.text or "Banned by Owner"
                conn = get_db()
                conn.execute("UPDATE users SET is_banned = 1, ban_reason = ? WHERE user_id = ?", (reason, target_id))
                conn.commit()
                conn.close()
                context.user_data.pop("owner_state", None)
                await msg.reply_text(
                    f"🚫 User <code>{target_id}</code> banned.
Reason: <i>{esc(reason)}</i>",
                    parse_mode=ParseMode.HTML,
                )
                return

            if mode == "broadcast" and msg.text:
                context.user_data.pop("owner_state", None)
                await run_broadcast_text(msg, context, msg.text)
                return

            if mode == "edit_away" and msg.text:
                set_setting("away_msg", msg.text)
                context.user_data.pop("owner_state", None)
                await msg.reply_text("✅ Away auto-reply message updated!")
                return

            if mode == "add_tpl" and msg.text and "|" in msg.text:
                title, body = [x.strip() for x in msg.text.split("|", 1)]
                conn = get_db()
                conn.execute("INSERT INTO templates (title, body) VALUES (?, ?)", (title, body))
                conn.commit()
                conn.close()
                context.user_data.pop("owner_state", None)
                await msg.reply_text(f"✅ Template <b>{esc(title)}</b> saved!", parse_mode=ParseMode.HTML)
                return

            if mode == "edit_welcome" and msg.text:
                lines = [x.strip() for x in msg.text.splitlines() if x.strip()]
                if len(lines) >= 2:
                    set_setting("welcome_hi", lines[0])
                    set_setting("welcome_en", lines[1])
                    context.user_data.pop("owner_state", None)
                    await msg.reply_text("✅ Welcome message (Hindi + English) updated!")
                else:
                    await msg.reply_text("⚠️ 2 lines bhejein: pehli Hindi, doosri English.")
                return

            if mode == "set_log_id" and msg.text:
                set_setting("log_channel", msg.text.strip())
                set_setting("log_enabled", "1")
                context.user_data.pop("owner_state", None)
                await msg.reply_text(f"✅ Log channel set to <code>{esc(msg.text.strip())}</code>", parse_mode=ParseMode.HTML)
                return

        await msg.reply_text(
            "ℹ️ Kisi user ko reply karne ke liye uske DM card ke neeche <b>💬 Reply</b> button dabayein, "
            "ya <code>/setting</code> kholein.",
            parse_mode=ParseMode.HTML,
        )
        return

    # -------- CASE 2: REGULAR USER DMs --------
    upsert_user(user)
    u_row = get_user(user.id)
    if u_row and u_row["is_banned"]:
        return

    if get_setting("maint_mode") == "1":
        await msg.reply_text(f"⏸️ {get_setting('maint_msg')}")
        return

    # Determine content & save in DB
    if msg.text:
        dm_no, date_str, time_str = save_message(user.id, "in", "text", msg.text)
        u_row = get_user(user.id)
        card = build_dm_card(dm_no, u_row, date_str, time_str, "Message", msg.text)
        kb = dm_card_kb(user.id, u_row["is_banned"])
        await context.bot.send_message(chat_id=OWNER_ID, text=card, reply_markup=kb, parse_mode=ParseMode.HTML)
        await send_log_mirror(context, card)
    elif msg.photo:
        dm_no, date_str, time_str = save_message(
            user.id, "in", "photo", msg.caption or "[Photo]", msg.photo[-1].file_id
        )
        u_row = get_user(user.id)
        card = build_dm_card(dm_no, u_row, date_str, time_str, "Photo", msg.caption or "")
        kb = dm_card_kb(user.id, u_row["is_banned"])
        await context.bot.send_photo(
            chat_id=OWNER_ID, photo=msg.photo[-1].file_id, caption=card, reply_markup=kb, parse_mode=ParseMode.HTML
        )
        await send_log_mirror(context, card)
    elif msg.video:
        dm_no, date_str, time_str = save_message(
            user.id, "in", "video", msg.caption or "[Video]", msg.video.file_id
        )
        u_row = get_user(user.id)
        card = build_dm_card(dm_no, u_row, date_str, time_str, "Video", msg.caption or "")
        kb = dm_card_kb(user.id, u_row["is_banned"])
        await context.bot.send_video(
            chat_id=OWNER_ID, video=msg.video.file_id, caption=card, reply_markup=kb, parse_mode=ParseMode.HTML
        )
        await send_log_mirror(context, card)
    elif msg.document:
        dm_no, date_str, time_str = save_message(
            user.id, "in", "document", msg.caption or "[Document]", msg.document.file_id
        )
        u_row = get_user(user.id)
        card = build_dm_card(dm_no, u_row, date_str, time_str, "Document", msg.caption or "")
        kb = dm_card_kb(user.id, u_row["is_banned"])
        await context.bot.send_document(
            chat_id=OWNER_ID, document=msg.document.file_id, caption=card, reply_markup=kb, parse_mode=ParseMode.HTML
        )
        await send_log_mirror(context, card)
    elif msg.voice:
        dm_no, date_str, time_str = save_message(user.id, "in", "voice", "[Voice]", msg.voice.file_id)
        u_row = get_user(user.id)
        card = build_dm_card(dm_no, u_row, date_str, time_str, "Voice Message", "")
        kb = dm_card_kb(user.id, u_row["is_banned"])
        await context.bot.send_voice(
            chat_id=OWNER_ID, voice=msg.voice.file_id, caption=card, reply_markup=kb, parse_mode=ParseMode.HTML
        )
        await send_log_mirror(context, card)
    elif msg.audio:
        dm_no, date_str, time_str = save_message(
            user.id, "in", "audio", msg.caption or "[Audio]", msg.audio.file_id
        )
        u_row = get_user(user.id)
        card = build_dm_card(dm_no, u_row, date_str, time_str, "Audio", msg.caption or "")
        kb = dm_card_kb(user.id, u_row["is_banned"])
        await context.bot.send_audio(
            chat_id=OWNER_ID, audio=msg.audio.file_id, caption=card, reply_markup=kb, parse_mode=ParseMode.HTML
        )
        await send_log_mirror(context, card)
    elif msg.sticker:
        dm_no, date_str, time_str = save_message(user.id, "in", "sticker", "[Sticker]", msg.sticker.file_id)
        u_row = get_user(user.id)
        card = build_dm_card(dm_no, u_row, date_str, time_str, "Sticker", "")
        kb = dm_card_kb(user.id, u_row["is_banned"])
        await context.bot.send_sticker(chat_id=OWNER_ID, sticker=msg.sticker.file_id)
        await context.bot.send_message(chat_id=OWNER_ID, text=card, reply_markup=kb, parse_mode=ParseMode.HTML)
        await send_log_mirror(context, card)
    else:
        dm_no, date_str, time_str = save_message(user.id, "in", "media", "[Media]")
        u_row = get_user(user.id)
        card = build_dm_card(dm_no, u_row, date_str, time_str, "Media", "")
        kb = dm_card_kb(user.id, u_row["is_banned"])
        await context.bot.copy_message(chat_id=OWNER_ID, from_chat_id=msg.chat_id, message_id=msg.message_id)
        await context.bot.send_message(chat_id=OWNER_ID, text=card, reply_markup=kb, parse_mode=ParseMode.HTML)
        await send_log_mirror(context, card)

    # Exact User Confirmation (Hindi + [English])
    confirm_text = (
        "यह मैसेज owner के पास चला गया है, owner जल्दी ही इसका रिप्लाई देगा।

"
        "[This message has been sent to the owner, the owner will reply to it soon.]"
    )
    if get_setting("away_mode") == "1":
        confirm_text += f"

💤 {get_setting('away_msg')}"

    await msg.reply_text(confirm_text)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing!")

    init_db()

    # Start Flask server in background thread for Render health checks
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()

    # Ensure clean event loop on Python 3.12 / 3.14
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("setting", cmd_setting))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("user", cmd_user))
    app.add_handler(CommandHandler("reply", cmd_reply))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("star", cmd_star))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_private_message))

    logger.info("Sanskari DM Bot started successfully...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
