"""
TrueWriting Shield - Base Mail Connector
Abstract interface all platform connectors implement.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from datetime import datetime


@dataclass
class MailUser:
    email: str
    display_name: str
    user_id: str
    department: Optional[str] = None
    job_title: Optional[str] = None


@dataclass
class SentEmail:
    body: str
    subject: str
    sent_at: Optional[datetime] = None
    to_addresses: List[str] = field(default_factory=list)
    message_id: str = ''
    word_count: int = 0

    def __post_init__(self):
        if self.body and not self.word_count:
            self.word_count = len(self.body.split())


class BaseMailConnector(ABC):

    def __init__(self, config: Dict):
        self.config = config
        self._token = None
        self._token_expiry = None

    @abstractmethod
    async def authenticate(self) -> bool:
        pass

    @abstractmethod
    async def list_users(self) -> List[MailUser]:
        pass

    @abstractmethod
    async def get_sent_emails(self, user_id: str, months_back: int = 12,
                              max_emails: int = 2000) -> List[SentEmail]:
        pass

    @abstractmethod
    async def watch_outbound(self, webhook_url: str) -> bool:
        pass

    @abstractmethod
    async def renew_watch(self) -> bool:
        pass

    @property
    @abstractmethod
    def platform_name(self) -> str:
        pass
