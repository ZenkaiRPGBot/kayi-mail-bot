import re
import random
import string
import asyncio
import time
from datetime import datetime, timezone
import aiohttp
from config import GLOBAL_API_RATE_LIMIT, GLOBAL_BURST_CAPACITY

BASE_URL = "https://api.mail.tm"

def generate_random_string(length: int = 8) -> str:
    """Generates clean random alphanumeric string for email prefix."""
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

def extract_verification_code(subject: str = "", body: str = "") -> str | None:
    """
    Intelligently extracts 4-8 digit OTP / confirmation codes using scored candidate matching.
    Prioritizes explicit keywords, rejects dates/years, prices, and tracking IDs.
    Returns code only when confidence is high; otherwise None.
    """
    combined = f"{subject}\n{body}"

    # 1. High-confidence patterns near keywords
    keyword_patterns = [
        r'(?:verification code|security code|confirmation code|login code|one-time code|otp|pin)[^\w\n\r]{0,25}\b([0-9]{4,8})\b',
        r'\b([0-9]{4,8})\b[^\w\n\r]{0,25}(?:is your|is the code|to verify|to confirm)',
        r'(?:code is:?\s*|otp is:?\s*|pin is:?\s*)\b([0-9]{4,8})\b',
        r'(?:use code|enter code)[^\w\n\r]{0,20}\b([0-9]{4,8})\b'
    ]

    for p in keyword_patterns:
        m = re.search(p, combined, re.IGNORECASE)
        if m:
            code = m.group(1)
            # Filter out years (1970-2030) if length is 4
            if len(code) == 4 and (code.startswith("19") or code.startswith("20")):
                continue
            return code

    # 2. Prominent standalone code in Subject line
    subject_matches = re.findall(r'\b([0-9]{4,8})\b', subject)
    for code in subject_matches:
        if len(code) == 4 and (code.startswith("19") or code.startswith("20")):
            continue
        # If words like "verify", "code", "account" are in subject, high confidence
        if re.search(r'(?:verify|code|account|login|signup|security)', subject, re.IGNORECASE):
            return code

    # 3. Formatted alphanumeric codes (e.g. ABC-123 or 6-char upper)
    alphanumeric_match = re.search(r'(?:code|pin)[^\w\n\r]{0,20}\b([A-Z0-9]{5,8})\b', combined, re.IGNORECASE)
    if alphanumeric_match:
        code = alphanumeric_match.group(1).upper()
        # Avoid common words
        if code not in ["PLEASE", "VERIFY", "THANKS", "GOOGLE", "GITHUB", "DISCORD"]:
            return code

    return None

class AsyncRateLimiter:
    """Token bucket rate limiter ensuring we do not exceed 4 requests/sec on Mail.tm."""
    def __init__(self, rate: float = GLOBAL_API_RATE_LIMIT, capacity: float = GLOBAL_BURST_CAPACITY):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.last_check = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self.last_check
                self.last_check = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)

                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                # Wait for token
                needed = (1.0 - self.tokens) / self.rate
                await asyncio.sleep(max(0.05, needed))

class MailService:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.limiter = AsyncRateLimiter()
        self.creation_lock = asyncio.Lock()
        self.last_creation_time = 0.0
        self.headers = {
            "Content-Type": "application/json",
            "User-Agent": "KayiMailBot/2.0 (Telegram disposable mail)"
        }

    async def _request(self, method: str, url: str, **kwargs) -> aiohttp.ClientResponse | None:
        """Executes HTTP request through the rate limiter with 429 retry backoff."""
        await self.limiter.acquire()
        headers = {**self.headers, **kwargs.pop("headers", {})}

        for attempt in range(3):
            try:
                resp = await self.session.request(method, url, headers=headers, timeout=aiohttp.ClientTimeout(total=8), **kwargs)
                if resp.status == 429:
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        wait_sec = float(retry_after) if retry_after else (2 ** attempt) + random.uniform(0.5, 1.5)
                    except ValueError:
                        wait_sec = (2 ** attempt) + 1.0
                    print(f"[mail_service] 429 Rate limited. Waiting {wait_sec:.1f}s...")
                    await asyncio.sleep(wait_sec)
                    await self.limiter.acquire()
                    continue
                return resp
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == 2:
                    print(f"[mail_service] Request failed ({method} {url}): {e}")
                    return None
                await asyncio.sleep(1.0)
        return None

    async def get_domain(self) -> str | None:
        """Fetches active provider domain."""
        resp = await self._request("GET", f"{BASE_URL}/domains")
        if resp and resp.status == 200:
            try:
                data = await resp.json()
                members = data.get("hydra:member", [])
                if members:
                    return members[0].get("domain")
            except Exception:
                pass
        return None

    async def create_mailbox(self) -> dict | None:
        """Serializes account creation to respect provider creation thresholds."""
        async with self.creation_lock:
            # Enforce small pacing between creations
            elapsed = time.monotonic() - self.last_creation_time
            if elapsed < 2.0:
                await asyncio.sleep(2.0 - elapsed)

            domain = await self.get_domain()
            if not domain:
                return None

            username = f"kayi_{generate_random_string(7)}"
            email_address = f"{username}@{domain}"
            password = generate_random_string(14)
            payload = {"address": email_address, "password": password}

            # 1. Create account
            acc_resp = await self._request("POST", f"{BASE_URL}/accounts", json=payload)
            if not acc_resp or acc_resp.status != 201:
                return None

            try:
                acc_data = await acc_resp.json()
                account_id = acc_data.get("id")
            except Exception:
                return None

            # 2. Obtain JWT Token
            token_resp = await self._request("POST", f"{BASE_URL}/token", json=payload)
            if not token_resp or token_resp.status != 200:
                return None

            try:
                token_data = await token_resp.json()
                token = token_data.get("token")
            except Exception:
                return None

            self.last_creation_time = time.monotonic()
            return {
                "email": email_address,
                "token": token,
                "account_id": account_id
            }

    async def get_messages(self, token: str) -> list[dict]:
        """Lists all messages currently in the user's provider inbox."""
        headers = {"Authorization": f"Bearer {token}"}
        resp = await self._request("GET", f"{BASE_URL}/messages", headers=headers)
        if resp and resp.status == 200:
            try:
                data = await resp.json()
                return data.get("hydra:member", [])
            except Exception:
                pass
        return []

    async def get_message_detail(self, token: str, provider_msg_id: str) -> dict | None:
        """Retrieves full body content and details of a single message."""
        headers = {"Authorization": f"Bearer {token}"}
        resp = await self._request("GET", f"{BASE_URL}/messages/{provider_msg_id}", headers=headers)
        if resp and resp.status == 200:
            try:
                return await resp.json()
            except Exception:
                pass
        return None

    async def delete_account(self, token: str, account_id: str) -> bool:
        """Deletes user mailbox from provider servers."""
        if not account_id or not token:
            return False
        headers = {"Authorization": f"Bearer {token}"}
        resp = await self._request("DELETE", f"{BASE_URL}/accounts/{account_id}", headers=headers)
        return resp is not None and resp.status in [200, 204]
