# 🦅 Kayi Mail Bot — Instant Disposable Inbox

A lightweight, high-performance Telegram bot that gives users instant temporary disposable email addresses with real-time push notifications, smart OTP detection, and anti-abuse rate limiting. Part of the **@KayiBots** suite.

---

## 🌟 Key Features
- ⚡ **Instant Email Generation:** Creates a real, working disposable email in < 0.5s.
- 📬 **Real-Time Push Notifications:** Automatically pings users the second an email or verification code arrives.
- 🔑 **Smart OTP Extractor:** Regex engine automatically detects 4-8 digit verification codes and presents a 1-tap copy button.
- 🛡️ **Anti-Abuse Engine:**
  - 1 active mailbox per user at a time.
  - 60-second generation cooldown to prevent bot spam.
  - 60-minute mailbox lifetime with interactive `[ ⏳ Extend +30m ]`.
  - Smart dynamic polling (auto-pauses after 15m of inactivity).
- 🕒 **24/7 Render Keep-Alive:** Built-in web server with a 10-minute self-ping loop.
- 📢 **Ecosystem Integration:** Direct channel button to `@KayiBots`.

---

## 🛠️ Local Setup
1. Create a bot with [@BotFather](https://t.me/BotFather) (e.g. `@KayiMailBot`).
2. Add your token to `.env`:
   ```env
   BOT_TOKEN=your_bot_token_here
   OWNER_ID=986380678
   PORT=8080
   PING_URL=
   ```
3. Run:
   ```bash
   python bot.py
   ```
