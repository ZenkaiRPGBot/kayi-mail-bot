import re
import random
import string
import aiohttp

BASE_URL = "https://api.mail.tm"

def generate_random_string(length: int = 8) -> str:
    """Generates clean random alphanumeric string for email prefix."""
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

def extract_verification_code(subject: str = "", body: str = "") -> str | None:
    """
    Intelligently extracts 4-8 digit OTP / confirmation codes from email subject and body.
    Prioritizes keywords like 'code', 'verification', 'otp', 'pin', 'confirm'.
    """
    combined = f"{subject}\n{body}"
    
    # 1. High-confidence patterns near keywords
    keyword_patterns = [
        r'(?:code|otp|pin|verification|security code|confirmation code|login code)[^\d\n\r]{0,20}\b(\d{4,8})\b',
        r'\b(\d{4,8})\b[^\d\n\r]{0,20}(?:is your|is the code|to verify)',
        r'(?:code is:?\s*|otp is:?\s*)\b(\d{4,8})\b'
    ]
    for p in keyword_patterns:
        m = re.search(p, combined, re.IGNORECASE)
        if m:
            code = m.group(1)
            # Avoid matching years like 2024, 2026 if not code
            if len(code) == 4 and (code.startswith("19") or code.startswith("20")):
                continue
            return code

    # 2. Standalone prominent digits in subject
    subject_digits = re.findall(r'\b(\d{4,8})\b', subject)
    for code in subject_digits:
        if len(code) == 4 and (code.startswith("19") or code.startswith("20")):
            continue
        return code

    # 3. Fallback to first standalone 5 to 6 digit number in body
    body_digits = re.findall(r'\b(\d{5,6})\b', body)
    if body_digits:
        return body_digits[0]

    return None

class MailService:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.headers = {
            "Content-Type": "application/json",
            "User-Agent": "KayiMailBot/1.0"
        }

    async def get_domain(self) -> str | None:
        """Fetches active domain from Mail.tm."""
        try:
            async with self.session.get(f"{BASE_URL}/domains", headers=self.headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    members = data.get("hydra:member", [])
                    if members:
                        return members[0].get("domain")
        except Exception as e:
            print(f"[mail_service] Error getting domain: {e}")
        return None

    async def create_mailbox(self) -> dict | None:
        """Creates a fresh temp mailbox and acquires JWT authentication token."""
        domain = await self.get_domain()
        if not domain:
            return None

        username = f"kayi_{generate_random_string(7)}"
        email_address = f"{username}@{domain}"
        password = generate_random_string(14)

        payload = {"address": email_address, "password": password}

        try:
            # 1. Create account
            async with self.session.post(f"{BASE_URL}/accounts", json=payload, headers=self.headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 201:
                    print(f"[mail_service] Failed to create account: {await resp.text()}")
                    return None
                acc_data = await resp.json()
                account_id = acc_data.get("id")

            # 2. Obtain JWT token
            async with self.session.post(f"{BASE_URL}/token", json=payload, headers=self.headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    print(f"[mail_service] Failed to obtain token: {await resp.text()}")
                    return None
                token_data = await resp.json()
                token = token_data.get("token")

            return {
                "email": email_address,
                "token": token,
                "account_id": account_id
            }
        except Exception as e:
            print(f"[mail_service] Exception creating mailbox: {e}")
            return None

    async def get_messages(self, token: str) -> list[dict]:
        """Lists all messages currently in the user's inbox."""
        auth_headers = {**self.headers, "Authorization": f"Bearer {token}"}
        try:
            async with self.session.get(f"{BASE_URL}/messages", headers=auth_headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("hydra:member", [])
        except Exception as e:
            print(f"[mail_service] Error fetching messages: {e}")
        return []

    async def get_message_detail(self, token: str, msg_id: str) -> dict | None:
        """Retrieves full body content and details of a single message."""
        auth_headers = {**self.headers, "Authorization": f"Bearer {token}"}
        try:
            async with self.session.get(f"{BASE_URL}/messages/{msg_id}", headers=auth_headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            print(f"[mail_service] Error fetching message detail {msg_id}: {e}")
        return None

    async def delete_account(self, token: str, account_id: str) -> bool:
        """Deletes user mailbox from Mail.tm servers."""
        if not account_id or not token:
            return False
        auth_headers = {**self.headers, "Authorization": f"Bearer {token}"}
        try:
            async with self.session.delete(f"{BASE_URL}/accounts/{account_id}", headers=auth_headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                return resp.status in [200, 204]
        except Exception as e:
            print(f"[mail_service] Error deleting account {account_id}: {e}")
            return False
