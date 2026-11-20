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
    BotCommand, BotCommandScopeDefault
)
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

from config import (
    BOT_TOKEN, OWNER_ID, LOG_CHANNEL_ID, BOT_START_TIME,
    BRAND_NAME, COMMUNITY_URL, INBOX_PAGE_SIZE,
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
# UI KEYBOARD BUILDERS (Clean, Apple-inspired, Telegram Bot API 10.x compliant)
# ==============================================================================

def build_welcome_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Create Address", callback_data="new:confirm:default", style="success")
        ],
        [
            InlineKeyboardButton(text="Choose Domain", callback_data="domain:menu", style="primary")
        ],
        [
            InlineKeyboardButton(text="Community", url=COMMUNITY_URL)
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
# UI CARD RENDERERS
# ==============================================================================

async def render_home_card(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    mailbox = await db.get_active_mailbox(user_id)
    if not mailbox:
        welcome_text = (
            f"<b>Welcome to {BRAND_NAME}</b>\n"
            "A private temporary inbox for quick signups and verification codes.\n\n"
            "Your address stays active for 60 minutes.\n"
            "Tap below to create your first inbox."
        )
        return welcome_text, build_welcome_keyboard()

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
        f"• <b>Account Email:</b> <code>{html.escape(RENDER_EMAIL)}</code>\n"
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
    commands = [
        BotCommand(command="start", description="Open your temporary inbox"),
        BotCommand(command="mail", description="View current address & inbox"),
        BotCommand(command="inbox", description="View received messages"),
        BotCommand(command="new", description="Create a new temporary address"),
        BotCommand(command="ping", description="Check bot latency and uptime"),
        BotCommand(command="usage", description="Render hosting & account usage")
    ]
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
        print("[commands] Registered official Telegram bot commands.")
    except Exception as e:
        print(f"[commands] Failed to register commands: {e}")

async def send_restart_notification(bot: Bot):
    """Notifies owner and log channel upon startup with clean Apple-inspired layout."""
    if not OWNER_ID:
        return

    reason_file = os.path.join(os.path.dirname(__file__), "restart_reason.txt")
    custom_reason = None
    if os.path.exists(reason_file):
        try:
            with open(reason_file, "r", encoding="utf-8") as f:
                custom_reason = f.read().strip()
            os.remove(reason_file)
        except Exception:
            pass

    ping_ms = await measure_gateway_latency(bot)
    now_str = datetime.now(IST).strftime("%I:%M:%S %p IST · %b %d")

    if custom_reason:
        content = (
            f"<b>{BRAND_NAME} · System Restart</b>\n\n"
            f"<b>Status:</b> 🟢 Online\n"
            f"<b>Cluster:</b> Frankfurt (EU-Central)\n"
            f"<b>Gateway Ping:</b> <code>{ping_ms:.1f} ms</code>\n"
            f"<b>Timestamp:</b> <code>{now_str}</code>\n\n"
            f"<blockquote><b>Restart Reason</b>\n{html.escape(custom_reason)}</blockquote>\n\n"
            "<i>All services operational and monitoring active.</i>"
        )
    else:
        content = (
            f"<b>{BRAND_NAME} · System Restart</b>\n\n"
            f"<b>Status:</b> 🟢 Online\n"
            f"<b>Cluster:</b> Frankfurt (EU-Central)\n"
            f"<b>Gateway Ping:</b> <code>{ping_ms:.1f} ms</code>\n"
            f"<b>Timestamp:</b> <code>{now_str}</code>\n\n"
            "<blockquote><b>What's New in this Build</b>\n"
            "• <b>Ultra-Low Latency:</b> Persistent TCP keep-alive socket\n"
            "• <b>Resource Monitor:</b> New /usage & /render stats command\n"
            "• <b>Multi-Bot Ready:</b> Unified hosting metrics card for groups\n"
            "• <b>Streamlined UI:</b> Clean typography and button hierarchy</blockquote>\n\n"
            "<i>Ready to process incoming emails and verification codes.</i>"
        )

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Check Ping", callback_data="ping:refresh", style="primary"),
            InlineKeyboardButton(text="Service Usage", callback_data="usage:refresh", style="success")
        ]
    ])

    targets = [OWNER_ID]
    if LOG_CHANNEL_ID:
        try:
            targets.append(int(LOG_CHANNEL_ID))
        except ValueError:
            pass

    for chat_id in set(targets):
        try:
            await bot.send_message(chat_id=chat_id, text=content, parse_mode=ParseMode.HTML, reply_markup=markup)
            print(f"[restart] Sent restart notification to {chat_id}")
        except Exception as e:
            print(f"[restart] Could not notify {chat_id}: {e}")

# ==============================================================================
# COMMAND HANDLERS
# ==============================================================================

@dp.message(Command("start"))
@dp.message(Command("mail"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    await db.touch_user(user_id)
    text, markup = await render_home_card(user_id)
    await message.reply(text, parse_mode=ParseMode.HTML, reply_markup=markup)

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
    uptime_sec = int((datetime.now(timezone.utc) - BOT_START_TIME).total_seconds())
    hours, rem = divmod(uptime_sec, 3600)
    mins, secs = divmod(rem, 60)
    uptime_str = f"{hours}h {mins}m {secs}s" if hours else f"{mins}m {secs}s"

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Refresh Ping", callback_data="ping:refresh", style="primary")],
        [InlineKeyboardButton(text="Service Usage", callback_data="usage:show", style="success")]
    ])

    text = (
        "<b>Pong!</b> ⚡\n\n"
        f"<b>Gateway Latency:</b> <code>{latency_ms:.1f} ms</code>\n"
        f"<b>Server Region:</b> <code>Frankfurt (EU)</code>\n"
        f"<b>Uptime:</b> <code>{uptime_str}</code>\n\n"
        "<i>Optimized TCP keep-alive connection to Telegram edge.</i>"
    )
    await message.reply(text, parse_mode=ParseMode.HTML, reply_markup=markup)

@dp.message(Command("usage"))
@dp.message(Command("render"))
@dp.message(Command("status"))
async def cmd_usage(message: types.Message, bot: Bot):
    text, markup = await build_usage_card_text(bot)
    await message.reply(text, parse_mode=ParseMode.HTML, reply_markup=markup)

@dp.message(Command("restart"))
async def cmd_restart(message: types.Message, bot: Bot):
    if message.from_user.id != OWNER_ID:
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
async def cb_home(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await db.touch_user(user_id)
    text, markup = await render_home_card(user_id)
    try:
        await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except TelegramBadRequest:
        pass
    await callback.answer()

@dp.callback_query(F.data == "ping:refresh")
async def cb_ping_refresh(callback: types.CallbackQuery, bot: Bot):
    latency_ms = await measure_gateway_latency(bot)
    uptime_sec = int((datetime.now(timezone.utc) - BOT_START_TIME).total_seconds())
    hours, rem = divmod(uptime_sec, 3600)
    mins, secs = divmod(rem, 60)
    uptime_str = f"{hours}h {mins}m {secs}s" if hours else f"{mins}m {secs}s"

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Refresh Ping", callback_data="ping:refresh", style="primary")],
        [InlineKeyboardButton(text="Service Usage", callback_data="usage:show", style="success")]
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
    await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=build_domain_selection_keyboard())
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
        await callback.message.edit_text(
            f"<b>Notice</b>\nUnable to initialize inbox on <code>{domain}</code> right now. Please select another domain.",
            parse_mode=ParseMode.HTML,
            reply_markup=build_domain_selection_keyboard()
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
    await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

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
        await callback.message.edit_text(
            "<b>Service Notice</b>\nEmail service is temporarily unavailable. Please try again shortly.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Retry", callback_data="new:confirm:default", style="success")], [InlineKeyboardButton(text="Cancel", callback_data="home", style="danger")]])
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
    await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

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
async def cb_delete_confirm(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    deleted_box = await db.delete_mailbox(user_id)
    if deleted_box:
        asyncio.create_task(mail_service.delete_account(
            deleted_box.get("provider", "guerrillamail"),
            deleted_box.get("token", ""),
            deleted_box.get("account_id", "")
        ))

    await callback.answer("Address deleted.")
    text, markup = await render_home_card(user_id)
    await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)

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
        await dp.start_polling(bot)
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
