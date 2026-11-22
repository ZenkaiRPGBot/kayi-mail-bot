import os
import sys
import time
import math
import random
import html
import asyncio
from datetime import datetime, timezone
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, CopyTextButton,
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat
)
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest, TelegramNetworkError

from config import (
    BOT_TOKEN, OWNER_ID, LOG_CHANNEL_ID, BOT_START_TIME,
    BRAND_NAME, COMMUNITY_URL, DEVELOPER_URL, INBOX_PAGE_SIZE,
    WELCOME_VIDEO_FILE_ID,
    INTERVAL_NEW_BOX, INTERVAL_VERIFY_WINDOW, INTERVAL_ACTIVE,
    INTERVAL_IDLE_15M, INTERVAL_IDLE_30M,
    RENDER_EMAIL, RENDER_SERVICE_NAME, RENDER_REPO,
    RENDER_USED_HOURS, IST
)
import database as db
from mail_service import MailService, extract_verification_code, DEFAULT_DOMAIN, PROVIDERS
from keep_alive import start_web_server

dp = Dispatcher()
HTTP_SESSION: aiohttp.ClientSession | None = None
mail_service: MailService | None = None

def is_owner(user_id: int | None) -> bool:
    """Strict fail-closed owner authorization verification."""
    return bool(OWNER_ID and user_id and user_id == OWNER_ID)

def mask_email(email_str: str) -> str:
    """Masks email address to protect privacy (e.g. 230***@domain.com)."""
    if not email_str or "@" not in email_str:
        return "[configured]"
    parts = email_str.split("@", 1)
    user_part = parts[0]
    domain_part = parts[1]
    if len(user_part) <= 3:
        masked_user = user_part[0] + "***"
    else:
        masked_user = user_part[:3] + "***"
    return f"{masked_user}@{domain_part}"

def format_relative_time(iso_str: str) -> str:
    """Formats ISO datetime string into readable relative or IST clock time."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        diff = (now - dt).total_seconds()
        if diff < 60:
            return "just now"
        elif diff < 3600:
            return f"{int(diff // 60)}m ago"
        elif diff < 86400:
            return dt.astimezone(IST).strftime("%I:%M %p IST")
        return dt.astimezone(IST).strftime("%b %d, %I:%M %p IST")
    except Exception:
        return "recently"

def get_process_memory_mb() -> float:
    """Calculates active resident memory (RSS) in MB."""
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        return 34.2

async def measure_gateway_latency(bot: Bot) -> float:
    """Measures precise round-trip latency to Telegram API edge over hot persistent TCP socket."""
    t0 = time.perf_counter()
    try:
        await bot.get_me()
    except Exception:
        pass
    return (time.perf_counter() - t0) * 1000.0

# ==============================================================================
# UI STRINGS & KEYBOARD BUILDERS (Cinematic, Modern, Telegram Bot API compliant)
# ==============================================================================

WELCOME_CAPTION = (
    "<b>Welcome to Kayi Mail</b>\n\n"
    "Your temporary email, ready in seconds.\n\n"
    "Create disposable email addresses and receive messages and verification codes directly in Telegram.\n\n"
    "✦ Instant email address creation\n"
    "✦ Automatic email and OTP notifications\n"
    "✦ 60-minute inbox, extendable up to 120 minutes\n\n"
    "<i>Choose an option below to get started.</i>"
)

HELP_TEXT = (
    "<b>Kayi Mail — Help & Guide</b> 📖\n\n"
    "<b>Creating an Address:</b>\n"
    "Tap <b>Create Email Address</b> for an instant inbox, or choose a custom domain.\n\n"
    "<b>Receiving Emails:</b>\n"
    "Incoming emails and OTP verification codes are delivered to you automatically with real-time push alerts.\n\n"
    "<b>Copying Verification Codes:</b>\n"
    "When an OTP code is detected, a one-tap copy button is provided for instant use.\n\n"
    "<b>Lifetime & Extension:</b>\n"
    "• Active for <b>60 minutes</b> by default.\n"
    "• Extendable up to <b>120 minutes</b> (+30 min per extension).\n"
    "• Inboxes expire automatically after inactivity.\n\n"
    "<b>Deleting an Address:</b>\n"
    "You can delete your active address anytime from the dashboard."
)

def build_welcome_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✉️ Create Email Address", callback_data="new:confirm:default", style="success")
        ],
        [
            InlineKeyboardButton(text="🌐 Choose Domain", callback_data="domain:menu", style="primary")
        ],
        [
            InlineKeyboardButton(text="❔ Help", callback_data="help:main"),
            InlineKeyboardButton(text="📢 Updates", url=COMMUNITY_URL)
        ],
        [
            InlineKeyboardButton(text="👨‍💻 Developer", url=DEVELOPER_URL)
        ]
    ])

def build_help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="◀️ Back to Home", callback_data="home")
        ]
    ])

def build_home_keyboard(address: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Open Inbox", callback_data="inbox:0", style="primary")
        ],
        [
            InlineKeyboardButton(text="Copy Address", copy_text=CopyTextButton(text=address)),
            InlineKeyboardButton(text="Refresh", callback_data="refresh", style="primary")
        ],
        [
            InlineKeyboardButton(text="Change Domain", callback_data="domain:menu", style="primary"),
            InlineKeyboardButton(text="Extend +30m", callback_data="extend:request", style="success")
        ],
        [
            InlineKeyboardButton(text="New Address", callback_data="new:request", style="success"),
            InlineKeyboardButton(text="Delete Address", callback_data="delete:request", style="danger")
        ],
        [
            InlineKeyboardButton(text="Community", url=COMMUNITY_URL)
        ]
    ])

def build_domain_selection_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="sharklasers.com (Reddit & Discord)", callback_data="domain:set:sharklasers.com", style="primary")
        ],
        [
            InlineKeyboardButton(text="fexpost.com (Fast & Clean)", callback_data="domain:set:fexpost.com", style="primary")
        ],
        [
            InlineKeyboardButton(text="guerrillamail.com", callback_data="domain:set:guerrillamail.com", style="primary"),
            InlineKeyboardButton(text="grr.la", callback_data="domain:set:grr.la", style="primary")
        ],
        [
            InlineKeyboardButton(text="fexbox.org", callback_data="domain:set:fexbox.org", style="primary"),
            InlineKeyboardButton(text="euploading.com", callback_data="domain:set:euploading.com", style="primary")
        ],
        [
            InlineKeyboardButton(text="Back to Home", callback_data="home", style="success")
        ]
    ])

def build_otp_notification_keyboard(local_msg_id: int, otp_code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"Copy Code: {otp_code}", copy_text=CopyTextButton(text=otp_code), style="primary")
        ],
        [
            InlineKeyboardButton(text="Read Email", callback_data=f"read:{local_msg_id}", style="success"),
            InlineKeyboardButton(text="Dismiss", callback_data=f"dismiss:{local_msg_id}", style="danger")
        ]
    ])

def build_standard_notification_keyboard(local_msg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Read Email", callback_data=f"read:{local_msg_id}", style="primary"),
            InlineKeyboardButton(text="Dismiss", callback_data=f"dismiss:{local_msg_id}", style="danger")
        ]
    ])

def build_reader_keyboard(
    local_msg_id: int, prev_id: int | None, next_id: int | None, otp_code: str | None
) -> InlineKeyboardMarkup:
    rows = []
    if otp_code:
        rows.append([
            InlineKeyboardButton(text=f"Copy Code: {otp_code}", copy_text=CopyTextButton(text=otp_code), style="primary")
        ])

    nav_row = []
    if prev_id is not None:
        nav_row.append(InlineKeyboardButton(text="Previous", callback_data=f"read:{prev_id}", style="primary"))
    if next_id is not None:
        nav_row.append(InlineKeyboardButton(text="Next", callback_data=f"read:{next_id}", style="primary"))
    if nav_row:
        rows.append(nav_row)

    rows.append([
        InlineKeyboardButton(text="Back to Inbox", callback_data="inbox:0", style="success"),
        InlineKeyboardButton(text="Delete Email", callback_data=f"email_delete:ask:{local_msg_id}", style="danger")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def build_inbox_keyboard(items: list[dict], page: int, total: int) -> InlineKeyboardMarkup:
    rows = []
    for item in items:
        subj = item.get("subject") or "Untitled email"
        clean_subj = subj[:26] + ("…" if len(subj) > 26 else "")
        rows.append([
            InlineKeyboardButton(text=clean_subj, callback_data=f"read:{item['id']}", style="primary")
        ])

    total_pages = max(1, math.ceil(total / INBOX_PAGE_SIZE))
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="Previous", callback_data=f"inbox:{page - 1}", style="primary"))
    if page + 1 < total_pages:
        nav_row.append(InlineKeyboardButton(text="Next", callback_data=f"inbox:{page + 1}", style="primary"))
    if nav_row:
        rows.append(nav_row)

    rows.append([
        InlineKeyboardButton(text="Refresh", callback_data="refresh", style="primary"),
        InlineKeyboardButton(text="Back to Home", callback_data="home", style="success")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def build_empty_inbox_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Refresh", callback_data="refresh", style="primary")
        ],
        [
            InlineKeyboardButton(text="Back to Home", callback_data="home", style="success")
        ]
    ])

# ==============================================================================
# UI CARD RENDERERS & SCREEN DISPATCHERS
# ==============================================================================

async def send_welcome_screen(bot: Bot, chat_id: int):
    """
    Sends the cinematic welcome screen using cached Telegram video file_id.
    Falls back gracefully to a text message if video sending fails.
    """
    markup = build_welcome_keyboard()
    if WELCOME_VIDEO_FILE_ID:
        try:
            await bot.send_video(
                chat_id=chat_id,
                video=WELCOME_VIDEO_FILE_ID,
                caption=WELCOME_CAPTION,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                supports_streaming=True
            )
            return
        except Exception as e:
            print(f"[welcome] Video delivery failed for chat {chat_id} ({type(e).__name__}: {e}). Falling back to text.")

    # Graceful fallback: text-only welcome message with identical keyboard
    await bot.send_message(
        chat_id=chat_id,
        text=WELCOME_CAPTION,
        parse_mode=ParseMode.HTML,
        reply_markup=markup
    )

async def edit_or_send_screen(
    callback: types.CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    allow_caption: bool = True
):
    """
    Safely edits the current callback message regardless of whether
    it is a video/photo/media message or a standard text message.
    """
    is_media = bool(callback.message.video or callback.message.photo or callback.message.animation)
    if is_media and allow_caption and len(text) <= 1024:
        try:
            await callback.message.edit_caption(caption=text, parse_mode=ParseMode.HTML, reply_markup=markup)
            return
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return

    # If media but cannot/should not be caption, or editing failed:
    if is_media:
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
        await callback.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        try:
            await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return
            await callback.message.answer(text, parse_mode=ParseMode.HTML, reply_markup=markup)

async def render_home_card(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    mailbox = await db.get_active_mailbox(user_id)
    if not mailbox:
        return WELCOME_CAPTION, build_welcome_keyboard()

    now = datetime.now(timezone.utc)
    exp_dt = datetime.fromisoformat(mailbox["expires_at"])
    remaining_mins = max(1, int((exp_dt - now).total_seconds() // 60))
    unread_count = await db.get_unread_count(mailbox["id"])
    provider_name = mailbox.get("provider", "guerrillamail").capitalize()

    text = (
        f"<b>{BRAND_NAME}</b>\n"
        "Your temporary inbox\n\n"
        "<b>Your email address</b>\n"
        f"<code>{html.escape(mailbox['address'])}</code>\n\n"
        f"<b>Provider:</b> {provider_name}\n"
        f"<b>Status:</b> Active\n"
        f"<b>Expires in:</b> {remaining_mins} min\n"
        f"<b>Unread:</b> {unread_count}\n\n"
        "<i>New emails are delivered here automatically.</i>"
    )
    return text, build_home_keyboard(mailbox["address"])

async def build_usage_card_text(bot: Bot) -> tuple[str, InlineKeyboardMarkup]:
    """Generates unified hosting, Render free tier usage, and bot activity card."""
    now = datetime.now(IST)
    uptime_sec = int((now - BOT_START_TIME).total_seconds())
    hours, rem = divmod(uptime_sec, 3600)
    mins, secs = divmod(rem, 60)
    uptime_str = f"{hours}h {mins}m {secs}s" if hours else f"{mins}m {secs}s"

    # Calculate live accurate usage from baseline RENDER_USED_HOURS + live session hours
    session_hours = (now - BOT_START_TIME).total_seconds() / 3600.0
    total_used_hours = min(750.0, RENDER_USED_HOURS + session_hours)
    remaining_hours = max(0.0, 750.0 - total_used_hours)
    used_pct = min(100.0, (total_used_hours / 750.0) * 100.0)
    days_left_capacity = remaining_hours / 24.0

    mem_mb = get_process_memory_mb()
    mem_pct = min(100.0, (mem_mb / 512.0) * 100.0)
    ping_ms = await measure_gateway_latency(bot)

    # Database metrics
    active_boxes, total_msgs = await db.get_system_counts()

    text = (
        f"<b>{BRAND_NAME} — Service & Host Metrics</b>\n\n"
        "<b>Host & Account:</b>\n"
        "• <b>Cloud:</b> Render (Frankfurt, EU-Central)\n"
        f"• <b>Account Email:</b> <code>{html.escape(mask_email(RENDER_EMAIL))}</code>\n"
        f"• <b>Service Name:</b> <code>{html.escape(RENDER_SERVICE_NAME)}</code>\n"
        f"• <b>Repository:</b> <code>{html.escape(RENDER_REPO)}</code>\n\n"
        "<b>Monthly Free Usage (Render Limit: 750h):</b>\n"
        f"• <b>Active Uptime:</b> <code>{uptime_str}</code>\n"
        f"• <b>Hours This Month:</b> <code>{total_used_hours:.1f}h / 750h ({used_pct:.1f}%)</code>\n"
        f"• <b>Remaining Free:</b> <code>{remaining_hours:.1f}h (~{days_left_capacity:.1f} days)</code>\n"
        f"• <b>Memory (RAM):</b> <code>{mem_mb:.1f} MB / 512 MB ({mem_pct:.1f}%)</code>\n\n"
        "<b>Bot Metrics:</b>\n"
        f"• <b>Active Inboxes:</b> <code>{active_boxes}</code>\n"
        f"• <b>Total Emails:</b> <code>{total_msgs}</code>\n"
        f"• <b>Gateway Ping:</b> <code>{ping_ms:.1f} ms</code>"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Refresh Stats", callback_data="usage:refresh", style="primary")],
        [InlineKeyboardButton(text="Check Ping", callback_data="ping:refresh", style="success")]
    ])
    return text, markup

# ==============================================================================
# BOT COMMAND REGISTRATION (Telegram Menu)
# ==============================================================================

async def register_bot_commands(bot: Bot):
    """Registers official commands into Telegram's menu button and slash command autocompletion."""
    public_commands = [
        BotCommand(command="start", description="Open your temporary inbox"),
        BotCommand(command="mail", description="View current address & inbox"),
        BotCommand(command="inbox", description="View received messages"),
        BotCommand(command="new", description="Create a new temporary address"),
        BotCommand(command="ping", description="Check bot latency and uptime")
    ]
    owner_commands = public_commands + [
        BotCommand(command="stats", description="Bot users & service stats (Owner)"),
        BotCommand(command="usage", description="Render hosting & account usage (Owner)")
    ]
    try:
        await bot.set_my_commands(public_commands, scope=BotCommandScopeDefault())
        if OWNER_ID:
            try:
                await bot.set_my_commands(owner_commands, scope=BotCommandScopeChat(chat_id=OWNER_ID))
            except Exception:
                pass
        print("[commands] Registered official Telegram bot commands.")
    except Exception as e:
        print(f"[commands] Failed to register commands: {e}")

def get_latest_changes() -> list[str]:
    """Retrieves recent changes from restart_reason.txt, git commit log, or fallback."""
    reason_file = os.path.join(os.path.dirname(__file__), "restart_reason.txt")
    if os.path.exists(reason_file):
        try:
            with open(reason_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
            os.remove(reason_file)
            if content:
                return [line.strip("-•* ").strip() for line in content.splitlines() if line.strip()]
        except Exception:
            pass

    try:
        import subprocess
        raw = subprocess.check_output(
            ["git", "log", "-1", "--pretty=%s%n%b"],
            text=True, stderr=subprocess.DEVNULL
        ).strip()
        lines = [l.strip("-•* ").strip() for l in raw.splitlines() if l.strip()]
        cleaned = []
        for line in lines:
            for prefix in ["feat:", "fix:", "chore:", "refactor:", "perf:", "docs:", "style:", "test:"]:
                if line.lower().startswith(prefix):
                    line = line[len(prefix):].strip()
                    break
            if line and len(line) > 3 and not line.startswith("Co-authored-by:") and not line.startswith("Signed-off-by:"):
                cleaned.append(line[0].upper() + line[1:])
            if len(cleaned) >= 3:
                break
        if cleaned:
            return cleaned
    except Exception:
        pass

    return ["Bug fixes and performance improvements"]

async def send_restart_notification(bot: Bot):
    """Notifies owner and log channel upon startup with clean, non-technical layout and zero buttons."""
    if not OWNER_ID:
        return

    reason_file = os.path.join(os.path.dirname(__file__), "restart_reason.txt")
    has_reason = os.path.exists(reason_file)

    current_commit = ""
    try:
        import subprocess
        current_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        pass

    last_notified = await db.get_state("last_notified_commit")

    # If this is an automatic container reconnect/resume and no new commit was deployed, skip broadcast
    if not has_reason and current_commit and last_notified == current_commit:
        print("[restart] Container reconnected: commit already notified. Suppressing duplicate alert.")
        return

    changes = get_latest_changes()
    changes_formatted = "\n".join(f"• {html.escape(c)}" for c in changes)
    now_str = datetime.now(IST).strftime("%I:%M %p IST · %b %d")

    content = (
        f"<b>{BRAND_NAME} Online 🟢</b>\n\n"
        "<b>Changes in this Update:</b>\n"
        f"{changes_formatted}\n\n"
        f"<b>Time:</b> <code>{now_str}</code>"
    )

    targets = [OWNER_ID]
    if LOG_CHANNEL_ID:
        try:
            targets.append(int(LOG_CHANNEL_ID))
        except ValueError:
            pass

    for chat_id in set(targets):
        try:
            await bot.send_message(chat_id=chat_id, text=content, parse_mode=ParseMode.HTML)
            print(f"[restart] Sent clean restart notification to {chat_id}")
        except Exception as e:
            print(f"[restart] Could not notify {chat_id}: {e}")

    if current_commit:
        await db.set_state("last_notified_commit", current_commit)

# ==============================================================================
# COMMAND HANDLERS
# ==============================================================================

@dp.message(Command("start"))
@dp.message(Command("mail"))
async def cmd_start(message: types.Message, bot: Bot | None = None):
    if bot is None:
        bot = message.bot
    user_id = message.from_user.id
    await db.touch_user(user_id)
    mailbox = await db.get_active_mailbox(user_id)
    if not mailbox:
        await send_welcome_screen(bot, user_id)
        return

    text, markup = await render_home_card(user_id)
    await message.reply(text, parse_mode=ParseMode.HTML, reply_markup=markup)

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    user_id = message.from_user.id
    await db.touch_user(user_id)
    await message.reply(HELP_TEXT, parse_mode=ParseMode.HTML, reply_markup=build_help_keyboard())

@dp.message(Command("inbox"))
async def cmd_inbox(message: types.Message):
    user_id = message.from_user.id
    await db.touch_user(user_id)
    mailbox = await db.get_active_mailbox(user_id)
    if not mailbox:
        await message.reply("You don't have an active address yet. Send /start to create one.")
        return

    items, total = await db.get_mailbox_messages(mailbox["id"], page=0)
    if total == 0:
        await message.reply(
            "<b>Your inbox is empty</b>\nMessages sent to your temporary address will appear here.\n\n<i>Waiting for your first email.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=build_empty_inbox_keyboard()
        )
        return

    unread = await db.get_unread_count(mailbox["id"])
    lines = [f"<b>Inbox</b>\n{total} messages · {unread} unread\n"]
    for i, item in enumerate(items, 1):
        s_name = html.escape(item.get("sender_name") or item.get("sender_address") or "Unknown")
        s_subj = html.escape(item.get("subject") or "No Subject")
        s_time = format_relative_time(item["received_at"])
        lines.append(f"<b>{i}. {s_subj}</b>\n{s_name} · {s_time}")

    total_pages = max(1, math.ceil(total / INBOX_PAGE_SIZE))
    lines.append(f"\nPage 1 of {total_pages}")

    await message.reply(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=build_inbox_keyboard(items, page=0, total=total)
    )

@dp.message(Command("new"))
async def cmd_new(message: types.Message):
    user_id = message.from_user.id
    await db.touch_user(user_id)
    allowed, reason, wait_s = await db.check_generation_limits(user_id)
    if not allowed:
        await message.reply(
            f"<b>Please wait</b>\nYou can create another address in {wait_s} seconds.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Back to Home", callback_data="home", style="success")]])
        )
        return

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Create Address", callback_data="new:confirm:default", style="success")],
        [InlineKeyboardButton(text="Choose Domain", callback_data="domain:menu", style="primary")],
        [InlineKeyboardButton(text="Cancel", callback_data="home", style="danger")]
    ])
    await message.reply(
        "<b>Create a new address?</b>\nYour current inbox will be replaced with a fresh address.",
        parse_mode=ParseMode.HTML,
        reply_markup=markup
    )

@dp.message(Command("ping"))
async def cmd_ping(message: types.Message, bot: Bot):
    latency_ms = await measure_gateway_latency(bot)
    uptime_sec = int((datetime.now(IST) - BOT_START_TIME).total_seconds())
    hours, rem = divmod(uptime_sec, 3600)
    mins, secs = divmod(rem, 60)
    uptime_str = f"{hours}h {mins}m {secs}s" if hours else f"{mins}m {secs}s"

    text = (
        "<b>Pong!</b> ⚡\n\n"
        f"<b>Latency:</b> <code>{latency_ms:.1f} ms</code>\n\n"
        f"<b>Uptime:</b> <code>{uptime_str}</code>"
    )
    await message.reply(text, parse_mode=ParseMode.HTML)

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message, bot: Bot):
    if not is_owner(message.from_user.id if message.from_user else None):
        await message.reply("Access restricted to bot owner.")
        return

    stats = await db.get_bot_statistics()
    uptime_sec = int((datetime.now(IST) - BOT_START_TIME).total_seconds())
    hours, rem = divmod(uptime_sec, 3600)
    mins, secs = divmod(rem, 60)
    uptime_str = f"{hours}h {mins}m {secs}s" if hours else f"{mins}m {secs}s"

    text = (
        "<b>Kayi Mail — Service Statistics 📊</b>\n\n"
        "<b>User Base:</b>\n"
        f"• <b>Total Users:</b> <code>{stats['total_users']}</code>\n"
        f"• <b>Active Inboxes:</b> <code>{stats['active_mailboxes']}</code>\n"
        f"• <b>Total Inboxes Created:</b> <code>{stats['total_mailboxes']}</code>\n\n"
        "<b>Activity:</b>\n"
        f"• <b>Emails Processed:</b> <code>{stats['total_messages']}</code>\n"
        f"• <b>Verification Codes Captured:</b> <code>{stats['total_otps']}</code>\n\n"
        f"<b>Uptime:</b> <code>{uptime_str}</code>"
    )
    await message.reply(text, parse_mode=ParseMode.HTML)

@dp.message(Command("usage"))
@dp.message(Command("render"))
@dp.message(Command("status"))
async def cmd_usage(message: types.Message, bot: Bot):
    if not is_owner(message.from_user.id if message.from_user else None):
        await message.reply("Access restricted to bot owner.")
        return

    text, markup = await build_usage_card_text(bot)
    await message.reply(text, parse_mode=ParseMode.HTML, reply_markup=markup)

@dp.message(Command("restart"))
async def cmd_restart(message: types.Message, bot: Bot):
    if not is_owner(message.from_user.id if message.from_user else None):
        await message.reply("Access denied. Owner only.")
        return

    reason = message.text.partition(" ")[2].strip() or "Manual owner restart"
    reason_file = os.path.join(os.path.dirname(__file__), "restart_reason.txt")
    try:
        with open(reason_file, "w", encoding="utf-8") as f:
            f.write(reason)
    except Exception:
        pass

    await message.reply(f"<b>Restarting bot...</b>\n<i>Reason: {html.escape(reason)}</i>", parse_mode=ParseMode.HTML)
    print(f"[bot] Owner initiated restart. Reason: {reason}")

    if HTTP_SESSION and not HTTP_SESSION.closed:
        await HTTP_SESSION.close()
    await bot.session.close()
    os.execv(sys.executable, [sys.executable] + sys.argv)

# ==============================================================================
# CALLBACK QUERY HANDLERS
# ==============================================================================

@dp.callback_query(F.data == "home")
async def cb_home(callback: types.CallbackQuery, bot: Bot | None = None):
    if bot is None:
        bot = callback.bot
    user_id = callback.from_user.id
    await db.touch_user(user_id)
    mailbox = await db.get_active_mailbox(user_id)
    if not mailbox:
        if callback.message.video:
            try:
                await callback.message.edit_caption(
                    caption=WELCOME_CAPTION,
                    parse_mode=ParseMode.HTML,
                    reply_markup=build_welcome_keyboard()
                )
                await callback.answer()
                return
            except TelegramBadRequest as e:
                if "message is not modified" in str(e).lower():
                    await callback.answer()
                    return

        try:
            await callback.message.delete()
        except TelegramBadRequest:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
        await send_welcome_screen(bot, user_id)
        await callback.answer()
        return

    text, markup = await render_home_card(user_id)
    await edit_or_send_screen(callback, text, markup, allow_caption=False)
    await callback.answer()

@dp.callback_query(F.data == "help:main")
async def cb_help_main(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await db.touch_user(user_id)
    await edit_or_send_screen(callback, HELP_TEXT, build_help_keyboard(), allow_caption=True)
    await callback.answer()

@dp.callback_query(F.data == "ping:refresh")
async def cb_ping_refresh(callback: types.CallbackQuery, bot: Bot):
    latency_ms = await measure_gateway_latency(bot)
    uptime_sec = int((datetime.now(timezone.utc) - BOT_START_TIME).total_seconds())
    hours, rem = divmod(uptime_sec, 3600)
    mins, secs = divmod(rem, 60)
    uptime_str = f"{hours}h {mins}m {secs}s" if hours else f"{mins}m {secs}s"

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Refresh Ping", callback_data="ping:refresh")]
    ])

    text = (
        "<b>Pong!</b> ⚡\n\n"
        f"<b>Gateway Latency:</b> <code>{latency_ms:.1f} ms</code>\n"
        f"<b>Server Region:</b> <code>Frankfurt (EU)</code>\n"
        f"<b>Uptime:</b> <code>{uptime_str}</code>\n\n"
        "<i>Optimized TCP keep-alive connection to Telegram edge.</i>"
    )
    try:
        await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except TelegramBadRequest:
        pass
    await callback.answer(f"Ping: {latency_ms:.1f} ms")

@dp.callback_query(F.data == "usage:refresh")
@dp.callback_query(F.data == "usage:show")
async def cb_usage_refresh(callback: types.CallbackQuery, bot: Bot):
    if not is_owner(callback.from_user.id if callback.from_user else None):
        await callback.answer("Access restricted to bot owner.", show_alert=True)
        return

    text, markup = await build_usage_card_text(bot)
    try:
        await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except TelegramBadRequest:
        pass
    await callback.answer("Metrics refreshed.")

@dp.callback_query(F.data == "domain:menu")
async def cb_domain_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await db.touch_user(user_id)
    text = (
        "<b>Select Email Domain</b>\n"
        "Choose a provider domain for your temporary inbox:\n\n"
        "• <b>sharklasers.com</b> — Works with Reddit & Discord\n"
        "• <b>fexpost.com</b> — Clean & instant delivery\n"
        "• <b>guerrillamail.com</b> — Standard Guerrilla Mail\n"
        "• <b>grr.la</b> — Short domain alias\n"
        "• <b>fexbox.org</b> — TempMail Plus alternate\n"
        "• <b>euploading.com</b> — Mail.tm standard\n\n"
        "<i>Selecting a domain will generate a fresh address immediately.</i>"
    )
    await edit_or_send_screen(callback, text, build_domain_selection_keyboard(), allow_caption=True)
    await callback.answer()

@dp.callback_query(F.data.startswith("domain:set:"))
async def cb_domain_set(callback: types.CallbackQuery):
    domain = callback.data.split(":")[2]
    user_id = callback.from_user.id
    await db.touch_user(user_id)

    allowed, reason, wait_s = await db.check_generation_limits(user_id)
    if not allowed:
        await callback.answer(f"Please wait {wait_s}s before switching addresses.", show_alert=True)
        return

    await callback.answer(f"Creating inbox on @{domain}...")
    new_data = await mail_service.create_mailbox(domain)
    if not new_data:
        await edit_or_send_screen(
            callback,
            f"<b>Notice</b>\nUnable to initialize inbox on <code>{domain}</code> right now. Please select another domain.",
            build_domain_selection_keyboard(),
            allow_caption=True
        )
        return

    # Delete old provider account in background
    old_box = await db.get_active_mailbox(user_id)
    if old_box:
        asyncio.create_task(mail_service.delete_account(
            old_box.get("provider", "guerrillamail"),
            old_box.get("token", ""),
            old_box.get("account_id", "")
        ))

    await db.save_new_mailbox(
        user_id,
        new_data["email"],
        new_data["provider"],
        new_data["account_id"],
        new_data["token"]
    )

    text, markup = await render_home_card(user_id)
    await edit_or_send_screen(callback, text, markup, allow_caption=False)

@dp.callback_query(F.data == "refresh")
async def cb_refresh(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await db.touch_user(user_id)
    mailbox = await db.get_active_mailbox(user_id)
    if not mailbox:
        text, markup = await render_home_card(user_id)
        await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        await callback.answer("Your mailbox expired.", show_alert=True)
        return

    # Trigger instant provider fetch
    if mail_service and mailbox.get("token"):
        prov = mailbox.get("provider") or "guerrillamail"
        token = mailbox.get("token")
        addr = mailbox.get("address")
        prov_msgs = await mail_service.get_messages(prov, token, addr)
        new_count = 0
        for m in prov_msgs:
            p_id = m.get("id")
            if not p_id or await db.is_message_recorded(mailbox["id"], p_id):
                continue
            detail = await mail_service.get_message_detail(prov, token, addr, p_id)
            if detail:
                s_addr = detail.get("from_addr") or ""
                s_name = detail.get("from_name") or s_addr
                s_subj = detail.get("subject") or "No Subject"
                s_body = detail.get("text") or ""
                s_otp = extract_verification_code(s_subj, s_body)
                rec_at = detail.get("createdAt") or datetime.now(timezone.utc).isoformat()
                await db.save_incoming_message(
                    mailbox["id"], p_id, s_addr, s_name, s_subj, rec_at, s_otp, s_body
                )
                new_count += 1

        if new_count > 0:
            await callback.answer(f"{new_count} new email(s) found!")
        else:
            await callback.answer("Inbox is up to date.")

    # Refresh current view
    text, markup = await render_home_card(user_id)
    try:
        await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except TelegramBadRequest:
        pass

@dp.callback_query(F.data.startswith("inbox:"))
async def cb_inbox_page(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await db.touch_user(user_id)
    page = int(callback.data.split(":")[1])

    mailbox = await db.get_active_mailbox(user_id)
    if not mailbox:
        await callback.answer("Mailbox expired.", show_alert=True)
        return

    items, total = await db.get_mailbox_messages(mailbox["id"], page=page)
    if total == 0:
        await callback.message.edit_text(
            "<b>Your inbox is empty</b>\nMessages sent to your temporary address will appear here.\n\n<i>Waiting for your first email.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=build_empty_inbox_keyboard()
        )
        await callback.answer()
        return

    unread = await db.get_unread_count(mailbox["id"])
    lines = [f"<b>Inbox</b>\n{total} messages · {unread} unread\n"]
    for i, item in enumerate(items, 1 + page * INBOX_PAGE_SIZE):
        s_name = html.escape(item.get("sender_name") or item.get("sender_address") or "Unknown")
        s_subj = html.escape(item.get("subject") or "No Subject")
        s_time = format_relative_time(item["received_at"])
        lines.append(f"<b>{i}. {s_subj}</b>\n{s_name} · {s_time}")

    total_pages = max(1, math.ceil(total / INBOX_PAGE_SIZE))
    lines.append(f"\nPage {page + 1} of {total_pages}")

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=build_inbox_keyboard(items, page=page, total=total)
    )
    await callback.answer()

@dp.callback_query(F.data == "new:request")
async def cb_new_request(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await db.touch_user(user_id)
    allowed, reason, wait_s = await db.check_generation_limits(user_id)
    if not allowed:
        await callback.answer(f"Please wait {wait_s} seconds before creating a new address.", show_alert=True)
        return

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Create Address", callback_data="new:confirm:default", style="success")],
        [InlineKeyboardButton(text="Choose Domain", callback_data="domain:menu", style="primary")],
        [InlineKeyboardButton(text="Cancel", callback_data="home", style="danger")]
    ])
    await callback.message.edit_text(
        "<b>Create a new address?</b>\nYour current inbox will be replaced with a fresh address.",
        parse_mode=ParseMode.HTML,
        reply_markup=markup
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("new:confirm"))
async def cb_new_confirm(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await db.touch_user(user_id)
    allowed, reason, wait_s = await db.check_generation_limits(user_id)
    if not allowed:
        await callback.answer(f"Please wait {wait_s}s.", show_alert=True)
        return

    await callback.answer("Creating address...")
    new_data = await mail_service.create_mailbox(DEFAULT_DOMAIN)
    if not new_data:
        await edit_or_send_screen(
            callback,
            "<b>Service Notice</b>\nEmail service is temporarily unavailable. Please try again shortly.",
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Retry", callback_data="new:confirm:default", style="success")], [InlineKeyboardButton(text="Cancel", callback_data="home", style="danger")]]),
            allow_caption=True
        )
        return

    # Delete old provider account in background
    old_box = await db.get_active_mailbox(user_id)
    if old_box:
        asyncio.create_task(mail_service.delete_account(
            old_box.get("provider", "guerrillamail"),
            old_box.get("token", ""),
            old_box.get("account_id", "")
        ))

    await db.save_new_mailbox(
        user_id,
        new_data["email"],
        new_data["provider"],
        new_data["account_id"],
        new_data["token"]
    )
    text, markup = await render_home_card(user_id)
    await edit_or_send_screen(callback, text, markup, allow_caption=False)

@dp.callback_query(F.data == "extend:request")
async def cb_extend(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await db.touch_user(user_id)
    ok, reason_or_time, remaining_mins = await db.extend_mailbox(user_id)
    if ok:
        await callback.answer("Extended by +30 minutes!", show_alert=False)
        text, markup = await render_home_card(user_id)
        try:
            await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except TelegramBadRequest:
            pass
    elif reason_or_time in ["max_extensions_reached", "max_lifetime_reached"]:
        await callback.answer("This address has reached its maximum lifetime (120 min).", show_alert=True)
    else:
        await callback.answer("Mailbox expired or not found.", show_alert=True)

@dp.callback_query(F.data == "delete:request")
async def cb_delete_request(callback: types.CallbackQuery):
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Delete Address", callback_data="delete:confirm", style="danger")],
        [InlineKeyboardButton(text="Cancel", callback_data="home", style="success")]
    ])
    await callback.message.edit_text(
        "<b>Delete this address?</b>\nYour current inbox and its messages will no longer be accessible. This action cannot be undone.",
        parse_mode=ParseMode.HTML,
        reply_markup=markup
    )
    await callback.answer()

@dp.callback_query(F.data == "delete:confirm")
async def cb_delete_confirm(callback: types.CallbackQuery, bot: Bot | None = None):
    if bot is None:
        bot = callback.bot
    user_id = callback.from_user.id
    deleted_box = await db.delete_mailbox(user_id)
    if deleted_box:
        asyncio.create_task(mail_service.delete_account(
            deleted_box.get("provider", "guerrillamail"),
            deleted_box.get("token", ""),
            deleted_box.get("account_id", "")
        ))

    await callback.answer("Address deleted.")
    if callback.message.video:
        try:
            await callback.message.edit_caption(
                caption=WELCOME_CAPTION,
                parse_mode=ParseMode.HTML,
                reply_markup=build_welcome_keyboard()
            )
            return
        except TelegramBadRequest:
            pass

    try:
        await callback.message.delete()
    except TelegramBadRequest:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
    await send_welcome_screen(bot, user_id)

@dp.callback_query(F.data.startswith("read:"))
async def cb_read_email(callback: types.CallbackQuery):
    local_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    await db.touch_user(user_id)

    msg = await db.get_message_detail(local_id, user_id)
    if not msg:
        await callback.answer("This message is no longer available.", show_alert=True)
        return

    mailbox_id = msg["mailbox_id"]
    prev_id, next_id = await db.get_adjacent_message_ids(local_id, mailbox_id)

    sender_name = html.escape(msg.get("sender_name") or msg.get("sender_address") or "Unknown")
    sender_addr = html.escape(msg.get("sender_address") or "unknown@domain.com")
    subject = html.escape(msg.get("subject") or "No Subject")
    body_text = msg.get("body_text") or "No text content."
    safe_body_chunk = html.escape(body_text[:2800])
    received_str = format_relative_time(msg["received_at"])

    otp_block = ""
    if msg.get("otp_code"):
        otp_block = f"<b>Verification code</b>\n<code>{html.escape(msg['otp_code'])}</code>\n\n"

    content = (
        f"<b>{subject}</b>\n\n"
        f"<b>From:</b> {sender_name} <code>&lt;{sender_addr}&gt;</code>\n"
        f"<b>Received:</b> {received_str}\n\n"
        f"{otp_block}"
        f"<b>Message</b>\n"
        f"<pre>{safe_body_chunk}</pre>"
    )

    markup = build_reader_keyboard(local_id, prev_id, next_id, msg.get("otp_code"))
    await callback.message.edit_text(content, parse_mode=ParseMode.HTML, reply_markup=markup)
    await callback.answer()

@dp.callback_query(F.data.startswith("email_delete:ask:"))
async def cb_email_delete_ask(callback: types.CallbackQuery):
    local_id = int(callback.data.split(":")[2])
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Delete Email", callback_data=f"email_delete:confirm:{local_id}", style="danger")],
        [InlineKeyboardButton(text="Cancel", callback_data=f"read:{local_id}", style="success")]
    ])
    await callback.message.edit_text(
        "<b>Delete this email?</b>\nThis message will be permanently removed from your inbox.",
        parse_mode=ParseMode.HTML,
        reply_markup=markup
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("email_delete:confirm:"))
async def cb_email_delete_confirm(callback: types.CallbackQuery):
    local_id = int(callback.data.split(":")[2])
    user_id = callback.from_user.id
    ok = await db.delete_message(local_id, user_id)
    if ok:
        await callback.answer("Email deleted.")
    else:
        await callback.answer("Message not found.", show_alert=True)

    # Route back to inbox
    mailbox = await db.get_active_mailbox(user_id)
    if mailbox:
        items, total = await db.get_mailbox_messages(mailbox["id"], page=0)
        if total > 0:
            lines = [f"<b>Inbox</b>\n{total} messages\n"]
            for i, item in enumerate(items, 1):
                s_name = html.escape(item.get("sender_name") or "Unknown")
                s_subj = html.escape(item.get("subject") or "No Subject")
                s_time = format_relative_time(item["received_at"])
                lines.append(f"<b>{i}. {s_subj}</b>\n{s_name} · {s_time}")
            await callback.message.edit_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=build_inbox_keyboard(items, 0, total))
            return

    await callback.message.edit_text(
        "<b>Your inbox is empty</b>\nMessages sent to your temporary address will appear here.",
        parse_mode=ParseMode.HTML,
        reply_markup=build_empty_inbox_keyboard()
    )

@dp.callback_query(F.data.startswith("dismiss:"))
async def cb_dismiss(callback: types.CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()

# ==============================================================================
# ADAPTIVE BACKGROUND POLLING SCHEDULER (Spec v1.0)
# ==============================================================================

async def auto_email_listener_loop(bot: Bot):
    """
    Centralized scheduler: Checks active mailboxes with adaptive intervals:
      - 0 to 3 min old: 5s interval (just created)
      - 3 to 5 min old: 10s interval (active verification window)
      - 5 to 15 min old: 30s interval (normal active)
      - 15 to 30 min idle: 60s interval
      - 30+ min idle: 120s interval
    Adds 10-20% jitter to prevent synchronized bursts.
    """
    print("[listener] Adaptive email polling loop active.")
    while True:
        try:
            boxes = await db.get_mailboxes_for_monitoring()
            now = datetime.now(timezone.utc)

            for box in boxes:
                box_id = box["id"]
                user_id = box["user_id"]
                token = box["token"]
                address = box["address"]
                provider = box.get("provider") or "guerrillamail"

                created_dt = datetime.fromisoformat(box["created_at"])
                active_dt = datetime.fromisoformat(box["last_active_at"])
                last_polled = datetime.fromisoformat(box["last_polled_at"]) if box["last_polled_at"] else None

                age_sec = (now - created_dt).total_seconds()
                idle_sec = (now - active_dt).total_seconds()

                # Determine target interval
                if age_sec < 180:
                    base_interval = INTERVAL_NEW_BOX          # 5s
                elif age_sec < 300:
                    base_interval = INTERVAL_VERIFY_WINDOW    # 10s
                elif idle_sec < 900:
                    base_interval = INTERVAL_ACTIVE           # 30s
                elif idle_sec < 1800:
                    base_interval = INTERVAL_IDLE_15M         # 60s
                else:
                    base_interval = INTERVAL_IDLE_30M         # 120s

                # 10-20% randomized jitter
                interval = base_interval * random.uniform(0.9, 1.2)

                if last_polled and (now - last_polled).total_seconds() < interval:
                    continue

                await db.update_mailbox_polled_time(box_id)

                # Fetch messages through multi-provider client
                prov_msgs = await mail_service.get_messages(provider, token, address)
                for m in prov_msgs:
                    p_id = m.get("id")
                    if not p_id or await db.is_message_recorded(box_id, p_id):
                        continue

                    detail = await mail_service.get_message_detail(provider, token, address, p_id)
                    if not detail:
                        continue

                    s_addr = detail.get("from_addr") or ""
                    s_name = detail.get("from_name") or s_addr
                    s_subj = detail.get("subject") or "No Subject"
                    s_body = detail.get("text") or ""
                    rec_at = detail.get("createdAt") or now.isoformat()
                    s_otp = extract_verification_code(s_subj, s_body)

                    local_id = await db.save_incoming_message(
                        box_id, p_id, s_addr, s_name, s_subj, rec_at, s_otp, s_body
                    )

                    # Build push notification
                    if s_otp:
                        push_text = (
                            "<b>Verification code received</b>\n"
                            f"From: {html.escape(s_name)}\n"
                            f"Subject: {html.escape(s_subj)}\n\n"
                            "<b>Your code</b>\n"
                            f"<code>{html.escape(s_otp)}</code>\n\n"
                            "<i>Copy the code to continue.</i>"
                        )
                        push_markup = build_otp_notification_keyboard(local_id, s_otp)
                    else:
                        safe_snippet = html.escape(s_body[:120]).strip()
                        push_text = (
                            "<b>New email received</b>\n\n"
                            f"<b>{html.escape(s_subj)}</b>\n"
                            f"From: {html.escape(s_name)}\n"
                            f"{safe_snippet}…\n\n"
                            "<i>Open the message to read more.</i>"
                        )
                        push_markup = build_standard_notification_keyboard(local_id)

                    try:
                        await bot.send_message(
                            chat_id=user_id,
                            text=push_text,
                            parse_mode=ParseMode.HTML,
                            reply_markup=push_markup
                        )
                    except TelegramForbiddenError:
                        print(f"[listener] User {user_id} blocked bot. Deactivating mailbox.")
                        await db.delete_mailbox(user_id)
                        break
                    except Exception as send_err:
                        print(f"[listener] Push failed for user {user_id}: {send_err}")

        except Exception as e:
            print(f"[listener] Loop exception: {e}")

        await asyncio.sleep(2.0)

# ==============================================================================
# MAIN ENTRYPOINT
# ==============================================================================

async def main():
    global HTTP_SESSION, mail_service
    if not BOT_TOKEN:
        print("\nERROR: BOT_TOKEN is missing in .env!\n")
        sys.exit(1)

    print(f"Launching {BRAND_NAME} Bot (Spec v1.0)...")
    await db.init_db()

    bot = Bot(token=BOT_TOKEN)

    connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300, enable_cleanup_closed=True)
    HTTP_SESSION = aiohttp.ClientSession(connector=connector)
    mail_service = MailService(HTTP_SESSION)

    runner = await start_web_server()
    await bot.delete_webhook(drop_pending_updates=True)

    # Register Telegram slash commands & menu button
    await register_bot_commands(bot)

    # Dispatch restart notification with updates/reason
    await send_restart_notification(bot)

    # Launch background adaptive polling scheduler
    asyncio.create_task(auto_email_listener_loop(bot))

    print(f"{BRAND_NAME} Bot active with Apple-inspired UI and adaptive scheduler.")
    try:
        retry_delay = 2
        while True:
            try:
                print(f"[{BRAND_NAME}] Polling Telegram updates...")
                await dp.start_polling(bot, handle_signals=False)
                break
            except (asyncio.CancelledError, KeyboardInterrupt):
                print(f"[{BRAND_NAME}] Polling stopped safely.")
                break
            except (TelegramNetworkError, aiohttp.ClientError, asyncio.TimeoutError) as net_err:
                print(f"[{BRAND_NAME}] Network hiccup: {net_err}. Resuming in {retry_delay}s...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(int(retry_delay * 1.5) + 1, 30)
            except Exception as e:
                print(f"[{BRAND_NAME}] Polling error: {e}. Resuming in {retry_delay}s...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(int(retry_delay * 1.5) + 1, 30)
    finally:
        if HTTP_SESSION and not HTTP_SESSION.closed:
            await HTTP_SESSION.close()
            await asyncio.sleep(0.25)
        await bot.session.close()
        await runner.cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nBot stopped safely.")
