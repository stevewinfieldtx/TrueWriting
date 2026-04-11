"""
TrueWriting Analyzer - Core Analysis Engine v3
Analyzes communication to generate a TW-0 voice profile.

Supports input types:
  - Email: .mbox, .eml directory, .pst (Windows with Outlook), live Outlook
  - Transcripts: video transcripts, call transcripts, podcast transcripts
  - API: JSON array of text segments (any source)

Key capability: Phrase-level fingerprinting - captures HOW someone
expresses concepts, not just what they write about.

Output: TW-0 profile JSON with phrase fingerprint data
"""

import mailbox
import email
import os
import re
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from email.utils import parseaddr, parsedate_to_datetime

import textstat
import nltk
from nltk.tokenize import sent_tokenize, word_tokenize
from nltk.corpus import stopwords

# Win32com for PST support (Windows only)
try:
    import win32com.client
    import pythoncom
    HAS_WIN32COM = True
except ImportError:
    HAS_WIN32COM = False

# Ensure NLTK data
def ensure_nltk_data():
    for path, name in {
        'tokenizers/punkt': 'punkt',
        'tokenizers/punkt_tab': 'punkt_tab',
        'corpora/stopwords': 'stopwords',
        'taggers/averaged_perceptron_tagger': 'averaged_perceptron_tagger',
        'taggers/averaged_perceptron_tagger_eng': 'averaged_perceptron_tagger_eng',
    }.items():
        try:
            nltk.data.find(path)
        except LookupError:
            nltk.download(name, quiet=True)

ensure_nltk_data()


# ============================================================
#  DATA STRUCTURES
# ============================================================

class ContentMessage:
    """Universal message container. Works for email, transcript, or any text.
    All analyzers work against .body — the source_type determines
    which metadata fields are populated."""

    def __init__(self, body, subject='', to_addresses=None, date=None,
                 message_id=None, source_id='', source_type='email',
                 speaker=None, title=''):
        self.body = body
        self.subject = subject or title
        self.to_addresses = to_addresses or []
        self.date = date or datetime.now()
        self.message_id = message_id or source_id
        self.word_count = len(body.split()) if body else 0
        self.source_id = source_id
        self.source_type = source_type  # 'email', 'transcript', 'call', 'text'
        self.speaker = speaker
        self.title = title


# Keep backward compat aliases
EmailMessage = ContentMessage


class PhraseInstance:
    def __init__(self, phrase, full_sentence, recipient_domain, date, position_in_email):
        self.phrase = phrase
        self.full_sentence = full_sentence
        self.recipient_domain = recipient_domain  # doubles as source_id for transcripts
        self.date = date
        self.position_in_email = position_in_email  # opening/body/closing works for any content


# ============================================================
#  TRANSCRIPT INGESTION
# ============================================================

class TranscriptIngester:
    """Ingests transcripts from API calls, JSON, or raw text arrays."""

    @classmethod
    def from_texts(cls, texts, source_ids=None, titles=None, dates=None, speakers=None):
        """Create ContentMessage objects from raw text arrays.
        This is what TrueEngine calls via the API."""
        messages = []
        for i, text in enumerate(texts):
            if not text or not text.strip():
                continue
            cleaned = cls._clean_transcript(text)
            if len(cleaned.split()) < 10:
                continue

            sid = source_ids[i] if source_ids and i < len(source_ids) else f'src_{i}'
            title = titles[i] if titles and i < len(titles) else ''
            date = None
            if dates and i < len(dates):
                d = dates[i]
                if isinstance(d, str):
                    try:
                        date = datetime.fromisoformat(d.replace('Z', '+00:00')).replace(tzinfo=None)
                    except:
                        date = None
                elif isinstance(d, datetime):
                    date = d
            speaker = speakers[i] if speakers and i < len(speakers) else None

            messages.append(ContentMessage(
                body=cleaned, source_id=sid, title=title,
                date=date, speaker=speaker, source_type='transcript'
            ))

        total_words = sum(m.word_count for m in messages)
        print(f"  Ingested {len(messages)} transcript segments ({total_words:,} words)")
        return messages

    @classmethod
    def from_json(cls, data):
        """Load from parsed JSON (list of objects or strings).
        Accepts the same format TrueEngine sends via API."""
        if not data:
            return []
        if isinstance(data[0], str):
            return cls.from_texts(data)
        texts = [d.get('text', '') for d in data]
        source_ids = [d.get('source_id', '') for d in data]
        titles = [d.get('title', '') for d in data]
        dates = [d.get('date') for d in data]
        speakers = [d.get('speaker') for d in data]
        return cls.from_texts(texts, source_ids, titles, dates, speakers)

    @staticmethod
    def _clean_transcript(text):
        """Clean transcript text — remove timestamps, collapse whitespace."""
        text = re.sub(r'[\[\(]\d{1,2}:\d{2}(?::\d{2})?[\]\)]', '', text)
        text = re.sub(r'^\s*[A-Z][A-Za-z\s]{0,20}:\s*', '', text, flags=re.MULTILINE)
        text = re.sub(r'\s+', ' ', text).strip()
        return text


# ============================================================
#  EMAIL INGESTION
# ============================================================
