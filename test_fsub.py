import os
import time
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from aiogram import types, Bot
from aiogram.enums import ParseMode
from aiogram.types import (
    ChatMemberOwner, ChatMemberAdministrator, ChatMemberMember,
    ChatMemberRestricted, ChatMemberLeft, ChatMemberBanned
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

import config
import database as db
from fsub import (
    FSubCache, check_channel_membership, FSubMiddleware,
    FSUB_PROMPT_TEXT, build_fsub_keyboard
)
import bot as bot_module

async def run_fsub_test_suite():
    print("[TEST] Starting Kayi Mail Mandatory Channel Membership (FSub) Test Suite (20 Scenarios)...")
    await db.init_db()

    # Shared test user ID
    USER_ID = 88001122
    OWNER_ID = config.OWNER_ID

    # Helper to construct a mock ChatMember object
    def make_member(status: str, is_member_attr: bool = True):
        m = MagicMock()
        m.status = status
        m.is_member = is_member_attr
        return m

    # --------------------------------------------------------------------------
    # Scenario 1: User is a member ('member')
    # --------------------------------------------------------------------------
    test_cache = FSubCache(default_ttl=5)
    mock_bot = AsyncMock(spec=Bot)
    mock_bot.get_chat_member.return_value = make_member("member")

    with patch("fsub.cache", test_cache):
        is_member, status = await check_channel_membership(mock_bot, USER_ID, bypass_cache=True)
        assert is_member is True, f"Expected True for member, got {is_member}"
        assert status == "member"
        assert test_cache.get(USER_ID) is True
    print("  [PASS] 1. User is a member ('member') -> access allowed & cached.")

    # --------------------------------------------------------------------------
    # Scenario 2: User is not a member ('left')
    # --------------------------------------------------------------------------
    mock_bot.get_chat_member.return_value = make_member("left")
    with patch("fsub.cache", test_cache):
        is_member, status = await check_channel_membership(mock_bot, USER_ID, bypass_cache=True)
        assert is_member is False, f"Expected False for left, got {is_member}"
        assert status == "left"
        assert test_cache.get(USER_ID) is False
    print("  [PASS] 2. User is not a member ('left') -> access denied & cached as False.")

    # --------------------------------------------------------------------------
    # Scenario 3: User is an administrator ('administrator')
    # --------------------------------------------------------------------------
    mock_bot.get_chat_member.return_value = make_member("administrator")
    with patch("fsub.cache", test_cache):
        is_member, status = await check_channel_membership(mock_bot, USER_ID, bypass_cache=True)
        assert is_member is True
        assert status == "administrator"
    print("  [PASS] 3. User is an administrator ('administrator') -> access allowed.")

    # --------------------------------------------------------------------------
    # Scenario 4: User is banned ('kicked')
    # --------------------------------------------------------------------------
    mock_bot.get_chat_member.return_value = make_member("kicked")
    with patch("fsub.cache", test_cache):
        is_member, status = await check_channel_membership(mock_bot, USER_ID, bypass_cache=True)
        assert is_member is False
        assert status == "kicked"
    print("  [PASS] 4. User is banned ('kicked') -> access denied.")

    # --------------------------------------------------------------------------
    # Scenario 5: Restricted member with is_member == True
    # --------------------------------------------------------------------------
    mock_bot.get_chat_member.return_value = make_member("restricted", is_member_attr=True)
    with patch("fsub.cache", test_cache):
        is_member, status = await check_channel_membership(mock_bot, USER_ID, bypass_cache=True)
        assert is_member is True
        assert status == "restricted"
    print("  [PASS] 5. Restricted member with is_member=True -> access allowed.")

    # --------------------------------------------------------------------------
    # Scenario 6: Restricted member with is_member == False
    # --------------------------------------------------------------------------
    mock_bot.get_chat_member.return_value = make_member("restricted", is_member_attr=False)
    with patch("fsub.cache", test_cache):
        is_member, status = await check_channel_membership(mock_bot, USER_ID, bypass_cache=True)
        assert is_member is False
        assert status == "restricted"
    print("  [PASS] 6. Restricted member with is_member=False -> access denied.")

    # --------------------------------------------------------------------------
    # Scenario 7: Membership API timeout (asyncio.TimeoutError)
    # --------------------------------------------------------------------------
    async def timeout_get_chat_member(*args, **kwargs):
        raise asyncio.TimeoutError()

    mock_bot.get_chat_member.side_effect = timeout_get_chat_member
    with patch("fsub.cache", test_cache):
        test_cache.invalidate(USER_ID)
        is_member, status = await check_channel_membership(mock_bot, USER_ID, bypass_cache=True)
        assert is_member is False
        assert status == "timeout"
    print("  [PASS] 7. Membership API timeout -> fails closed safely (denied).")

    # --------------------------------------------------------------------------
    # Scenario 8: Missing channel access (TelegramBadRequest 'member list is inaccessible')
    # --------------------------------------------------------------------------
    mock_bot.get_chat_member.side_effect = TelegramBadRequest(
        method="getChatMember", message="Bad Request: member list is inaccessible"
    )
    with patch("fsub.cache", test_cache):
        test_cache.invalidate(USER_ID)
        is_member, status = await check_channel_membership(mock_bot, USER_ID, bypass_cache=True)
        assert is_member is False
        assert "TelegramBadRequest" in status
    print("  [PASS] 8. Missing channel access / Telegram error -> fails closed safely.")

    # Reset mock_bot side_effect
    mock_bot.get_chat_member.side_effect = None

    # --------------------------------------------------------------------------
    # Scenario 9: Cache expiration
    # --------------------------------------------------------------------------
    short_cache = FSubCache(default_ttl=1)  # 1s TTL
    short_cache.set(USER_ID, True, ttl=1)
    assert short_cache.get(USER_ID) is True
    await asyncio.sleep(1.1)
    assert short_cache.get(USER_ID) is None, "Expected cache to expire after TTL"
    print("  [PASS] 9. Cache expiration -> entries expire strictly after TTL.")

    # --------------------------------------------------------------------------
    # Scenario 10: User leaves after verification
    # --------------------------------------------------------------------------
    temp_cache = FSubCache(default_ttl=1)
    mock_bot.get_chat_member.return_value = make_member("member")
    with patch("fsub.cache", temp_cache):
        # 1. Initially verified
        is_member, _ = await check_channel_membership(mock_bot, USER_ID, bypass_cache=True)
        assert is_member is True
        # 2. Cache is valid
        assert temp_cache.get(USER_ID) is True
        # 3. User leaves channel
        mock_bot.get_chat_member.return_value = make_member("left")
        # While cache is alive, cached status returns
        cached_val, _ = await check_channel_membership(mock_bot, USER_ID, bypass_cache=False)
        assert cached_val is True
        # Wait for TTL to expire
        await asyncio.sleep(1.1)
        # Next check queries Telegram and detects user left!
        fresh_val, status = await check_channel_membership(mock_bot, USER_ID, bypass_cache=False)
        assert fresh_val is False
        assert status == "left"
    print("  [PASS] 10. User leaves after verification -> access revoked when cache expires.")

    # --------------------------------------------------------------------------
    # Scenario 11: /start without membership
    # --------------------------------------------------------------------------
    middleware = FSubMiddleware()
    mock_msg = AsyncMock(spec=types.Message)
    mock_msg.answer = AsyncMock()
    mock_msg.chat = MagicMock(type="private")
    mock_user = MagicMock(spec=types.User, id=10101)
    mock_msg.from_user = mock_user
    mock_msg.text = "/start"

    handler_called = False
    async def dummy_handler(event, data):
        nonlocal handler_called
        handler_called = True
        return "handler_result"

    mock_bot.get_chat_member.return_value = make_member("left")
    with patch("fsub.cache", FSubCache(default_ttl=30)):
        result = await middleware(
            dummy_handler,
            mock_msg,
            {"event_from_user": mock_user, "bot": mock_bot}
        )
        assert handler_called is False, "Handler must NOT be called for non-member"
        assert result is None
        mock_msg.answer.assert_called_once()
        call_kwargs = mock_msg.answer.call_args.kwargs
        assert "Join Kayi Bots to Continue" in call_kwargs["text"]
        markup = call_kwargs["reply_markup"]
        assert len(markup.inline_keyboard) == 2
        assert markup.inline_keyboard[0][0].url == config.COMMUNITY_URL
        assert markup.inline_keyboard[1][0].callback_data == "fsub:verify"
    print("  [PASS] 11. /start without membership -> blocked by middleware & prompts FSub card.")

    # --------------------------------------------------------------------------
    # Scenario 12: /start with membership
    # --------------------------------------------------------------------------
    mock_msg.reset_mock()
    handler_called = False
    mock_bot.get_chat_member.return_value = make_member("member")
    with patch("fsub.cache", FSubCache(default_ttl=30)):
        result = await middleware(
            dummy_handler,
            mock_msg,
            {"event_from_user": mock_user, "bot": mock_bot}
        )
        assert handler_called is True, "Handler MUST be called for active member"
        assert result == "handler_result"
        mock_msg.answer.assert_not_called()
    print("  [PASS] 12. /start with membership -> passed through to handler smoothly.")

    # --------------------------------------------------------------------------
    # Scenario 13: fsub:verify success
    # --------------------------------------------------------------------------
    # Test user has no mailbox
    await db.delete_mailbox(USER_ID)
    mock_cb = AsyncMock(spec=types.CallbackQuery)
    mock_cb.answer = AsyncMock()
    mock_cb.from_user = MagicMock(id=USER_ID)
    mock_cb.data = "fsub:verify"
    mock_cb.message = AsyncMock(spec=types.Message)
    mock_cb.message.delete = AsyncMock()

    mock_bot.get_chat_member.return_value = make_member("member")
    with patch("fsub.cache", FSubCache(default_ttl=30)):
        with patch.object(bot_module, "send_welcome_screen", new_callable=AsyncMock) as mock_send_welcome:
            await bot_module.cb_fsub_verify(mock_cb, bot=mock_bot)
            mock_cb.answer.assert_any_call("Checking membership...")
            mock_cb.message.delete.assert_called_once()
            mock_send_welcome.assert_called_once_with(mock_bot, USER_ID)
    print("  [PASS] 13. fsub:verify success (no mailbox) -> deletes FSub card & sends cinematic welcome.")

    # --------------------------------------------------------------------------
    # Scenario 14: fsub:verify failure
    # --------------------------------------------------------------------------
    mock_cb.reset_mock()
    mock_cb.message.reset_mock()
    mock_bot.get_chat_member.return_value = make_member("left")
    with patch("fsub.cache", FSubCache(default_ttl=30)):
        with patch.object(bot_module, "send_welcome_screen", new_callable=AsyncMock) as mock_send_welcome:
            await bot_module.cb_fsub_verify(mock_cb, bot=mock_bot)
            mock_cb.answer.assert_any_call(
                "You haven't joined @KayiBots yet. Please join the channel and tap I've Joined.",
                show_alert=True
            )
            mock_send_welcome.assert_not_called()
            mock_cb.message.delete.assert_not_called()
    print("  [PASS] 14. fsub:verify failure -> shows alert and keeps FSub prompt active.")

    # --------------------------------------------------------------------------
    # Scenario 15: Existing callbacks cannot bypass FSub
    # --------------------------------------------------------------------------
    cb_handler_called = False
    async def dummy_cb_handler(event, data):
        nonlocal cb_handler_called
        cb_handler_called = True
        return "cb_handler_result"

    mock_existing_cb = AsyncMock(spec=types.CallbackQuery)
    mock_existing_cb.answer = AsyncMock()
    mock_existing_cb.from_user = MagicMock(id=USER_ID)
    mock_existing_cb.data = "domain:menu"
    mock_existing_cb.message = AsyncMock(spec=types.Message)
    mock_existing_cb.message.edit_text = AsyncMock()

    mock_bot.get_chat_member.return_value = make_member("left")
    with patch("fsub.cache", FSubCache(default_ttl=30)):
        res = await middleware(
            dummy_cb_handler,
            mock_existing_cb,
            {"event_from_user": mock_existing_cb.from_user, "bot": mock_bot}
        )
        assert cb_handler_called is False, "Existing callbacks must NOT bypass FSub"
        assert res is None
        mock_existing_cb.answer.assert_called_with("Please join @KayiBots to use Kayi Mail.", show_alert=True)
        mock_existing_cb.message.edit_text.assert_called_once()
    print("  [PASS] 15. Existing callbacks cannot bypass FSub -> intercepted with alert & FSub card.")

    # --------------------------------------------------------------------------
    # Scenario 16: Existing owner-only protections still work
    # --------------------------------------------------------------------------
    # Even if non-owner is a verified channel member, owner command is strictly denied
    mock_bot.get_chat_member.return_value = make_member("member")
    non_owner_msg = AsyncMock(spec=types.Message)
    non_owner_msg.from_user = MagicMock(id=12345)
    non_owner_msg.reply = AsyncMock()

    # Direct invocation of cmd_stats and cmd_usage
    await bot_module.cmd_stats(non_owner_msg, bot=mock_bot)
    non_owner_msg.reply.assert_called_with("Access restricted to bot owner.")

    non_owner_msg.reply.reset_mock()
    await bot_module.cmd_usage(non_owner_msg, bot=mock_bot)
    non_owner_msg.reply.assert_called_with("Access restricted to bot owner.")
    print("  [PASS] 16. Existing owner-only protections still work -> unauthorized users strictly denied.")

    # --------------------------------------------------------------------------
    # Scenario 17: Cinematic welcome remains functional
    # --------------------------------------------------------------------------
    send_video_called = False
    async def fake_send_video(chat_id, video, caption, parse_mode, reply_markup, supports_streaming):
        nonlocal send_video_called
        send_video_called = True
        assert video == config.WELCOME_VIDEO_FILE_ID
        assert "Welcome to Kayi Mail" in caption
        assert len(reply_markup.inline_keyboard) == 4

    mock_bot.send_video = fake_send_video
    await bot_module.send_welcome_screen(mock_bot, USER_ID)
    assert send_video_called is True
    print("  [PASS] 17. Cinematic welcome remains functional -> sends cached video with 4-row keyboard.")

    # --------------------------------------------------------------------------
    # Scenario 18: Video delivery failure fallback
    # --------------------------------------------------------------------------
    send_text_fallback_called = False
    async def failing_send_video(*args, **kwargs):
        raise TelegramBadRequest(method="sendVideo", message="FILE_ID_INVALID")

    async def fake_send_message(chat_id, text, parse_mode, reply_markup):
        nonlocal send_text_fallback_called
        send_text_fallback_called = True
        assert "Welcome to Kayi Mail" in text
        assert len(reply_markup.inline_keyboard) == 4

    mock_bot.send_video = failing_send_video
    mock_bot.send_message = fake_send_message
    await bot_module.send_welcome_screen(mock_bot, USER_ID)
    assert send_text_fallback_called is True
    print("  [PASS] 18. Video delivery failure fallback -> gracefully falls back to text welcome card.")

    # --------------------------------------------------------------------------
    # Scenario 19: Active mailbox dashboard remains functional
    # --------------------------------------------------------------------------
    # Create active mailbox for user
    await db.save_new_mailbox(USER_ID, "fsub_test@guerrillamail.com", "guerrillamail", "box_fsub", "tok_fsub")
    active_box = await db.get_active_mailbox(USER_ID)
    assert active_box is not None

    mock_cb_active = AsyncMock(spec=types.CallbackQuery)
    mock_cb_active.answer = AsyncMock()
    mock_cb_active.from_user = MagicMock(id=USER_ID)
    mock_cb_active.data = "fsub:verify"
    mock_cb_active.message = AsyncMock(spec=types.Message)
    mock_cb_active.message.edit_text = AsyncMock()
    mock_cb_active.message.video = None
    mock_cb_active.message.photo = None
    mock_cb_active.message.animation = None

    mock_bot.get_chat_member.return_value = make_member("member")
    with patch("fsub.cache", FSubCache(default_ttl=30)):
        await bot_module.cb_fsub_verify(mock_cb_active, bot=mock_bot)
        # Should edit to active mailbox dashboard
        mock_cb_active.message.edit_text.assert_called_once()
        rendered_text = mock_cb_active.message.edit_text.call_args[0][0]
        assert "fsub_test@guerrillamail.com" in rendered_text
        assert "Active" in rendered_text
    print("  [PASS] 19. Active mailbox dashboard remains functional -> displays active inbox card.")

    # Cleanup mailbox
    await db.delete_mailbox(USER_ID)

    # --------------------------------------------------------------------------
    # Scenario 20: No duplicate or unexpected messages
    # --------------------------------------------------------------------------
    # Repeated /start calls by non-member edit or answer without creating runaway loops
    mock_repeat_msg = AsyncMock(spec=types.Message)
    mock_repeat_msg.answer = AsyncMock()
    mock_repeat_msg.chat = MagicMock(type="private")
    mock_repeat_msg.from_user = MagicMock(id=USER_ID)
    mock_bot.get_chat_member.return_value = make_member("left")

    with patch("fsub.cache", FSubCache(default_ttl=30)):
        # Call middleware 5 times
        for _ in range(5):
            await middleware(dummy_handler, mock_repeat_msg, {"event_from_user": mock_repeat_msg.from_user, "bot": mock_bot})
        assert mock_repeat_msg.answer.call_count == 5
    print("  [PASS] 20. No duplicate or unexpected messages -> clean, deterministic lifecycle.")

    print("\n[SUMMARY] ALL 20/20 Mandatory Channel Membership (FSub) Scenarios PASSED successfully!\n")

if __name__ == "__main__":
    asyncio.run(run_fsub_test_suite())
