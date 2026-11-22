import os
import sys
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))

from aiogram import types, Bot
from aiogram.filters import CommandObject
import bot as bot_module
import database as db
from fsub import set_pending_start_param, get_and_clear_pending_start_param, FSubMiddleware

async def test_mail_deep_link():
    print("[TEST] Starting Kayi Mail /start help Deep-Link Test Suite...")
    await db.init_db()
    USER_ID = 55443322
    mock_bot = AsyncMock(spec=Bot)
    mock_bot.send_video = AsyncMock()
    mock_bot.send_message = AsyncMock()

    cmd_obj_help = CommandObject(prefix="/", command="start", args="help")
    cmd_obj_none = CommandObject(prefix="/", command="start", args=None)

    # 1. New user (no mailbox) sending /start help -> opens Help Center
    msg_new = MagicMock()
    msg_new.from_user = MagicMock(id=USER_ID)
    msg_new.text = "/start help"
    msg_new.reply = AsyncMock()
    msg_new.bot = mock_bot

    with patch("database.touch_user", new=AsyncMock()), patch("database.get_active_mailbox", new=AsyncMock(return_value=None)):
        await bot_module.cmd_start(msg_new, bot=mock_bot, command=cmd_obj_help)
        assert msg_new.reply.called, "Should reply with Help Center"
        assert not mock_bot.send_video.called, "Must NOT send cinematic video when start=help"
        reply_text = str(msg_new.reply.call_args)
        assert "Kayi Mail — Help Center" in reply_text
        print("  [PASS] 1. New user /start help opens Help Center immediately.")

    # 2. Existing user (with mailbox) sending /start help -> opens Help Center
    fake_mailbox = {
        "id": 99,
        "address": "active@sharklasers.com",
        "user_id": USER_ID,
        "expires_at": "2026-11-22T22:00:00+00:00",
        "provider": "guerrilla"
    }
    msg_exist = MagicMock()
    msg_exist.from_user = MagicMock(id=USER_ID)
    msg_exist.text = "/start help"
    msg_exist.reply = AsyncMock()
    msg_exist.bot = mock_bot

    with patch("database.touch_user", new=AsyncMock()), patch("database.get_active_mailbox", new=AsyncMock(return_value=fake_mailbox)):
        await bot_module.cmd_start(msg_exist, bot=mock_bot, command=cmd_obj_help)
        assert msg_exist.reply.called
        reply_text = str(msg_exist.reply.call_args)
        assert "Kayi Mail — Help Center" in reply_text
        assert "active@sharklasers.com" not in reply_text
        print("  [PASS] 2. Existing user /start help opens Help Center (not dashboard).")

    # 3. New user sending normal /start -> sends cinematic video
    msg_normal_new = MagicMock()
    msg_normal_new.from_user = MagicMock(id=USER_ID)
    msg_normal_new.text = "/start"
    msg_normal_new.reply = AsyncMock()
    msg_normal_new.bot = mock_bot

    with patch("database.touch_user", new=AsyncMock()), patch("database.get_active_mailbox", new=AsyncMock(return_value=None)):
        await bot_module.cmd_start(msg_normal_new, bot=mock_bot, command=cmd_obj_none)
        assert mock_bot.send_video.called, "Should send cinematic video for normal /start"
        print("  [PASS] 3. New user normal /start preserves cinematic welcome video.")

    # 4. Existing user sending normal /start -> replies with active mailbox dashboard
    msg_exist.text = "/start"
    msg_exist.reply.reset_mock()
    with patch("database.touch_user", new=AsyncMock()), patch("database.get_active_mailbox", new=AsyncMock(return_value=fake_mailbox)):
        await bot_module.cmd_start(msg_exist, bot=mock_bot, command=cmd_obj_none)
        assert msg_exist.reply.called
        reply_text = str(msg_exist.reply.call_args)
        assert "active@sharklasers.com" in reply_text
        print("  [PASS] 4. Existing user normal /start preserves active dashboard.")

    # 5. Non-member sends /start help -> FSub middleware intercepts and captures pending param
    middleware = FSubMiddleware()
    mock_unsub_msg = AsyncMock(spec=types.Message)
    mock_unsub_msg.text = "/start help"
    mock_unsub_msg.chat = MagicMock(type="private")
    mock_unsub_msg.from_user = MagicMock(id=998877)
    mock_unsub_msg.answer = AsyncMock()

    dummy_handler = AsyncMock()
    with patch("fsub.check_channel_membership", new=AsyncMock(return_value=(False, "left"))):
        await middleware(dummy_handler, mock_unsub_msg, {"event_from_user": mock_unsub_msg.from_user, "bot": mock_bot})
        dummy_handler.assert_not_called()
        assert mock_unsub_msg.answer.called
        # Check that pending param was registered
        assert get_and_clear_pending_start_param(998877) == "help"
        print("  [PASS] 5. Non-member /start help is intercepted by FSub and pending param is recorded.")

    # 6. Non-member joins and clicks fsub:verify after /start help -> transitions to Help Center
    set_pending_start_param(USER_ID, "help")
    mock_cb = MagicMock(spec=types.CallbackQuery)
    mock_cb.from_user = MagicMock(id=USER_ID)
    mock_cb.bot = mock_bot
    mock_cb.answer = AsyncMock()
    mock_cb.message = MagicMock()
    mock_cb.message.delete = AsyncMock()
    mock_cb.message.answer = AsyncMock()

    with patch("bot.check_channel_membership", new=AsyncMock(return_value=(True, "member"))):
        await bot_module.cb_fsub_verify(mock_cb, bot=mock_bot)
        assert mock_cb.message.answer.called
        reply_text = str(mock_cb.message.answer.call_args)
        assert "Kayi Mail — Help Center" in reply_text
        print("  [PASS] 6. FSub verification callback honors pending help parameter and opens Help Center.")

    print("\n[ALL 6/6 KAYI MAIL DEEP LINK TESTS PASSED!]\n")

if __name__ == "__main__":
    asyncio.run(test_mail_deep_link())
