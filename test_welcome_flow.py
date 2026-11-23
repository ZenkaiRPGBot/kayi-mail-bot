import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))

import config
import bot
import database as db

async def run_welcome_tests():
    print("[TEST] Starting Kayi Mail Welcome Screen & Media Delivery Verification...")
    passed = 0
    total = 0

    # Test 1: Video file_id configuration and presence
    total += 1
    assert bool(config.WELCOME_VIDEO_FILE_ID), "WELCOME_VIDEO_FILE_ID must not be empty"
    assert config.WELCOME_VIDEO_FILE_ID.startswith("BAACAg"), "Expected valid Telegram video file_id prefix"
    print(f"  [PASS] 1. Reusable Telegram video file_id verified: {config.WELCOME_VIDEO_FILE_ID[:20]}...")
    passed += 1

    # Test 2: Exact welcome caption text
    total += 1
    assert "<b>Welcome to Kayi Mail</b>" in bot.WELCOME_CAPTION
    assert "Your temporary email, ready in seconds." in bot.WELCOME_CAPTION
    assert "✦ Instant email address creation" in bot.WELCOME_CAPTION
    assert "✦ Automatic email and OTP notifications" in bot.WELCOME_CAPTION
    assert "✦ 60-minute inbox, extendable up to 120 minutes" in bot.WELCOME_CAPTION
    assert "Need help? Use /help for instructions and feature guides." in bot.WELCOME_CAPTION
    assert "<i>Choose an option below to get started.</i>" in bot.WELCOME_CAPTION
    assert len(bot.WELCOME_CAPTION) < 1024, "Caption must fit within Telegram 1024-char limit"
    print("  [PASS] 2. Exact welcome caption validated (HTML format, formatting tags, length: %d chars)." % len(bot.WELCOME_CAPTION))
    passed += 1

    # Test 3: Exact keyboard rows and button actions (Simplified 3-row layout without redundant Help button)
    total += 1
    kb = bot.build_welcome_keyboard()
    rows = kb.inline_keyboard
    assert len(rows) == 3, f"Expected exactly 3 rows, got {len(rows)}"
    
    # Row 1: [ ✉️ Create Email Address ] — green/success
    assert len(rows[0]) == 1
    assert rows[0][0].text == "✉️ Create Email Address"
    assert rows[0][0].callback_data == "new:confirm:default"
    assert rows[0][0].style == "success"

    # Row 2: [ 🌐 Choose Domain ] — blue/primary
    assert len(rows[1]) == 1
    assert rows[1][0].text == "🌐 Choose Domain"
    assert rows[1][0].callback_data == "domain:menu"
    assert rows[1][0].style == "primary"

    # Row 3: [ 📢 Updates ] [ 👨‍💻 Developer ]
    assert len(rows[2]) == 2
    assert rows[2][0].text == "📢 Updates"
    assert rows[2][0].url == "https://t.me/KayiBots"
    assert rows[2][1].text == "👨‍💻 Developer"
    assert rows[2][1].url == "https://t.me/qouba"
    print("  [PASS] 3. Exact 3-row colored inline keyboard verified with native styles & targets.")
    passed += 1

    # Test 4: /start with NO active mailbox (video delivery path)
    total += 1
    with patch("database.touch_user", new=AsyncMock()), \
         patch("database.get_active_mailbox", new=AsyncMock(return_value=None)):
        
        mock_msg = MagicMock()
        mock_msg.from_user.id = 555111
        mock_bot = MagicMock()
        mock_bot.send_video = AsyncMock()
        mock_bot.send_message = AsyncMock()

        t0 = time.perf_counter()
        await bot.cmd_start(mock_msg, bot=mock_bot)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        assert mock_bot.send_video.called, "Should dispatch video when no mailbox exists"
        args, kwargs = mock_bot.send_video.call_args
        assert kwargs["video"] == config.WELCOME_VIDEO_FILE_ID
        assert kwargs["caption"] == bot.WELCOME_CAPTION
        assert kwargs["supports_streaming"] is True
        print(f"  [PASS] 4. /start (no mailbox) triggers cached video delivery ({latency_ms:.2f} ms server overhead).")
        passed += 1

    # Test 5: /start with EXISTING active mailbox (dashboard preservation)
    total += 1
    fake_box = {
        "id": 1,
        "user_id": 555111,
        "address": "testuser@sharklasers.com",
        "provider": "guerrillamail",
        "expires_at": "2026-11-22T13:00:00+00:00"
    }
    with patch("database.touch_user", new=AsyncMock()), \
         patch("database.get_active_mailbox", new=AsyncMock(return_value=fake_box)), \
         patch("database.get_unread_count", new=AsyncMock(return_value=0)):
        
        mock_msg_active = MagicMock()
        mock_msg_active.from_user.id = 555111
        mock_msg_active.reply = AsyncMock()
        mock_bot_active = MagicMock()
        mock_bot_active.send_video = AsyncMock()

        await bot.cmd_start(mock_msg_active, bot=mock_bot_active)
        assert not mock_bot_active.send_video.called, "Must NOT send welcome video to user with active mailbox"
        assert mock_msg_active.reply.called, "Must reply with active dashboard"
        assert "testuser@sharklasers.com" in str(mock_msg_active.reply.call_args)
        print("  [PASS] 5. /start (active mailbox) preserves existing active dashboard cleanly.")
        passed += 1

    # Test 6: Video delivery failure fallback to text
    total += 1
    mock_bot_failing = MagicMock()
    mock_bot_failing.send_video = AsyncMock(side_effect=Exception("TelegramNetworkError: Connection timed out"))
    mock_bot_failing.send_message = AsyncMock()

    await bot.send_welcome_screen(mock_bot_failing, chat_id=555111)
    assert mock_bot_failing.send_message.called, "Must fallback to send_message"
    fb_args, fb_kwargs = mock_bot_failing.send_message.call_args
    assert fb_kwargs["text"] == bot.WELCOME_CAPTION
    assert fb_kwargs["reply_markup"] is not None
    print("  [PASS] 6. Video delivery failure gracefully falls back to text welcome card.")
    passed += 1

    # Test 7: Help functionality (/help command, help:main, and subpage navigation)
    total += 1
    mock_msg_help = MagicMock()
    mock_msg_help.from_user.id = 555111
    mock_msg_help.reply = AsyncMock()
    with patch("database.touch_user", new=AsyncMock()):
        await bot.cmd_help(mock_msg_help)
        assert mock_msg_help.reply.called
        assert "Kayi Mail — Help Center" in str(mock_msg_help.reply.call_args)
        # Verify section buttons are attached
        reply_kb = mock_msg_help.reply.call_args[1].get("reply_markup") or mock_msg_help.reply.call_args[0][2]
        cb_datas = [btn.callback_data for row in reply_kb.inline_keyboard for btn in row if btn.callback_data]
        assert "help:sec:start" in cb_datas
        assert "help:sec:manage" in cb_datas
        assert "help:sec:reading" in cb_datas
        assert "help:sec:privacy" in cb_datas

    # Test callback on video message (caption edit to help center)
    mock_cb_help = MagicMock()
    mock_cb_help.from_user.id = 555111
    mock_cb_help.message.video = MagicMock()
    mock_cb_help.message.photo = None
    mock_cb_help.message.animation = None
    mock_cb_help.message.edit_caption = AsyncMock()
    mock_cb_help.answer = AsyncMock()
    with patch("database.touch_user", new=AsyncMock()):
        await bot.cb_help_main(mock_cb_help)
        assert mock_cb_help.message.edit_caption.called
        assert "Kayi Mail — Help Center" in str(mock_cb_help.message.edit_caption.call_args)

    # Test subpage navigation (e.g. help:sec:privacy)
    mock_cb_sec = MagicMock()
    mock_cb_sec.from_user.id = 555111
    mock_cb_sec.data = "help:sec:privacy"
    mock_cb_sec.message.video = None
    mock_cb_sec.message.photo = None
    mock_cb_sec.message.animation = None
    mock_cb_sec.message.edit_text = AsyncMock()
    mock_cb_sec.answer = AsyncMock()
    with patch("database.touch_user", new=AsyncMock()):
        await bot.cb_help_section(mock_cb_sec)
        assert mock_cb_sec.message.edit_text.called
        assert "Limits & Privacy" in str(mock_cb_sec.message.edit_text.call_args)
        # Verify back button points to help:main
        sec_kb = mock_cb_sec.message.edit_text.call_args[1].get("reply_markup")
        assert sec_kb.inline_keyboard[0][0].callback_data == "help:main"
        assert sec_kb.inline_keyboard[0][0].text == "← Back to Help"

    print("  [PASS] 7. /help command, Help Center sections, and subpage navigation verified.")
    passed += 1

    # Test 8: Navigation from video Help back to Home (restores welcome caption & buttons)
    total += 1
    mock_cb_back = MagicMock()
    mock_cb_back.from_user.id = 555111
    mock_cb_back.message.video = MagicMock()
    mock_cb_back.message.photo = None
    mock_cb_back.message.animation = None
    mock_cb_back.message.edit_caption = AsyncMock()
    mock_cb_back.answer = AsyncMock()
    with patch("database.touch_user", new=AsyncMock()), \
         patch("database.get_active_mailbox", new=AsyncMock(return_value=None)):
        await bot.cb_home(mock_cb_back, bot=MagicMock())
        assert mock_cb_back.message.edit_caption.called
        cap_call = mock_cb_back.message.edit_caption.call_args
        assert cap_call.kwargs["caption"] == bot.WELCOME_CAPTION
    print("  [PASS] 8. Back to Home smoothly restores welcome caption on active video message.")
    passed += 1

    # Test 9: Domain selection from video (edits caption safely)
    total += 1
    mock_cb_domain = MagicMock()
    mock_cb_domain.from_user.id = 555111
    mock_cb_domain.message.video = MagicMock()
    mock_cb_domain.message.photo = None
    mock_cb_domain.message.animation = None
    mock_cb_domain.message.edit_caption = AsyncMock()
    mock_cb_domain.answer = AsyncMock()
    with patch("database.touch_user", new=AsyncMock()):
        await bot.cb_domain_menu(mock_cb_domain)
        assert mock_cb_domain.message.edit_caption.called
        assert "Select Email Domain" in str(mock_cb_domain.message.edit_caption.call_args)
    print("  [PASS] 9. Choose Domain from video edits caption cleanly without BadRequest.")
    passed += 1

    # Test 10: Address creation from video (deletes video & delivers text dashboard)
    total += 1
    mock_cb_create = MagicMock()
    mock_cb_create.from_user.id = 555111
    mock_cb_create.message.video = MagicMock()
    mock_cb_create.message.photo = None
    mock_cb_create.message.animation = None
    mock_cb_create.message.delete = AsyncMock()
    mock_cb_create.message.answer = AsyncMock()
    mock_cb_create.answer = AsyncMock()
    
    mock_ms = MagicMock()
    mock_ms.create_mailbox = AsyncMock(return_value={"email": "newuser@sharklasers.com", "provider": "guerrillamail", "account_id": "123", "token": "tok"})
    mock_ms.delete_account = AsyncMock()
    orig_ms = bot.mail_service
    bot.mail_service = mock_ms

    try:
        with patch("database.touch_user", new=AsyncMock()), \
             patch("database.check_generation_limits", new=AsyncMock(return_value=(True, "ok", 0))), \
             patch("database.get_active_mailbox", new=AsyncMock(return_value=None)), \
             patch("database.save_new_mailbox", new=AsyncMock()), \
             patch("bot.render_home_card", new=AsyncMock(return_value=("<b>Kayi Mail</b>\nYour temporary inbox", bot.build_home_keyboard("newuser@sharklasers.com")))):
            
            await bot.cb_new_confirm(mock_cb_create)
            assert mock_cb_create.message.delete.called, "Should delete welcome video upon creating inbox"
            assert mock_cb_create.message.answer.called, "Should answer with text dashboard"
            assert "newuser@sharklasers.com" in str(mock_cb_create.message.answer.call_args)
        print("  [PASS] 10. Create Email Address transitions from video to text dashboard seamlessly.")
        passed += 1
    finally:
        bot.mail_service = orig_ms

    # Test 11: Address deletion resets user to welcome screen
    total += 1
    mock_cb_del = MagicMock()
    mock_cb_del.from_user.id = 555111
    mock_cb_del.message.video = None  # Text dashboard
    mock_cb_del.message.delete = AsyncMock()
    mock_cb_del.answer = AsyncMock()
    mock_bot_del = MagicMock()
    mock_bot_del.send_video = AsyncMock()
    mock_bot_del.send_message = AsyncMock()

    mock_ms = MagicMock()
    mock_ms.delete_account = AsyncMock()
    orig_ms = bot.mail_service
    bot.mail_service = mock_ms

    try:
        with patch("database.delete_mailbox", new=AsyncMock(return_value={"provider": "guerrillamail", "token": "tok", "account_id": "acc"})):
            await bot.cb_delete_confirm(mock_cb_del, bot=mock_bot_del)
            assert mock_cb_del.message.delete.called
            assert mock_bot_del.send_video.called or mock_bot_del.send_message.called
        print("  [PASS] 11. Delete Address deactivates mailbox and resets cleanly to cinematic welcome.")
        passed += 1
    finally:
        bot.mail_service = orig_ms

    print(f"\n[SUMMARY] ALL {passed}/{total} Welcome Screen & Fast Media tests PASSED!")

if __name__ == "__main__":
    asyncio.run(run_welcome_tests())
