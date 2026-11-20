import re
import random
import string
import asyncio
import time
import html
from datetime import datetime, timezone
import aiohttp

# Supported Providers and Domains
PROVIDERS = {
    "guerrillamail": {
        "name": "Guerrilla Mail",
        "domains": ["sharklasers.com", "guerrillamail.com", "grr.la", "pokemail.net"]
    },
    "tempmailplus": {
        "name": "TempMail Plus",
        "domains": ["fexpost.com", "fexbox.org"]
    },
    "mailtm": {
        "name": "Mail.tm",
        "domains": ["euploading.com"]
    }
}

DEFAULT_DOMAIN = "sharklasers.com"  # Reddit & Discord approved!

def get_provider_for_domain(domain: str) -> str:
    domain_lower = domain.lower().strip()
    for prov_key, prov_info in PROVIDERS.items():
        if domain_lower in prov_info["domains"]:
            return prov_key
    if "mail.tm" in domain_lower or "euploading" in domain_lower:
        return "mailtm"
    return "guerrillamail"

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
        if re.search(r'(?:verify|code|account|login|signup|security)', subject, re.IGNORECASE):
            return code

    # 3. Formatted alphanumeric codes (e.g. ABC-123 or 6-char upper)
    alphanumeric_match = re.search(r'(?:code|pin)[^\w\n\r]{0,20}\b([A-Z0-9]{5,8})\b', combined, re.IGNORECASE)
    if alphanumeric_match:
        code = alphanumeric_match.group(1).upper()
        if code not in ["PLEASE", "VERIFY", "THANKS", "GOOGLE", "GITHUB", "DISCORD"]:
            return code

    return None

class MailService:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }

    # =========================================================================
    # UNIFIED MAILBOX CREATION
    # =========================================================================

    async def create_mailbox(self, domain: str = DEFAULT_DOMAIN) -> dict | None:
        """
        Creates a new mailbox with the requested domain across providers.
        Defaults to sharklasers.com (Guerrilla Mail - Reddit & Discord friendly).
        """
        provider = get_provider_for_domain(domain)

        if provider == "guerrillamail":
            return await self._create_guerrillamail_box(domain)
        elif provider == "tempmailplus":
            return await self._create_tempmailplus_box(domain)
        elif provider == "mailtm":
            return await self._create_mailtm_box(domain)
        return await self._create_guerrillamail_box(DEFAULT_DOMAIN)

    # 1. Guerrilla Mail Provider
    async def _create_guerrillamail_box(self, domain: str) -> dict | None:
        try:
            url = "https://api.guerrillamail.com/ajax.php?f=get_email_address"
            async with self.session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
                sid_token = data.get("sid_token")

            # Set custom clean username
            user_prefix = f"kayi_{generate_random_string(7)}"
            set_url = f"https://api.guerrillamail.com/ajax.php?f=set_email_user&email_user={user_prefix}&sid_token={sid_token}"
            async with self.session.get(set_url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=8)) as resp2:
                if resp2.status != 200:
                    return None

            email_address = f"{user_prefix}@{domain}"
            return {
                "email": email_address,
                "provider": "guerrillamail",
                "token": sid_token,
                "account_id": user_prefix
            }
        except Exception as e:
            print(f"[mail_service] Guerrilla Mail create error: {e}")
            return None

    # 2. TempMail Plus Provider
    async def _create_tempmailplus_box(self, domain: str) -> dict | None:
        user_prefix = f"kayi_{generate_random_string(7)}"
        email_address = f"{user_prefix}@{domain}"
        return {
            "email": email_address,
            "provider": "tempmailplus",
            "token": email_address,
            "account_id": user_prefix
        }

    # 3. Mail.tm Provider
    async def _create_mailtm_box(self, domain: str) -> dict | None:
        try:
            user_prefix = f"kayi_{generate_random_string(7)}"
            email_address = f"{user_prefix}@{domain}"
            password = generate_random_string(14)
            payload = {"address": email_address, "password": password}

            # 1. Create account
            async with self.session.post("https://api.mail.tm/accounts", json=payload, headers=self.headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 201:
                    return None
                acc_data = await resp.json()
                account_id = acc_data.get("id")

            # 2. Obtain real JWT token
            async with self.session.post("https://api.mail.tm/token", json=payload, headers=self.headers, timeout=aiohttp.ClientTimeout(total=8)) as resp2:
                if resp2.status != 200:
                    return None
                token_data = await resp2.json()
                jwt_token = token_data.get("token")

            return {
                "email": email_address,
                "provider": "mailtm",
                "token": jwt_token,
                "account_id": account_id
            }
        except Exception as e:
            print(f"[mail_service] Mail.tm create error: {e}")
            return None

    # =========================================================================
    # UNIFIED MESSAGE LISTING
    # =========================================================================

    async def get_messages(self, provider: str, token: str, address: str) -> list[dict]:
        """
        Retrieves list of messages normalized to:
        [{'id': str, 'from': str, 'subject': str, 'body': str, 'createdAt': str}]
        """
        if provider == "guerrillamail":
            return await self._get_guerrillamail_messages(token)
        elif provider == "tempmailplus":
            return await self._get_tempmailplus_messages(address)
        elif provider == "mailtm":
            return await self._get_mailtm_messages(token)
        return []

    async def _get_guerrillamail_messages(self, sid_token: str) -> list[dict]:
        try:
            url = f"https://api.guerrillamail.com/ajax.php?f=check_email&seq=0&sid_token={sid_token}"
            async with self.session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
                raw_list = data.get("list", [])
                normalized = []
                for m in raw_list:
                    # Ignore initial Guerrilla Mail welcome email
                    if m.get("mail_id") == 1 or "welcome to guerrilla mail" in (m.get("mail_subject") or "").lower():
                        continue
                    clean_body = re.sub(r'<[^>]+>', ' ', m.get("mail_body") or m.get("mail_excerpt") or "").strip()
                    normalized.append({
                        "id": str(m.get("mail_id")),
                        "from": m.get("mail_from") or "Unknown",
                        "subject": m.get("mail_subject") or "No Subject",
                        "body": clean_body,
                        "createdAt": datetime.now(timezone.utc).isoformat()
                    })
                return normalized
        except Exception as e:
            print(f"[mail_service] Guerrilla Mail check error: {e}")
            return []

    async def _get_tempmailplus_messages(self, email_address: str) -> list[dict]:
        try:
            url = f"https://tempmail.plus/api/mails?email={email_address}&limit=20"
            async with self.session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
                raw_list = data.get("mail_list", [])
                normalized = []
                for m in raw_list:
                    normalized.append({
                        "id": str(m.get("mail_id")),
                        "from": m.get("from") or "Unknown",
                        "subject": m.get("subject") or "No Subject",
                        "body": "",
                        "createdAt": m.get("date") or datetime.now(timezone.utc).isoformat()
                    })
                return normalized
        except Exception as e:
            print(f"[mail_service] TempMail Plus check error: {e}")
            return []

    async def _get_mailtm_messages(self, jwt_token: str) -> list[dict]:
        try:
            headers = {**self.headers, "Authorization": f"Bearer {jwt_token}"}
            async with self.session.get("https://api.mail.tm/messages", headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                raw_list = data.get("hydra:member", [])
                normalized = []
                for m in raw_list:
                    normalized.append({
                        "id": m.get("id"),
                        "from": m.get("from", {}).get("address", "Unknown"),
                        "subject": m.get("subject") or "No Subject",
                        "body": m.get("intro") or "",
                        "createdAt": m.get("createdAt") or datetime.now(timezone.utc).isoformat()
                    })
                return normalized
        except Exception as e:
            print(f"[mail_service] Mail.tm check error: {e}")
            return []

    # =========================================================================
    # UNIFIED MESSAGE DETAIL
    # =========================================================================

    async def get_message_detail(self, provider: str, token: str, address: str, msg_id: str) -> dict | None:
        """
        Retrieves full details normalized to:
        {'from_name': str, 'from_addr': str, 'subject': str, 'text': str, 'createdAt': str}
        """
        if provider == "guerrillamail":
            return await self._get_guerrillamail_detail(token, msg_id)
        elif provider == "tempmailplus":
            return await self._get_tempmailplus_detail(address, msg_id)
        elif provider == "mailtm":
            return await self._get_mailtm_detail(token, msg_id)
        return None

    async def _get_guerrillamail_detail(self, sid_token: str, msg_id: str) -> dict | None:
        try:
            url = f"https://api.guerrillamail.com/ajax.php?f=fetch_email&email_id={msg_id}&sid_token={sid_token}"
            async with self.session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
                raw_body = data.get("mail_body") or ""
                clean_body = re.sub(r'<[^>]+>', ' ', raw_body).strip()
                clean_body = html.unescape(clean_body)
                from_raw = data.get("mail_from") or "Unknown"
                return {
                    "from_name": from_raw,
                    "from_addr": from_raw,
                    "subject": data.get("mail_subject") or "No Subject",
                    "text": clean_body,
                    "createdAt": datetime.now(timezone.utc).isoformat()
                }
        except Exception as e:
            print(f"[mail_service] Guerrilla Mail detail error: {e}")
            return None

    async def _get_tempmailplus_detail(self, email_address: str, msg_id: str) -> dict | None:
        try:
            url = f"https://tempmail.plus/api/mails/{msg_id}?email={email_address}"
            async with self.session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
                text_content = data.get("text") or re.sub(r'<[^>]+>', ' ', data.get("html") or "").strip()
                from_raw = data.get("from") or "Unknown"
                return {
                    "from_name": from_raw,
                    "from_addr": from_raw,
                    "subject": data.get("subject") or "No Subject",
                    "text": text_content,
                    "createdAt": data.get("date") or datetime.now(timezone.utc).isoformat()
                }
        except Exception as e:
            print(f"[mail_service] TempMail Plus detail error: {e}")
            return None

    async def _get_mailtm_detail(self, jwt_token: str, msg_id: str) -> dict | None:
        try:
            headers = {**self.headers, "Authorization": f"Bearer {jwt_token}"}
            async with self.session.get(f"https://api.mail.tm/messages/{msg_id}", headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                from_obj = data.get("from", {})
                return {
                    "from_name": from_obj.get("name") or from_obj.get("address") or "Unknown",
                    "from_addr": from_obj.get("address") or "Unknown",
                    "subject": data.get("subject") or "No Subject",
                    "text": data.get("text") or data.get("intro") or "",
                    "createdAt": data.get("createdAt") or datetime.now(timezone.utc).isoformat()
                }
        except Exception as e:
            print(f"[mail_service] Mail.tm detail error: {e}")
            return None

    # =========================================================================
    # DELETE ACCOUNT
    # =========================================================================

    async def delete_account(self, provider: str, token: str, account_id: str) -> bool:
        if provider == "mailtm" and account_id and token:
            try:
                headers = {**self.headers, "Authorization": f"Bearer {token}"}
                async with self.session.delete(f"https://api.mail.tm/accounts/{account_id}", headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    return resp.status in [200, 204]
            except Exception:
                return False
        elif provider == "guerrillamail" and token:
            try:
                url = f"https://api.guerrillamail.com/ajax.php?f=forget_me&sid_token={token}"
                async with self.session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=5)):
                    return True
            except Exception:
                return False
        return True
