import os
import sys
import time
import asyncio
from datetime import datetime, timezone
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import (
    BOT_TOKEN, OWNER_ID, LOG_CHANNEL_ID, BOT_START_TIME, BRAND_NAME, CHANNEL_USERNAME,
    RATE_LIMIT_SECONDS, MAILBOX_LIFETIME_MINUTES, POLLING_INTERVAL_SECONDS
)
import database as db
from mail_service import MailService, extract_verification_code
from keep_alive import start_web_server

dp = Dispatcher()
HTTP_SESSION: aiohttp.ClientSession | None = None
mail_service: MailService | None = None

def get_uptime_string() -> str:
    delta = datetime.now(timezone.utc) - BOT_START_TIME
    days = delta.days
    hours, remainder = divmod(delta.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0 or days > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)

def build_rich_restart_markdown() -> str:
    """Clean, non-technical rich message table for restart notification."""
    return f"""# {BRAND_NAME} Online

| Feature | Status |
| :--- | :---: |
| Bot Status | Online 🟢 |
| Mail Engine | Ready 🟢 |
| Smart OTP | Active 🟢 |
| Auto-Listener | Running 🟢 |

### What's New
- Instant disposable email generation
- Real-time incoming email notifications
- 1-tap OTP verification code copy
- Anti-abuse limits (60s cooldown, 1h expiry)
- Ecosystem integration for {CHANNEL_USERNAME}

*Ready for incoming requests.*"""

async def send_rich_restart_notification(bot: Bot):
    """Sends clean rich restart card to owner and optional log channel."""
    rich_md = build_rich_restart_markdown()
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendRichMessage"

    # 1. Send to Owner
    if OWNER_ID:
        try:
            payload = {"chat_id": OWNER_ID, "rich_message": {"markdown": rich_md}}
            if HTTP_SESSION:
                async with HTTP_SESSION.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        print(f"[restart] Restart notification sent to owner ({OWNER_ID})")
        except Exception as e:
            print(f"[restart] Notice: Could not send restart notification to owner: {e}")

    # 2. Send to Log Channel (if configured)
    if LOG_CHANNEL_ID:
        try:
            payload = {"chat_id": LOG_CHANNEL_ID, "rich_message": {"markdown": rich_md}}
            if HTTP_SESSION:
                async with HTTP_SESSION.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    print(f"[log_channel] Restart log sent to channel: {LOG_CHANNEL_ID}")
        except Exception as e:
            print(f"[log_channel] Notice: Could not send log to channel: {e}")

def build_main_keyboard(unread_count: int = 0) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    inbox_label = f"🔄 Check Inbox ({unread_count})" if unread_count > 0 else "🔄 Refresh Inbox"
    builder.row(
        types.InlineKeyboardButton(text=inbox_label, callback_data="check_inbox"),
        types.InlineKeyboardButton(text="📧 New Address", callback_data="new_mailbox")
    )
    builder.row(
        types.InlineKeyboardButton(text="⏳ Extend (+30m)", callback_data="extend_box"),
        types.InlineKeyboardButton(text="🗑️ Delete Mailbox", callback_data="delete_box")
    )
    channel_url = f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"
    builder.row(
        types.InlineKeyboardButton(text=f"📢 Join {CHANNEL_USERNAME}", url=channel_url)
    )
    return builder.as_markup()

async def format_mailbox_card(mailbox: dict) -> tuple[str, types.InlineKeyboardMarkup]:
    now = datetime.now(timezone.utc)
    exp_dt = datetime.fromisoformat(mailbox["expires_at"])
    rem_minutes = max(0, int((exp_dt - now).total_seconds() // 60))
    
    unread = 0
    if mail_service and mailbox.get("token"):
        msgs = await mail_service.get_messages(mailbox["token"])
        unread = len(msgs)

    text = (
        f"<b>🦅 {BRAND_NAME} — Instant Disposable Inbox</b>\n\n"
        f"📫 <b>Your Temporary Email:</b>\n"
        f"<code>{mailbox['email']}</code> <i>(Tap to copy)</i>\n\n"
        f"⏳ <b>Status:</b> <code>Listening for incoming mail...</code>\n"
        f"⏱️ <b>Expires in:</b> <code>{rem_minutes} minutes</code>\n\n"
        f"<i>Use this address anywhere. As soon as an email or OTP arrives, you will be notified instantly!</i>"
    )
    return text, build_main_keyboard(unread)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    await db.touch_user_activity(user_id)
    
    mailbox = await db.get_user_mailbox(user_id)
    
    if not mailbox:
        can_gen, wait_s = await db.can_generate_new(user_id, RATE_LIMIT_SECONDS)
        if not can_gen:
            await message.reply(
                f"⏳ Please wait <b>{wait_s}s</b> before generating a new address.",
                parse_mode=ParseMode.HTML
            )
            return

        status_msg = await message.reply("<code>Generating secure temporary email...</code>", parse_mode=ParseMode.HTML)
        new_data = await mail_service.create_mailbox()
        if not new_data:
            await status_msg.edit_text("❌ Failed to generate email. Please try again in a few moments.")
            return

        await db.save_user_mailbox(user_id, new_data["email"], new_data["token"], new_data["account_id"])
        mailbox = await db.get_user_mailbox(user_id)
        await status_msg.delete()

    card_text, reply_markup = await format_mailbox_card(mailbox)
    await message.reply(card_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

@dp.message(Command("new"))
async def cmd_new(message: types.Message):
    user_id = message.from_user.id
    can_gen, wait_s = await db.can_generate_new(user_id, RATE_LIMIT_SECONDS)
    if not can_gen:
        await message.reply(
            f"⏳ <b>Rate Limit:</b> Please wait <code>{wait_s}s</code> before generating another email.",
            parse_mode=ParseMode.HTML
        )
        return

    old_box = await db.get_user_mailbox(user_id)
    if old_box and old_box.get("token") and old_box.get("account_id"):
        await mail_service.delete_account(old_box["token"], old_box["account_id"])

    new_data = await mail_service.create_mailbox()
    if not new_data:
        await message.reply("Failed to generate mailbox. Please try again.")
        return

    await db.save_user_mailbox(user_id, new_data["email"], new_data["token"], new_data["account_id"])
    mailbox = await db.get_user_mailbox(user_id)
    card_text, reply_markup = await format_mailbox_card(mailbox)
    await message.reply(card_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

@dp.message(Command("inbox"))
async def cmd_inbox(message: types.Message):
    user_id = message.from_user.id
    await db.touch_user_activity(user_id)
    mailbox = await db.get_user_mailbox(user_id)
    if not mailbox:
        await message.reply("You don't have an active mailbox. Send /start to get one.")
        return

    msgs = await mail_service.get_messages(mailbox["token"])
    if not msgs:
        await message.reply("📭 <b>Inbox is empty!</b>\n\nNo emails have arrived yet. Send /start to view your address.", parse_mode=ParseMode.HTML)
        return

    builder = InlineKeyboardBuilder()
    for m in msgs[:8]:
        sender = m.get('from', {}).get('address', 'Unknown')
        subject = m.get('subject') or 'No Subject'
        btn_text = f"📩 {sender[:15]} — {subject[:20]}"
        builder.row(types.InlineKeyboardButton(text=btn_text, callback_data=f"read:{m['id']}"))
    builder.row(types.InlineKeyboardButton(text="🔄 Refresh", callback_data="check_inbox"))

    await message.reply(f"📬 <b>Your Inbox ({len(msgs)} Messages):</b>", parse_mode=ParseMode.HTML, reply_markup=builder.as_markup())

@dp.message(Command("delete"))
async def cmd_delete(message: types.Message):
    user_id = message.from_user.id
    mailbox = await db.get_user_mailbox(user_id)
    if mailbox and mailbox.get("token") and mailbox.get("account_id"):
        await mail_service.delete_account(mailbox["token"], mailbox["account_id"])
    await db.delete_user_mailbox(user_id)
    await message.reply("🗑️ <b>Mailbox deleted successfully.</b>\nSend /start to generate a fresh address anytime.", parse_mode=ParseMode.HTML)

@dp.callback_query(F.data == "check_inbox")
async def cb_check_inbox(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await db.touch_user_activity(user_id)
    mailbox = await db.get_user_mailbox(user_id)
    if not mailbox:
        await callback.answer("Your mailbox has expired. Send /start to create a new one.", show_alert=True)
        return

    msgs = await mail_service.get_messages(mailbox["token"])
    if not msgs:
        await callback.answer("📭 Inbox is empty. Waiting for emails...", show_alert=True)
        return

    builder = InlineKeyboardBuilder()
    for m in msgs[:8]:
        sender = m.get('from', {}).get('address', 'Unknown')
        subject = m.get('subject') or 'No Subject'
        btn_text = f"📩 {sender[:15]} — {subject[:20]}"
        builder.row(types.InlineKeyboardButton(text=btn_text, callback_data=f"read:{m['id']}"))
    builder.row(types.InlineKeyboardButton(text="⬅️ Back to Main", callback_data="back_main"))

    await callback.message.edit_text(
        f"📬 <b>Your Inbox ({len(msgs)} Messages):</b>\n\nTap on any email to view full content:",
        parse_mode=ParseMode.HTML,
        reply_markup=builder.as_markup()
    )

@dp.callback_query(F.data == "new_mailbox")
async def cb_new_mailbox(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    can_gen, wait_s = await db.can_generate_new(user_id, RATE_LIMIT_SECONDS)
    if not can_gen:
        await callback.answer(f"⏳ Cooldown: Please wait {wait_s}s before generating a new address.", show_alert=True)
        return

    old_box = await db.get_user_mailbox(user_id)
    if old_box and old_box.get("token") and old_box.get("account_id"):
        await mail_service.delete_account(old_box["token"], old_box["account_id"])

    new_data = await mail_service.create_mailbox()
    if not new_data:
        await callback.answer("Failed to create mailbox. Try again.", show_alert=True)
        return

    await db.save_user_mailbox(user_id, new_data["email"], new_data["token"], new_data["account_id"])
    mailbox = await db.get_user_mailbox(user_id)
    card_text, reply_markup = await format_mailbox_card(mailbox)
    await callback.message.edit_text(card_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    await callback.answer("✨ New email address generated!")

@dp.callback_query(F.data == "extend_box")
async def cb_extend_box(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    ok = await db.extend_mailbox(user_id, extra_minutes=30)
    if ok:
        mailbox = await db.get_user_mailbox(user_id)
        card_text, reply_markup = await format_mailbox_card(mailbox)
        await callback.message.edit_text(card_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        await callback.answer("⏱️ Extended by +30 minutes!", show_alert=False)
    else:
        await callback.answer("Mailbox expired or not found.", show_alert=True)

@dp.callback_query(F.data == "delete_box")
async def cb_delete_box(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    mailbox = await db.get_user_mailbox(user_id)
    if mailbox and mailbox.get("token") and mailbox.get("account_id"):
        await mail_service.delete_account(mailbox["token"], mailbox["account_id"])
    await db.delete_user_mailbox(user_id)
    
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="📧 Generate New Address", callback_data="new_mailbox"))
    await callback.message.edit_text(
        f"🗑️ <b>Mailbox deleted.</b>\n\nYour address has been wiped from the server. Tap below to create a new one anytime.",
        parse_mode=ParseMode.HTML,
        reply_markup=builder.as_markup()
    )
    await callback.answer("Mailbox deleted.")

@dp.callback_query(F.data.startswith("read:"))
async def cb_read_email(callback: types.CallbackQuery):
    msg_id = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    await db.touch_user_activity(user_id)
    mailbox = await db.get_user_mailbox(user_id)
    if not mailbox:
        await callback.answer("Mailbox expired.", show_alert=True)
        return

    detail = await mail_service.get_message_detail(mailbox["token"], msg_id)
    if not detail:
        await callback.answer("Could not load email content.", show_alert=True)
        return

    sender = detail.get("from", {}).get("address", "Unknown")
    sender_name = detail.get("from", {}).get("name") or sender
    subject = detail.get("subject") or "No Subject"
    body_text = detail.get("text") or "No text content."
    otp_code = extract_verification_code(subject, body_text)

    trimmed_body = body_text[:2500] if len(body_text) > 2500 else body_text

    builder = InlineKeyboardBuilder()
    if otp_code:
        builder.row(types.InlineKeyboardButton(text=f"🔑 Code: {otp_code} (Tap)", callback_data=f"copy_code:{otp_code}"))
    builder.row(types.InlineKeyboardButton(text="⬅️ Back to Inbox", callback_data="check_inbox"))

    content = (
        f"📧 <b>Email Details</b>\n\n"
        f"• <b>From:</b> <code>{sender_name} &lt;{sender}&gt;</code>\n"
        f"• <b>Subject:</b> <b>{subject}</b>\n\n"
        f"<b>Message Body:</b>\n"
        f"<pre>{trimmed_body}</pre>"
    )
    await callback.message.edit_text(content, parse_mode=ParseMode.HTML, reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("copy_code:"))
async def cb_copy_code(callback: types.CallbackQuery):
    code = callback.data.split(":", 1)[1]
    await callback.answer(f"Verification Code: {code}", show_alert=True)

@dp.callback_query(F.data == "back_main")
async def cb_back_main(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    mailbox = await db.get_user_mailbox(user_id)
    if not mailbox:
        await callback.answer("No active mailbox.", show_alert=True)
        return
    card_text, reply_markup = await format_mailbox_card(mailbox)
    await callback.message.edit_text(card_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

@dp.message(Command("ping"))
async def cmd_ping(message: types.Message, bot: Bot):
    t0 = time.perf_counter()
    await bot.get_me()
    latency = (time.perf_counter() - t0) * 1000
    uptime = get_uptime_string()
    response = (
        f"<b>{BRAND_NAME} — Pong!</b>\n\n"
        f"• <b>Speed:</b> <code>{latency:.1f} ms</code>\n"
        f"• <b>Uptime:</b> <code>{uptime}</code>\n"
        f"• <b>Status:</b> <code>Healthy</code>"
    )
    await message.reply(response, parse_mode=ParseMode.HTML)

@dp.message(Command("restart"))
async def cmd_restart(message: types.Message, bot: Bot):
    if message.from_user.id != OWNER_ID:
        await message.reply("Access denied. Owner only.")
        return
    await message.reply("<b>Restarting...</b>", parse_mode=ParseMode.HTML)
    if HTTP_SESSION and not HTTP_SESSION.closed:
        await HTTP_SESSION.close()
    await bot.session.close()
    os.execv(sys.executable, [sys.executable] + sys.argv)

async def auto_email_listener_loop(bot: Bot):
    """
    Active polling loop: Checks mailboxes of active users every 5 seconds.
    Instantly pushes incoming emails directly to the user's Telegram chat.
    """
    print("[listener] Active email polling loop started.")
    while True:
        try:
            active_boxes = await db.get_active_mailboxes()
            for box in active_boxes:
                user_id = box["user_id"]
                token = box.get("token")
                if not token:
                    continue

                msgs = await mail_service.get_messages(token)
                for m in msgs:
                    msg_id = m.get("id")
                    if not msg_id:
                        continue

                    already_seen = await db.is_message_recorded(msg_id)
                    if not already_seen:
                        detail = await mail_service.get_message_detail(token, msg_id)
                        if not detail:
                            continue

                        sender = detail.get("from", {}).get("address", "Unknown")
                        sender_name = detail.get("from", {}).get("name") or sender
                        subject = detail.get("subject") or "No Subject"
                        body_text = detail.get("text") or ""
                        otp_code = extract_verification_code(subject, body_text)

                        await db.record_message(msg_id, user_id, sender, subject, otp_code)

                        builder = InlineKeyboardBuilder()
                        if otp_code:
                            builder.row(types.InlineKeyboardButton(text=f"🔑 Copy OTP: {otp_code}", callback_data=f"copy_code:{otp_code}"))
                        builder.row(
                            types.InlineKeyboardButton(text="📖 Read Full Email", callback_data=f"read:{msg_id}")
                        )

                        push_text = (
                            f"📬 <b>New Email Received!</b>\n\n"
                            f"• <b>From:</b> <code>{sender_name} &lt;{sender}&gt;</code>\n"
                            f"• <b>Subject:</b> <b>{subject}</b>\n"
                        )
                        if otp_code:
                            push_text += f"• <b>Verification Code:</b> <code>{otp_code}</code>\n"

                        try:
                            await bot.send_message(
                                chat_id=user_id,
                                text=push_text,
                                parse_mode=ParseMode.HTML,
                                reply_markup=builder.as_markup()
                            )
                        except Exception as send_err:
                            print(f"[listener] Failed to push email to user {user_id}: {send_err}")

        except Exception as e:
            print(f"[listener] Loop exception: {e}")

        await asyncio.sleep(POLLING_INTERVAL_SECONDS)

async def main():
    global HTTP_SESSION, mail_service
    if not BOT_TOKEN:
        print("\nERROR: BOT_TOKEN is missing in .env!\n")
        sys.exit(1)

    print(f"Launching {BRAND_NAME} Bot...")
    await db.init_db()

    connector = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300, ssl=False, enable_cleanup_closed=True)
    HTTP_SESSION = aiohttp.ClientSession(connector=connector)
    mail_service = MailService(HTTP_SESSION)

    bot = Bot(token=BOT_TOKEN)
    runner = await start_web_server()
    await bot.delete_webhook(drop_pending_updates=True)

    # Send clean rich restart card to owner (and log channel if set)
    await send_rich_restart_notification(bot)

    # Launch background email listener
    asyncio.create_task(auto_email_listener_loop(bot))

    print(f"{BRAND_NAME} Bot is actively polling. Ready to receive requests.")
    try:
        await dp.start_polling(bot)
    finally:
        if HTTP_SESSION and not HTTP_SESSION.closed:
            await HTTP_SESSION.close()
        await bot.session.close()
        await runner.cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nBot stopped.")
