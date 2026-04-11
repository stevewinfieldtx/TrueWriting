"""
TrueWriting Shield - Google Workspace Connector (Placeholder)
Will use Gmail API with service account + domain-wide delegation.

Setup:
  1. Google Cloud project → enable Gmail API + Admin SDK
  2. Service account with domain-wide delegation
  3. Scopes: gmail.readonly, admin.directory.user.readonly, admin.directory.group.readonly
"""

from .base import BaseMailConnector, MailUser, SentEmail
from typing import List, Dict


class GoogleConnector(BaseMailConnector):

    @property
    def platform_name(self) -> str:
        return "google"

    async def authenticate(self) -> bool:
        print("  [Google] Connector not yet implemented.")
        return False

    async def list_users(self) -> List[MailUser]:
        return []

    async def get_sent_emails(self, user_id: str, months_back: int = 12,
                              max_emails: int = 2000) -> List[SentEmail]:
        return []

    async def watch_outbound(self, webhook_url: str) -> bool:
        return False

    async def renew_watch(self) -> bool:
        return False
