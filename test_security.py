import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure project directory is on sys.path
sys.path.insert(0, os.path.dirname(__file__))

import config
import bot
import database as db

async def run_tests():
    print("[TEST] Starting Kayi Mail Extended Security & Regression Test Suite...")
    passed = 0
    total = 0

    # 1. Test cmd_ping markup safety
    total += 1
    mock_msg = MagicMock()
    mock_msg.reply = AsyncMock()
    mock_bot = MagicMock()
    mock_bot.get_me = AsyncMock()
    
    await bot.cmd_ping(mock_msg, mock_bot)
    assert mock_msg.reply.called, "cmd_ping should reply"
    call_args = mock_msg.reply.call_args
    assert "reply_markup" not in call_args.kwargs or call_args.kwargs["reply_markup"] is None
    print("  [PASS] 1. cmd_ping does not expose any admin buttons.")
    passed += 1

    # 2. Test cb_ping_refresh markup safety
    total += 1
    mock_cb = MagicMock()
    mock_cb.from_user.id = 123456789  # ordinary user
    mock_cb.message = MagicMock()
    mock_cb.message.edit_text = AsyncMock()
    mock_cb.answer = AsyncMock()

    await bot.cb_ping_refresh(mock_cb, mock_bot)
    assert mock_cb.message.edit_text.called
    edit_args = mock_cb.message.edit_text.call_args
    markup = edit_args.kwargs.get("reply_markup")
    assert markup is not None
    for row in markup.inline_keyboard:
        for b in row:
            assert b.callback_data != "usage:show", "cb_ping_refresh must NOT contain usage:show"
            assert "usage" not in (b.callback_data or ""), "No usage callback allowed in public ping"
    print("  [PASS] 2. cb_ping_refresh contains only safe ping refresh, zero usage buttons.")
    passed += 1

    # 3. Test cb_usage_refresh rejection for unauthorized user
    total += 1
    mock_cb_unauth = MagicMock()
    mock_cb_unauth.from_user.id = 999999999  # attacker / random user
    mock_cb_unauth.message = MagicMock()
    mock_cb_unauth.message.edit_text = AsyncMock()
    mock_cb_unauth.answer = AsyncMock()

    await bot.cb_usage_refresh(mock_cb_unauth, mock_bot)
    assert not mock_cb_unauth.message.edit_text.called, "Unauthorized user must not trigger edit_text"
    assert mock_cb_unauth.answer.called
    assert "Access restricted to bot owner." in str(mock_cb_unauth.answer.call_args)
    assert mock_cb_unauth.answer.call_args.kwargs.get("show_alert") is True
    print("  [PASS] 3. cb_usage_refresh strictly denies unauthorized users with alert.")
    passed += 1

    # 4. Test cb_usage_refresh with missing / None OWNER_ID (fail closed)
    total += 1
    orig_owner = bot.OWNER_ID
    try:
        bot.OWNER_ID = None
        mock_cb_missing_owner = MagicMock()
        mock_cb_missing_owner.from_user.id = orig_owner
        mock_cb_missing_owner.message = MagicMock()
        mock_cb_missing_owner.message.edit_text = AsyncMock()
        mock_cb_missing_owner.answer = AsyncMock()

        await bot.cb_usage_refresh(mock_cb_missing_owner, mock_bot)
        assert not mock_cb_missing_owner.message.edit_text.called, "Fail-closed check failed when OWNER_ID is None"
        print("  [PASS] 4. cb_usage_refresh fails closed when OWNER_ID is None or unset.")
        passed += 1
    finally:
        bot.OWNER_ID = orig_owner

    # 5. Test cmd_stats rejection for unauthorized user
    total += 1
    mock_msg_unauth = MagicMock()
    mock_msg_unauth.from_user.id = 888888888
    mock_msg_unauth.reply = AsyncMock()

    await bot.cmd_stats(mock_msg_unauth, mock_bot)
    assert mock_msg_unauth.reply.called
    assert "Access restricted to bot owner." in str(mock_msg_unauth.reply.call_args)
    print("  [PASS] 5. cmd_stats strictly denies unauthorized users.")
    passed += 1

    # 6. Test cmd_usage rejection for unauthorized user
    total += 1
    mock_msg_usage = MagicMock()
    mock_msg_usage.from_user.id = 777777777
    mock_msg_usage.reply = AsyncMock()

    await bot.cmd_usage(mock_msg_usage, mock_bot)
    assert mock_msg_usage.reply.called
    assert "Access restricted to bot owner." in str(mock_msg_usage.reply.call_args)
    print("  [PASS] 6. cmd_usage strictly denies unauthorized users.")
    passed += 1

    # 7. Test cmd_restart rejection for unauthorized user
    total += 1
    mock_msg_restart = MagicMock()
    mock_msg_restart.from_user.id = 666666666
    mock_msg_restart.reply = AsyncMock()

    await bot.cmd_restart(mock_msg_restart, mock_bot)
    assert mock_msg_restart.reply.called
    assert "Access denied. Owner only." in str(mock_msg_restart.reply.call_args)
    print("  [PASS] 7. cmd_restart strictly denies unauthorized users.")
    passed += 1

    # 8. Test authorized owner access to cb_usage_refresh
    total += 1
    mock_cb_owner = MagicMock()
    mock_cb_owner.from_user.id = bot.OWNER_ID
    mock_cb_owner.message = MagicMock()
    mock_cb_owner.message.edit_text = AsyncMock()
    mock_cb_owner.answer = AsyncMock()

    with patch("database.get_system_counts", new=AsyncMock(return_value=(1, 5))):
        await bot.cb_usage_refresh(mock_cb_owner, mock_bot)
        assert mock_cb_owner.message.edit_text.called, "Owner must be allowed to edit_text"
        assert mock_cb_owner.answer.called
        print("  [PASS] 8. cb_usage_refresh allows authorized owner access.")
        passed += 1

    # 9. Test is_owner helper unit correctness
    total += 1
    assert bot.is_owner(bot.OWNER_ID) is True
    assert bot.is_owner(999999999) is False
    assert bot.is_owner(None) is False
    assert bot.is_owner(0) is False
    print("  [PASS] 9. is_owner helper unit tests passed (strict identity matching).")
    passed += 1

    # 10. Test mask_email privacy helper
    total += 1
    assert bot.mask_email("2305101270081@paruluniversity.ac.in") == "230***@paruluniversity.ac.in"
    assert bot.mask_email("ab@domain.com") == "a***@domain.com"
    assert bot.mask_email("") == "[configured]"
    print("  [PASS] 10. mask_email masks institutional identifiers properly.")
    passed += 1

    # 11. Test normal user commands (cmd_start, cmd_inbox, cmd_new)
    total += 1
    with patch("database.touch_user", new=AsyncMock()), \
         patch("database.get_active_mailbox", new=AsyncMock(return_value=None)), \
         patch("database.check_generation_limits", new=AsyncMock(return_value=(True, "ok", 0))):
        
        user_msg = MagicMock()
        user_msg.from_user.id = 123456789
        user_msg.reply = AsyncMock()
        mock_bot = MagicMock()
        mock_bot.send_video = AsyncMock()
        mock_bot.send_message = AsyncMock()
        user_msg.bot = mock_bot

        # When no mailbox exists, cmd_start dispatches the cinematic welcome screen
        await bot.cmd_start(user_msg, bot=mock_bot)
        assert mock_bot.send_video.called or mock_bot.send_message.called
        sent_call = mock_bot.send_video.call_args or mock_bot.send_message.call_args
        assert "Welcome to" in str(sent_call)

        await bot.cmd_inbox(user_msg)
        assert user_msg.reply.called

        await bot.cmd_new(user_msg)
        assert user_msg.reply.called
        print("  [PASS] 11. Normal user commands (start, inbox, new) execute cleanly without regressions.")
        passed += 1

    print(f"\n[SUMMARY] {passed}/{total} tests PASSED successfully.")

if __name__ == "__main__":
    asyncio.run(run_tests())
