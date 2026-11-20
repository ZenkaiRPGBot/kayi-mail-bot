import os
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# Indian Standard Time (UTC + 05:30)
IST = timezone(timedelta(hours=5, minutes=30))

BOT_START_TIME = datetime.now(IST)

# Telegram Credentials
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "986380678"))
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID", "").strip()

# Branding & Ecosystem
BRAND_NAME = "Kayi Mail"
COMMUNITY_URL = "https://t.me/KayiBots"

# Mailbox Lifecycle & Limits (Spec v1.0)
DEFAULT_LIFETIME_MINUTES = 60          # 60m default mailbox lifetime
EXTENSION_INCREMENT_MINUTES = 30       # +30m per extension
MAX_TOTAL_LIFETIME_MINUTES = 120       # 120m max lifetime cap
MAX_EXTENSIONS_PER_MAILBOX = 2         # Maximum 2 extensions allowed per mailbox

# Anti-Abuse Rate Limits
RATE_LIMIT_COOLDOWN_SECONDS = 60       # 60s cooldown between address creations
HOURLY_CREATION_LIMIT = 5              # Max 5 new addresses per rolling hour
DAILY_CREATION_LIMIT = 15              # Max 15 new addresses per rolling 24 hours

# UI & Pagination
INBOX_PAGE_SIZE = 5                    # 5 messages per page in /inbox

# Global API Limiting (Mail.tm protection)
GLOBAL_API_RATE_LIMIT = 4              # 4 requests/sec ceiling (leaves 50% headroom below 8 req/s limit)
GLOBAL_BURST_CAPACITY = 4              # Max burst capacity
GLOBAL_ACCOUNT_CREATION_COOLDOWN = 10  # Serialization spacing for account creation

# Adaptive Polling Schedule (seconds)
INTERVAL_NEW_BOX = 5                   # First 3 minutes: priority checking (5s)
INTERVAL_VERIFY_WINDOW = 10            # 3 to 5 minutes: active verification window (10s)
INTERVAL_ACTIVE = 30                   # 5 to 15 minutes: normal active (30s)
INTERVAL_IDLE_15M = 60                 # 15 to 30 minutes idle: relaxed checking (60s)
INTERVAL_IDLE_30M = 120                # 30+ minutes idle: infrequent check (120s)

# Web server for keep-alive
PORT = int(os.getenv("PORT", "8080"))
PING_URL = os.getenv("PING_URL") or os.getenv("RENDER_EXTERNAL_URL")
if not PING_URL and os.getenv("RENDER"):
    PING_URL = "https://kayi-mail-bot.onrender.com"

# Render Cloud Hosting Metadata
RENDER_EMAIL = os.getenv("RENDER_EMAIL", "giyanrtjd@gmail.com")
RENDER_SERVICE_NAME = os.getenv("RENDER_SERVICE_NAME", "kayi-mail-bot")
RENDER_REPO = "ZenkaiRPGBot/kayi-mail-bot"
RENDER_USED_HOURS = float(os.getenv("RENDER_USED_HOURS", "0.0"))
