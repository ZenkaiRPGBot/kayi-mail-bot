import os
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# Startup timestamp for uptime calculation
BOT_START_TIME = datetime.now(timezone.utc)

# Telegram Bot Token from @BotFather
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Bot Owner ID
OWNER_ID = int(os.getenv("OWNER_ID", "986380678"))

# Future Shared Log Channel (Optional)
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID", "").strip()

# Brand Identity
BRAND_NAME = "Kayi Mail"
CHANNEL_USERNAME = "@KayiBots"

# Fair-Use & Anti-Abuse Limits
RATE_LIMIT_SECONDS = 60  # Cooldown between generating new email addresses
MAILBOX_LIFETIME_MINUTES = 60  # Default lifetime of temporary mailbox
POLLING_INTERVAL_SECONDS = 5  # Check inbox interval for active listeners
IDLE_TIMEOUT_MINUTES = 15  # Auto-pause background polling if user is inactive

# Web server port for Render keep-alive
PORT = int(os.getenv("PORT", "8080"))
PING_URL = os.getenv("PING_URL") or os.getenv("RENDER_EXTERNAL_URL")
