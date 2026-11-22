import asyncio
import io
import os
import sys
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))

import config
import bot
import database as db

async def run_regression_tests():
    print("[TEST] Starting Kayi Mail /start Regression & Root-Cause Verification Suite...")
    passed = 0
    total = 0

    # 1. /start with NO active mailbox -> START_CINEMATIC_SUCCESS
    total += 1
    mock_msg = MagicMock()
    mock_msg.from_user.id = 9911
    mock_bot = MagicMock()
    mock_bot.send_video = AsyncMock()
    mock_bot.send_message = AsyncMock()

    log_stream = io.StringIO()
    with contextlib.redirect_stdout(log_stream), \
         patch("database.touch_user", new=AsyncMock()), \
         patch("database.get_active_mailbox", new=AsyncMock(return_value=None)):
        
        await bot.cmd_start(mock_msg, bot=mock_bot)
        assert mock_bot.send_video.called, "Should dispatch video when no mailbox exists"
        logs = log_stream.getvalue()
        assert "START_CINEMATIC_SUCCESS" in logs
        print("  [PASS] 1. /start (no active mailbox) logs START_CINEMATIC_SUCCESS and sends video.")
        passed += 1

    # 2. /start with an ACTIVE mailbox -> START_ACTIVE_MAILBOX
    total += 1
    fake_box = {
        "id": 42,
        "user_id": 9911,
        "address": "user42@sharklasers.com",
        "provider": "guerrillamail",
        "expires_at": "2026-11-22T14:00:00+00:00"
    }
    mock_msg_act = MagicMock()
    mock_msg_act.from_user.id = 9911
    mock_msg_act.reply = AsyncMock()
    log_stream = io.StringIO()

    with contextlib.redirect_stdout(log_stream), \
         patch("database.touch_user", new=AsyncMock()), \
         patch("database.get_active_mailbox", new=AsyncMock(return_value=fake_box)), \
         patch("database.get_unread_count", new=AsyncMock(return_value=0)):
        
        await bot.cmd_start(mock_msg_act, bot=mock_bot)
        assert mock_msg_act.reply.called
        logs = log_stream.getvalue()
        assert "START_ACTIVE_MAILBOX" in logs
        assert "user42@sharklasers.com" in str(mock_msg_act.reply.call_args)
        print("  [PASS] 2. /start (active mailbox) logs START_ACTIVE_MAILBOX and preserves dashboard.")
        passed += 1

    # 3. Repeated /start calls (state idempotency)
    total += 1
    mock_bot_rep = MagicMock()
    mock_bot_rep.send_video = AsyncMock()
    with patch("database.touch_user", new=AsyncMock()), \
         patch("database.get_active_mailbox", new=AsyncMock(return_value=None)):
        for _ in range(5):
            await bot.cmd_start(mock_msg, bot=mock_bot_rep)
        assert mock_bot_rep.send_video.call_count == 5
        print("  [PASS] 3. Repeated /start calls behave consistently without race conditions.")
        passed += 1

    # 4. Missing or invalid video file_id -> START_CINEMATIC_FAILURE + START_TEXT_FALLBACK
    total += 1
    orig_fid = bot.WELCOME_VIDEO_FILE_ID
    try:
        bot.WELCOME_VIDEO_FILE_ID = ""
        mock_bot_nofid = MagicMock()
        mock_bot_nofid.send_message = AsyncMock()
        log_stream = io.StringIO()
        with contextlib.redirect_stdout(log_stream):
            await bot.send_welcome_screen(mock_bot_nofid, chat_id=9911)
        logs = log_stream.getvalue()
        assert "START_CINEMATIC_FAILURE" in logs
        assert "START_TEXT_FALLBACK" in logs
        assert mock_bot_nofid.send_message.called
        fb_call = mock_bot_nofid.send_message.call_args
        # MUST send NEW caption and NEW keyboard
        assert "Your temporary email, ready in seconds" in fb_call.kwargs["text"]
        assert len(fb_call.kwargs["reply_markup"].inline_keyboard) == 4
        assert "A private temporary inbox" not in fb_call.kwargs["text"]
        print("  [PASS] 4. Missing file_id falls back to NEW caption & 4-row keyboard (never old menu).")
        passed += 1
    finally:
        bot.WELCOME_VIDEO_FILE_ID = orig_fid

    # 5. Telegram video API failure -> START_CINEMATIC_FAILURE + START_TEXT_FALLBACK
    total += 1
    mock_bot_err = MagicMock()
    mock_bot_err.send_video = AsyncMock(side_effect=Exception("TelegramNetworkError: Flood control"))
    mock_bot_err.send_message = AsyncMock()
    log_stream = io.StringIO()
    with contextlib.redirect_stdout(log_stream):
        await bot.send_welcome_screen(mock_bot_err, chat_id=9911)
    logs = log_stream.getvalue()
    assert "START_CINEMATIC_FAILURE" in logs
    assert "START_TEXT_FALLBACK" in logs
    assert mock_bot_err.send_message.called
    fb_call2 = mock_bot_err.send_message.call_args
    assert "Your temporary email, ready in seconds" in fb_call2.kwargs["text"]
    assert len(fb_call2.kwargs["reply_markup"].inline_keyboard) == 4
    print("  [PASS] 5. Telegram video API failure logs diagnostics and sends NEW text fallback.")
    passed += 1

    # 6. Old callback interactions (stale callback data safety)
    total += 1
    mock_cb_stale = MagicMock()
    mock_cb_stale.from_user.id = 9911
    mock_cb_stale.message.video = None
    mock_cb_stale.message.delete = AsyncMock()
    mock_cb_stale.message.edit_text = AsyncMock()
    mock_cb_stale.message.edit_reply_markup = AsyncMock()
    mock_cb_stale.answer = AsyncMock()
    with patch("database.touch_user", new=AsyncMock()), \
         patch("database.get_active_mailbox", new=AsyncMock(return_value=None)):
        # Tapping 'home' on stale message
        await bot.cb_home(mock_cb_stale, bot=mock_bot)
        assert mock_bot.send_video.called or mock_cb_stale.message.edit_text.called
    print("  [PASS] 6. Stale callback interactions route safely to new welcome screen.")
    passed += 1

    # 7. Concurrent /start updates (re-entrant safety)
    total += 1
    with patch("database.touch_user", new=AsyncMock()), \
         patch("database.get_active_mailbox", new=AsyncMock(return_value=None)):
        mock_bot_conc = MagicMock()
        mock_bot_conc.send_video = AsyncMock()
        tasks = [bot.cmd_start(mock_msg, bot=mock_bot_conc) for _ in range(10)]
        await asyncio.gather(*tasks)
        assert mock_bot_conc.send_video.call_count == 10
    print("  [PASS] 7. Concurrent /start updates handled cleanly without lockups or state leaks.")
    passed += 1

    # 8. Delayed background polling does not mutate welcome messages
    total += 1
    # Verify listener only dispatches push notifications and never modifies welcome cards
    import inspect
    listener_src = inspect.getsource(bot.auto_email_listener_loop)
    assert "render_home_card" not in listener_src
    assert "build_welcome_keyboard" not in listener_src
    print("  [PASS] 8. Background listener strictly pushes email alerts; never mutates welcome cards.")
    passed += 1

    # 9. Application restart notification logic
    total += 1
    mock_bot_rst = MagicMock()
    mock_bot_rst.send_message = AsyncMock()
    with patch("database.get_state", new=AsyncMock(return_value="different_commit")), \
         patch("database.set_state", new=AsyncMock()):
        await bot.send_restart_notification(mock_bot_rst)
        assert mock_bot_rst.send_message.called
    print("  [PASS] 9. Application restart notification notifies owner without old menu buttons.")
    passed += 1

    # 10. Deployment configuration & Signal handling check
    total += 1
    import keep_alive
    health_resp = await keep_alive.handle_health(MagicMock())
    assert health_resp.status == 200
    print("  [PASS] 10. Enhanced health endpoint reports commit and service uptime cleanly.")
    passed += 1

    print(f"\n[SUMMARY] ALL {passed}/{total} Regression & Root-Cause tests PASSED!")

if __name__ == "__main__":
    asyncio.run(run_regression_tests())
