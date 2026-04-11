"""
TrueWriting Shield - Microsoft 365 Connector (Multi-Tenant MSP)

Supports two auth models:
  1. Per-tenant app registration (customer creates app in their tenant)
  2. Multi-tenant app registration (MSP creates one app, customers grant consent)

Required Graph API permissions (Application):
  - Mail.Read          (read sent items)
  - User.Read.All      (enumerate users)
  - Group.Read.All     (enumerate security groups + memberships)
  - GroupMember.Read.All (read group members)

Admin consent URL for multi-tenant:
  https://login.microsoftonline.com/{customer-tenant}/adminconsent
    ?client_id={your-app-client-id}
    &redirect_uri={your-redirect-uri}
"""

import re
import httpx
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict
from .base import BaseMailConnector, MailUser, SentEmail


GRAPH_BASE = "https://graph.microsoft.com/v1.0"
AUTH_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"


class MicrosoftConnector(BaseMailConnector):

    @property
    def platform_name(self) -> str:
        return "m365"

    async def authenticate(self) -> bool:
        tenant = self.config.get("tenant_id", "")
        client_id = self.config.get("client_id", "")
        client_secret = self.config.get("client_secret", "")

        if not all([tenant, client_id, client_secret]):
            print("  [M365] Missing tenant_id, client_id, or client_secret")
            return False

        url = AUTH_URL.format(tenant=tenant)
        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, data=payload)
            if resp.status_code != 200:
                print(f"  [M365] Auth failed ({self.config.get('tenant_id', '?')}): "
                      f"{resp.status_code} {resp.text[:200]}")
                return False

            data = resp.json()
            self._token = data["access_token"]
            expires_in = data.get("expires_in", 3600)
            self._token_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)
            print(f"  [M365] Authenticated to tenant {tenant[:8]}... Token expires in {expires_in}s")
            return True

    async def _ensure_token(self):
        if not self._token or (self._token_expiry and datetime.now(timezone.utc) >= self._token_expiry):
            await self.authenticate()

    def _headers(self) -> Dict:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    # ── User enumeration ─────────────────────────────────────

    async def list_users(self) -> List[MailUser]:
        """Enumerate all licensed mailbox users with their security group memberships."""
        await self._ensure_token()
        users = []
        url = (f"{GRAPH_BASE}/users?$select=id,displayName,mail,department,jobTitle,"
               f"assignedLicenses&$top=999")

        async with httpx.AsyncClient(timeout=30) as client:
            while url:
                resp = await client.get(url, headers=self._headers())
                if resp.status_code != 200:
                    print(f"  [M365] List users failed: {resp.status_code}")
                    break
                data = resp.json()
                for u in data.get("value", []):
                    email = u.get("mail")
                    if not email:
                        continue
                    if not u.get("assignedLicenses"):
                        continue
                    users.append(MailUser(
                        email=email.lower(),
                        display_name=u.get("displayName", ""),
                        user_id=u["id"],
                        department=u.get("department"),
                        job_title=u.get("jobTitle"),
                    ))
                url = data.get("@odata.nextLink")

        print(f"  [M365] Found {len(users)} licensed mailbox users")
        return users

    async def get_user_groups(self, user_id: str) -> List[Dict]:
        """Get security group memberships for a user."""
        await self._ensure_token()
        groups = []
        url = f"{GRAPH_BASE}/users/{user_id}/memberOf?$select=id,displayName,securityEnabled,groupTypes"

        async with httpx.AsyncClient(timeout=30) as client:
            while url:
                resp = await client.get(url, headers=self._headers())
                if resp.status_code != 200:
                    print(f"  [M365] Get user groups failed for {user_id}: {resp.status_code}")
                    break
                data = resp.json()
                for item in data.get("value", []):
                    # Only include security groups (not distribution lists, M365 groups, etc.)
                    if item.get("@odata.type") == "#microsoft.graph.group":
                        if item.get("securityEnabled"):
                            groups.append({
                                "id": item["id"],
                                "name": item.get("displayName", ""),
                            })
                url = data.get("@odata.nextLink")

        return groups

    async def list_security_groups(self) -> List[Dict]:
        """Enumerate all security groups in the tenant."""
        await self._ensure_token()
        groups = []
        url = (f"{GRAPH_BASE}/groups?$filter=securityEnabled eq true"
               f"&$select=id,displayName,description,membershipRule&$top=999")

        async with httpx.AsyncClient(timeout=30) as client:
            while url:
                resp = await client.get(url, headers=self._headers())
                if resp.status_code != 200:
                    print(f"  [M365] List security groups failed: {resp.status_code}")
                    break
                data = resp.json()
                for g in data.get("value", []):
                    groups.append({
                        "id": g["id"],
                        "name": g.get("displayName", ""),
                        "description": g.get("description", ""),
                    })
                url = data.get("@odata.nextLink")

        print(f"  [M365] Found {len(groups)} security groups")
        return groups

    # ── Sent email retrieval ─────────────────────────────────

    async def get_sent_emails(self, user_id: str, months_back: int = 12,
                              max_emails: int = 2000) -> List[SentEmail]:
        await self._ensure_token()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=months_back * 30)).isoformat()
        emails = []
        url = (
            f"{GRAPH_BASE}/users/{user_id}/mailFolders/SentItems/messages"
            f"?$select=id,subject,body,sentDateTime,toRecipients"
            f"&$filter=sentDateTime ge {cutoff}"
            f"&$orderby=sentDateTime desc"
            f"&$top=100"
        )

        async with httpx.AsyncClient(timeout=60) as client:
            while url and len(emails) < max_emails:
                resp = await client.get(url, headers=self._headers())
                if resp.status_code != 200:
                    print(f"  [M365] Get sent emails failed: {resp.status_code} {resp.text[:200]}")
                    break
                data = resp.json()
                for msg in data.get("value", []):
                    body_content = msg.get("body", {}).get("content", "")
                    content_type = msg.get("body", {}).get("contentType", "text")
                    if content_type.lower() == "html":
                        body_content = self._strip_html(body_content)
                    body_content = self._strip_reply_chain(body_content)
                    body_content = self._strip_signature(body_content)
                    if not body_content or len(body_content.split()) < 5:
                        continue
                    to_addrs = []
                    for recip in msg.get("toRecipients", []):
                        addr = recip.get("emailAddress", {}).get("address", "")
                        if addr:
                            to_addrs.append(addr.lower())
                    sent_dt = None
                    raw_date = msg.get("sentDateTime")
                    if raw_date:
                        try:
                            sent_dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                        except (ValueError, TypeError):
                            pass
                    emails.append(SentEmail(
                        body=body_content,
                        subject=msg.get("subject", ""),
                        sent_at=sent_dt,
                        to_addresses=to_addrs,
                        message_id=msg.get("id", ""),
                    ))
                url = data.get("@odata.nextLink")

        print(f"  [M365] Retrieved {len(emails)} sent emails for user {user_id}")
        return emails

    # ── Watch / Webhook ──────────────────────────────────────

    async def watch_outbound(self, webhook_url: str) -> bool:
        print("  [M365] Webhook subscriptions require per-user setup. Use polling for MVP.")
        return False

    async def renew_watch(self) -> bool:
        return False

    # ── Email cleaning helpers ───────────────────────────────

    @staticmethod
    def _strip_html(html: str) -> str:
        text = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', '', text)
        for entity, char in [('&nbsp;', ' '), ('&amp;', '&'), ('&lt;', '<'),
                             ('&gt;', '>'), ('&quot;', '"'), ('&#39;', "'")]:
            text = text.replace(entity, char)
        text = re.sub(r'&#\d+;', '', text)
        text = re.sub(r'\r\n', '\n', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r'[ \t]{2,}', ' ', text)
        return text.strip()

    @staticmethod
    def _strip_reply_chain(text: str) -> str:
        markers = [
            r'\nOn .+wrote:\s*\n',
            r'\n-{3,}\s*Original Message\s*-{3,}',
            r'\n-{3,}\s*Forwarded\s',
            r'\nFrom:\s+.+\nSent:\s+',
        ]
        for marker in markers:
            match = re.search(marker, text, re.IGNORECASE)
            if match:
                text = text[:match.start()]
        return text.strip()

    @staticmethod
    def _strip_signature(text: str) -> str:
        markers = [
            r'\n--\s*\n',
            r'\n---\s*[A-Z]',
            r'\nSent from my (?:iPhone|iPad|Android)',
            r'\nGet Outlook for ',
        ]
        for marker in markers:
            match = re.search(marker, text, re.IGNORECASE | re.DOTALL)
            if match and match.start() > len(text) * 0.3:
                text = text[:match.start()]
        return text.strip()
