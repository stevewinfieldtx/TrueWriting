"""
TrueWriting Analyzer - Core Analysis Engine v3
Analyzes communication to generate a TW-0 voice profile.

Supports:
  - Email: .mbox, .eml directory, .pst, live Outlook
  - Transcripts: video, call, podcast (via API or JSON)
  - API: POST /analyze with JSON text array

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
    All analyzers work against .body — source_type determines metadata."""

    def __init__(self, body='', subject='', to_addresses=None, date=None,
                 message_id=None, source_id='', source_type='email',
                 speaker=None, title=''):
        self.body = body
        self.subject = subject or title
        self.to_addresses = to_addresses or []
        self.date = date
        self.message_id = message_id or source_id
        self.word_count = len(body.split()) if body else 0
        self.source_id = source_id
        self.source_type = source_type
        self.speaker = speaker
        self.title = title

# Backward compat
EmailMessage = ContentMessage


class PhraseInstance:
    def __init__(self, phrase, full_sentence, recipient_domain, date, position_in_email):
        self.phrase = phrase
        self.full_sentence = full_sentence
        self.recipient_domain = recipient_domain
        self.date = date
        self.position_in_email = position_in_email




# ============================================================
#  TRANSCRIPT INGESTION
# ============================================================

class TranscriptIngester:
    """Ingests transcripts from API calls, JSON, or raw text arrays."""

    @classmethod
    def from_texts(cls, texts, source_ids=None, titles=None, dates=None, speakers=None):
        """Create ContentMessage objects from raw text arrays.
        This is what TrueEngine and other services call via the API."""
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
                    except: date = None
                elif isinstance(d, datetime): date = d
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
        """Load from parsed JSON (list of objects or strings)."""
        if not data: return []
        if isinstance(data[0], str): return cls.from_texts(data)
        return cls.from_texts(
            [d.get('text', '') for d in data],
            [d.get('source_id', '') for d in data],
            [d.get('title', '') for d in data],
            [d.get('date') for d in data],
            [d.get('speaker') for d in data],
        )

    @staticmethod
    def _clean_transcript(text):
        text = re.sub(r'[\[\(]\d{1,2}:\d{2}(?::\d{2})?[\]\)]', '', text)
        text = re.sub(r'^\s*[A-Z][A-Za-z\s]{0,20}:\s*', '', text, flags=re.MULTILINE)
        text = re.sub(r'\s+', ' ', text).strip()
        return text


# ============================================================
#  EMAIL INGESTION
# ============================================================

class EmailIngester:

    @staticmethod
    def _strip_quotes(body):
        """Strip quoted replies, forwarded content, and signatures from email body.

        Handles:
        - Standard '>' quoting
        - Outlook "On ... wrote:" and "Original Message" blocks
        - Outlook From/Sent/To/Subject reply headers
        - Forwarded message blocks
        - Email signatures (--- delimiter, -- delimiter, name+title blocks)
        - Boilerplate footers (Sent from iPhone, Get Outlook, etc.)
        - \r\n line endings from Outlook COM
        """
        # Normalize line endings first
        body = body.replace('\r\n', '\n').replace('\r', '\n')

        lines = body.split('\n')
        cleaned = []
        for idx, line in enumerate(lines):
            stripped = line.strip()

            # Skip blank lines at the very start
            if not cleaned and not stripped:
                continue

            # Skip '>' quoted lines
            if stripped.startswith('>'):
                continue

            # --- REPLY / FORWARD DETECTION (stop here, everything below is quoted) ---

            # "On <date> <person> wrote:"
            if re.match(r'^On .+ wrote:\s*$', stripped):
                break

            # Outlook: "-----Original Message-----"
            if re.match(r'^-{3,}\s*Original Message\s*-{3,}', stripped, re.IGNORECASE):
                break

            # Outlook: "-----Forwarded Message-----"
            if re.match(r'^-{3,}\s*Forwarded\s', stripped, re.IGNORECASE):
                break

            # Outlook reply header block: "From: ... Sent: ... To: ... Subject: ..."
            # Detect "From:" followed shortly by "Sent:" — this is a reply header, not body content
            if re.match(r'^From:', stripped, re.IGNORECASE) and len(cleaned) > 2:
                # Look ahead for Sent:/To:/Subject: within next 4 lines
                lookahead = [lines[idx+j].strip() for j in range(1, min(5, len(lines) - idx))]
                if any(re.match(r'^(Sent|To|Subject|Date|Cc):', la, re.IGNORECASE) for la in lookahead):
                    break

            # Underscores separator (Outlook web)
            if re.match(r'^_{3,}\s*$', stripped) and len(cleaned) > 2:
                break

            # Gmail forwarded
            if re.match(r'^-{2,}\s*Forwarded message\s*-{2,}', stripped, re.IGNORECASE):
                break

            # --- SIGNATURE DETECTION (stop here, everything below is signature) ---

            # "---Name" or "--- Name" (common personal delimiter)
            if re.match(r'^-{2,3}\s*[A-Z][a-z]', stripped):
                break

            # "-- " standard email signature delimiter (RFC 3676)
            if stripped == '--':
                break

            cleaned.append(line)

        body = '\n'.join(cleaned).strip()

        # Remove trailing signature blocks that don't use a delimiter
        # Pattern: name line + title line + company line + phone/email lines at end
        sig_patterns = [
            r'\n--\s*\n.*',                                          # -- delimiter
            r'\nSent from my (?:iPhone|iPad|Android|Galaxy).*',     # mobile
            r'\nGet Outlook for .*',                                 # Outlook mobile
            r'\nSent via .*',                                        # generic mobile
        ]
        for pattern in sig_patterns:
            body = re.split(pattern, body, flags=re.DOTALL)[0]

        # Strip trailing signature block: if the last few lines look like
        # a sig (short lines with phone numbers, email addresses, URLs, titles)
        lines = body.split('\n')
        sig_start = None
        for i in range(len(lines) - 1, max(len(lines) - 12, -1), -1):
            line = lines[i].strip()
            if not line:
                continue
            # Lines that are signature-like: phone, email, URL, title, short name
            is_sig_line = bool(
                re.match(r'^[\s\S]*[\(\)0-9\-\+]{7,}', line) or        # phone number
                re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', line) or         # email address
                re.match(r'^https?://', line) or                         # URL
                re.match(r'^\s*\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}', line) or  # US phone
                (len(line.split()) <= 4 and re.match(r'^[A-Z]', line) and
                 any(t in line.lower() for t in ['founder', 'partner', 'ceo', 'cto', 'director',
                     'manager', 'president', 'vp ', 'vice president', 'consultant',
                     'managing', 'principal', 'owner', 'chief']))       # title line
            )
            if is_sig_line:
                sig_start = i
            else:
                break

        if sig_start is not None:
            # Walk back to trim empty lines and the name line right before the sig
            while sig_start > 0 and not lines[sig_start - 1].strip():
                sig_start -= 1
            body = '\n'.join(lines[:sig_start]).strip()

        return body.strip()

    @staticmethod
    def _resolve_outlook_recipients(item):
        """Extract recipient email addresses from an Outlook COM mail item.

        Uses item.Recipients collection which provides SMTP addresses,
        falling back to item.To display names if needed.
        """
        to_addresses = []
        try:
            # Try Recipients collection first — gives us actual SMTP addresses
            recips = item.Recipients
            for r in range(1, recips.Count + 1):
                try:
                    recip = recips.Item(r)
                    # Type 1 = To, 2 = CC, 3 = BCC — we only want To
                    if recip.Type != 1:
                        continue
                    name = recip.Name or ''
                    # Try to get SMTP address
                    smtp = ''
                    try:
                        # Exchange addresses need PropertyAccessor
                        addr_entry = recip.AddressEntry
                        if addr_entry.Type == 'SMTP':
                            smtp = addr_entry.Address.lower()
                        elif addr_entry.Type == 'EX':
                            # Exchange user — get SMTP via PropertyAccessor
                            try:
                                smtp = addr_entry.GetExchangeUser().PrimarySmtpAddress.lower()
                            except Exception:
                                try:
                                    PR_SMTP = "http://schemas.microsoft.com/mapi/proptag/0x39FE001E"
                                    smtp = addr_entry.PropertyAccessor.GetProperty(PR_SMTP).lower()
                                except Exception:
                                    smtp = addr_entry.Address.lower()
                        else:
                            smtp = addr_entry.Address.lower()
                    except Exception:
                        smtp = recip.Address.lower() if recip.Address else ''

                    if smtp and '@' in smtp:
                        to_addresses.append((name, smtp))
                    elif name:
                        to_addresses.append((name, name.lower()))
                except Exception:
                    continue
        except Exception:
            # Fallback to item.To string parsing
            try:
                to_raw = item.To
                if to_raw:
                    for addr in to_raw.split(';'):
                        addr = addr.strip()
                        if addr:
                            m = re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', addr)
                            if m:
                                to_addresses.append(('', m.group().lower()))
                            else:
                                to_addresses.append((addr, addr.lower()))
            except Exception:
                pass
        return to_addresses

    @staticmethod
    def extract_text_body(msg):
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    try:
                        payload = part.get_payload(decode=True)
                        if payload:
                            body = payload.decode('utf-8', errors='replace')
                            break
                    except Exception:
                        continue
        else:
            try:
                payload = msg.get_payload(decode=True)
                if payload:
                    body = payload.decode('utf-8', errors='replace')
            except Exception:
                body = str(msg.get_payload())
        return EmailIngester._strip_quotes(body)

    @staticmethod
    def parse_date(msg):
        date_str = msg.get('Date', '')
        if not date_str:
            return None
        try:
            return parsedate_to_datetime(date_str)
        except Exception:
            return None

    @staticmethod
    def parse_recipients(msg):
        to_raw = msg.get('To', '')
        if not to_raw:
            return []
        recipients = []
        for addr in to_raw.split(','):
            name, email_addr = parseaddr(addr.strip())
            if email_addr:
                recipients.append((name, email_addr.lower()))
        return recipients

    @classmethod
    def from_mbox(cls, filepath, months_back=12):
        cutoff = datetime.now() - timedelta(days=months_back * 30)
        messages = []
        mbox = mailbox.mbox(filepath)
        for msg in mbox:
            date = cls.parse_date(msg)
            if date and date.replace(tzinfo=None) < cutoff:
                continue
            body = cls.extract_text_body(msg)
            if not body or len(body.split()) < 5:
                continue
            messages.append(EmailMessage(
                subject=msg.get('Subject', '(no subject)'),
                body=body,
                to_addresses=cls.parse_recipients(msg),
                date=date,
                message_id=msg.get('Message-ID', '')
            ))
        return messages

    @classmethod
    def from_eml_directory(cls, dirpath, months_back=12):
        cutoff = datetime.now() - timedelta(days=months_back * 30)
        messages = []
        for filename in os.listdir(dirpath):
            if not filename.lower().endswith('.eml'):
                continue
            filepath = os.path.join(dirpath, filename)
            try:
                with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                    msg = email.message_from_file(f)
            except Exception:
                continue
            date = cls.parse_date(msg)
            if date and date.replace(tzinfo=None) < cutoff:
                continue
            body = cls.extract_text_body(msg)
            if not body or len(body.split()) < 5:
                continue
            messages.append(EmailMessage(
                subject=msg.get('Subject', '(no subject)'),
                body=body,
                to_addresses=cls.parse_recipients(msg),
                date=date,
                message_id=msg.get('Message-ID', '')
            ))
        return messages

    @classmethod
    def from_pst(cls, filepath, months_back=12):
        """Read sent emails from .pst via Outlook COM (Windows only)."""
        if not HAS_WIN32COM:
            print("ERROR: pywin32 required for PST. Install: pip install pywin32")
            sys.exit(1)
        if not os.path.exists(filepath):
            print(f"ERROR: PST not found: {filepath}")
            sys.exit(1)

        cutoff = datetime.now() - timedelta(days=months_back * 30)
        messages = []

        try:
            pythoncom.CoInitialize()
            outlook = win32com.client.Dispatch("Outlook.Application")
            namespace = outlook.GetNamespace("MAPI")

            pst_path = os.path.abspath(filepath)
            print(f"Opening PST: {pst_path}")
            namespace.AddStore(pst_path)

            # Find the added PST store (last one)
            pst_folder = None
            for i in range(namespace.Folders.Count, 0, -1):
                folder = namespace.Folders.Item(i)
                try:
                    _ = folder.StoreID
                    pst_folder = folder
                    break
                except Exception:
                    continue

            if not pst_folder:
                print("ERROR: Could not find PST in Outlook")
                return messages

            print(f"PST loaded as: {pst_folder.Name}")

            # Find Sent Items
            sent_names = ['Sent Items', 'Sent Mail', 'Sent', 'Sent Messages']

            def find_sent(parent, depth=0):
                if depth > 3:
                    return None
                try:
                    for j in range(1, parent.Folders.Count + 1):
                        sub = parent.Folders.Item(j)
                        if sub.Name in sent_names:
                            return sub
                        result = find_sent(sub, depth + 1)
                        if result:
                            return result
                except Exception:
                    pass
                return None

            sent_folder = find_sent(pst_folder)

            if not sent_folder:
                print("No 'Sent Items' found. Scanning all folders...")
                all_folders = []
                def collect(parent, depth=0):
                    if depth > 5:
                        return
                    try:
                        all_folders.append(parent)
                        for j in range(1, parent.Folders.Count + 1):
                            collect(parent.Folders.Item(j), depth + 1)
                    except Exception:
                        pass
                collect(pst_folder)
                print(f"Found {len(all_folders)} folders to scan")
            else:
                all_folders = [sent_folder]
                print(f"Found '{sent_folder.Name}' with {sent_folder.Items.Count} items")

            for folder in all_folders:
                try:
                    items = folder.Items
                    if items.Count == 0:
                        continue
                    print(f"  Scanning '{folder.Name}' ({items.Count} items)...")

                    for k in range(1, items.Count + 1):
                        try:
                            item = items.Item(k)
                            if item.Class != 43:  # Mail item
                                continue

                            try:
                                sd = item.SentOn
                                if sd:
                                    sent_dt = datetime(sd.year, sd.month, sd.day, sd.hour, sd.minute, sd.second)
                                    if sent_dt < cutoff:
                                        continue
                                else:
                                    sent_dt = None
                            except Exception:
                                sent_dt = None

                            try:
                                body = item.Body
                            except Exception:
                                body = ""
                            if not body or len(body.split()) < 5:
                                continue

                            body = cls._strip_quotes(body)
                            if not body or len(body.split()) < 5:
                                continue

                            to_addresses = cls._resolve_outlook_recipients(item)

                            try:
                                subject = item.Subject or '(no subject)'
                            except Exception:
                                subject = '(no subject)'

                            messages.append(EmailMessage(
                                subject=subject, body=body,
                                to_addresses=to_addresses,
                                date=sent_dt, message_id=str(k)
                            ))
                        except Exception:
                            continue
                except Exception:
                    continue

            try:
                namespace.RemoveStore(pst_folder)
            except Exception:
                pass
            pythoncom.CoUninitialize()

        except Exception as e:
            print(f"ERROR reading PST: {e}")
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

        return messages

    @classmethod
    def from_outlook_live(cls, accounts=None, months_back=12):
        """Read sent emails from the running Outlook instance.
        Uses Restrict filter for speed instead of iterating all items.
        """
        if not HAS_WIN32COM:
            print("ERROR: pywin32 required. Install: pip install pywin32")
            sys.exit(1)

        cutoff = datetime.now() - timedelta(days=months_back * 30)
        cutoff_str = cutoff.strftime('%m/%d/%Y %H:%M %p')
        messages = []

        try:
            pythoncom.CoInitialize()
            outlook = win32com.client.Dispatch("Outlook.Application")
            namespace = outlook.GetNamespace("MAPI")

            print(f"Connected to Outlook with {namespace.Folders.Count} accounts:")
            for i in range(1, namespace.Folders.Count + 1):
                print(f"  {i}. {namespace.Folders.Item(i).Name}")

            sent_folders = []
            sent_names = ['Sent Items', 'Sent Mail', 'Sent', 'Sent Messages']

            for i in range(1, namespace.Folders.Count + 1):
                acct = namespace.Folders.Item(i)
                acct_name = acct.Name
                if accounts and not any(a.lower() in acct_name.lower() for a in accounts):
                    continue
                try:
                    for j in range(1, acct.Folders.Count + 1):
                        sub = acct.Folders.Item(j)
                        if sub.Name in sent_names:
                            sent_folders.append((acct_name, sub))
                            break
                except Exception:
                    pass

            if not sent_folders:
                print("ERROR: No Sent Items folders found")
                return messages

            for acct_name, folder in sent_folders:
                try:
                    total = folder.Items.Count
                    print(f"\n  [{acct_name}] '{folder.Name}' — {total} total items")
                    if total == 0:
                        continue

                    # Use Restrict to pre-filter by date (MUCH faster)
                    restrict_str = f"[SentOn] >= '{cutoff_str}'"
                    print(f"    Filtering: {restrict_str}")
                    try:
                        filtered = folder.Items.Restrict(restrict_str)
                        fcount = filtered.Count
                        print(f"    {fcount} items after date filter")
                    except Exception as e:
                        print(f"    Restrict failed ({e}), falling back to manual scan...")
                        filtered = folder.Items
                        fcount = total

                    if fcount == 0:
                        continue

                    processed = 0
                    errors = 0
                    item = filtered.GetFirst()
                    while item is not None:
                        try:
                            if item.Class != 43:
                                item = filtered.GetNext()
                                continue

                            # Subject first (lightweight check)
                            try:
                                subject = item.Subject or '(no subject)'
                            except Exception:
                                subject = '(no subject)'

                            print(f"    [{processed+1}] {subject[:60]}...", end='')

                            # Date
                            sent_dt = None
                            try:
                                sd = item.SentOn
                                if sd:
                                    sent_dt = datetime(sd.year, sd.month, sd.day,
                                                       sd.hour, sd.minute, sd.second)
                            except Exception:
                                pass

                            # Body
                            try:
                                body = item.Body
                            except Exception:
                                body = ""
                                print(" [no body]")
                                item = filtered.GetNext()
                                continue

                            if not body or len(body.split()) < 5:
                                print(" [too short]")
                                item = filtered.GetNext()
                                continue

                            body = cls._strip_quotes(body)
                            if not body or len(body.split()) < 5:
                                print(" [quotes only]")
                                item = filtered.GetNext()
                                continue

                            # Recipients
                            to_addresses = cls._resolve_outlook_recipients(item)

                            messages.append(EmailMessage(
                                subject=subject, body=body,
                                to_addresses=to_addresses,
                                date=sent_dt,
                                message_id=f"{acct_name}_{processed}"
                            ))
                            processed += 1
                            wc = len(body.split())
                            print(f" OK ({wc} words)")

                        except Exception as e:
                            errors += 1
                            print(f" [ERROR: {e}]")

                        item = filtered.GetNext()

                    print(f"    Done: {processed} extracted, {errors} errors")

                except Exception as e:
                    print(f"    Account error: {e}")
                    continue

            pythoncom.CoUninitialize()

        except Exception as e:
            print(f"ERROR connecting to Outlook: {e}")
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

        return messages


# ============================================================
#  PHRASE FINGERPRINT ENGINE
# ============================================================

class PhraseFingerprinter:
    """
    Extracts phrase-level vocabulary - the expressions that make
    someone sound like THEM. The core differentiator.
    """

    NOISE_PHRASES = {
        'i think', 'i have', 'i am', 'i was', 'i will', 'i can',
        'it is', 'it was', 'there is', 'there are', 'this is',
        'that is', 'we have', 'we are', 'we will', 'we can',
        'do you', 'can you', 'will you', 'would you', 'could you',
        'let me know', 'thank you', 'thanks for', 'sounds good',
        'as well', 'in the', 'of the', 'for the', 'on the',
        'to the', 'at the', 'by the', 'with the', 'from the',
        'and the', 'but the', 'is the', 'are the', 'was the',
        'have a', 'has been', 'have been', 'had been',
        'going to', 'want to', 'need to', 'able to',
        'a lot', 'a few', 'a bit', 'right now',
    }

    def __init__(self, messages):
        self.messages = messages
        self.stop_words = set(stopwords.words('english'))
        self.phrase_instances = []

    def extract(self):
        self._collect_phrase_instances()
        return {
            "signature_phrases": self._signature_phrases(),
            "sentence_templates": self._sentence_templates(),
            "greeting_expressions": self._greeting_expressions(),
            "closing_expressions": self._closing_expressions(),
            "transition_phrases": self._transition_phrases(),
            "intensifiers_and_softeners": self._intensifiers_and_softeners(),
            "action_phrases": self._action_phrases(),
            "agreement_disagreement": self._agreement_disagreement(),
            "temporal_expressions": self._temporal_expressions(),
            "phrase_embeddings_data": self._prepare_embedding_data(),
        }

    def _collect_phrase_instances(self):
        for msg in self.messages:
            if not msg.body:
                continue
            # Use source_id for transcripts, domain for emails
            domain = 'unknown'
            if hasattr(msg, 'source_id') and msg.source_id:
                domain = msg.source_id
            elif msg.to_addresses:
                addr = msg.to_addresses[0][1]
                domain = addr.split('@')[-1] if '@' in addr else 'unknown'

            sentences = sent_tokenize(msg.body)
            total = len(sentences)

            for i, sentence in enumerate(sentences):
                # Skip signature/boilerplate sentences
                if self._is_sig_noise(sentence):
                    continue
                position = 'opening' if i == 0 else ('closing' if i >= total - 2 else 'body')
                words = [w for w in word_tokenize(sentence.lower()) if w.isalpha()]

                for n in [2, 3, 4, 5]:
                    for j in range(len(words) - n + 1):
                        phrase = ' '.join(words[j:j+n])
                        if n <= 3 and phrase in self.NOISE_PHRASES:
                            continue
                        self.phrase_instances.append(PhraseInstance(
                            phrase=phrase, full_sentence=sentence,
                            recipient_domain=domain, date=msg.date,
                            position_in_email=position
                        ))

    def _signature_phrases(self):
        phrase_freq = Counter(pi.phrase for pi in self.phrase_instances)
        min_count = max(3, len(self.messages) * 0.005)
        candidates = []

        for phrase, count in phrase_freq.items():
            if count < min_count:
                continue
            words = phrase.split()
            if all(w in self.stop_words for w in words):
                continue
            if len(words) == 2 and any(w in self.stop_words for w in words) and all(len(w) <= 3 for w in words):
                continue

            examples = []
            seen = set()
            for pi in self.phrase_instances:
                if pi.phrase == phrase and pi.full_sentence not in seen:
                    examples.append(pi.full_sentence)
                    seen.add(pi.full_sentence)
                    if len(examples) >= 3:
                        break

            domains = Counter(pi.recipient_domain for pi in self.phrase_instances if pi.phrase == phrase)
            positions = Counter(pi.position_in_email for pi in self.phrase_instances if pi.phrase == phrase)

            candidates.append({
                "phrase": phrase, "frequency": count,
                "frequency_per_100_emails": round(count / len(self.messages) * 100, 1),
                "example_sentences": examples,
                "position_distribution": dict(positions),
                "source_distribution": dict(domains.most_common(5)),
                "word_count": len(words),
            })

        candidates.sort(key=lambda x: -x["frequency"])
        return candidates[:100]

    # Words that indicate a line is signature/boilerplate, not real content
    SIG_NOISE_WORDS = {
        'founder', 'partner', 'managing', 'ceo', 'cto', 'director', 'president',
        'manager', 'consultant', 'principal', 'owner', 'chief', 'vp',
        'vice', 'sent', 'mailto', 'http', 'https', 'www', 'com',
    }

    def _is_sig_noise(self, text):
        """Check if a sentence looks like signature/boilerplate content."""
        lower = text.lower()
        # Contains phone numbers
        if re.search(r'\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}', text):
            return True
        # Contains email addresses
        if re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', text):
            return True
        # Contains URLs
        if re.search(r'https?://', lower):
            return True
        # Contains mailto:
        if 'mailto:' in lower:
            return True
        # Mostly signature noise words
        words = [w for w in lower.split() if w.isalpha() and len(w) > 1]
        if words and sum(1 for w in words if w in self.SIG_NOISE_WORDS) / len(words) > 0.4:
            return True
        return False

    def _sentence_templates(self):
        template_freq = Counter()
        template_examples = defaultdict(list)
        skip_starters = ('the', 'a', 'an', 'in', 'on', 'at', 'to', 'for', 'of', 'by', 'it', 'this', 'that')

        for msg in self.messages:
            if not msg.body:
                continue
            for sentence in sent_tokenize(msg.body):
                # Skip signature/boilerplate content
                if self._is_sig_noise(sentence):
                    continue
                words = sentence.split()
                if len(words) < 4:
                    continue
                if words[0].lower() in skip_starters:
                    continue
                for plen in [3, 4, 5]:
                    if len(words) >= plen:
                        t = ' '.join(words[:plen]).lower()
                        template_freq[t] += 1
                        if len(template_examples[t]) < 3:
                            template_examples[t].append(sentence)

        min_count = max(3, len(self.messages) * 0.005)
        return [
            {"template": t, "frequency": c, "examples": template_examples[t]}
            for t, c in template_freq.most_common(50) if c >= min_count
        ]

    def _greeting_expressions(self):
        """Extract greeting patterns, normalizing names to [Name] to find patterns.

        Captures both the normalized pattern ("Hi [Name]...") and raw examples.
        """
        pattern_freq = Counter()           # "Hi [Name]..." -> count
        raw_examples = defaultdict(list)    # "Hi [Name]..." -> ["Hi George...", ...]
        pattern_domains = defaultdict(list)
        greeting_words = ('hi', 'hey', 'hello', 'good', 'dear', 'hope', 'howdy', 'yo', 'greetings')

        # Common names / titles to detect and normalize
        name_pattern = re.compile(
            r'^((?:Hi|Hey|Hello|Dear|Good morning|Good afternoon|Good evening|Howdy|Greetings)[,\s]*)'
            r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)'  # One or two capitalized words (name)
            r'(.*)',                                 # trailing punctuation/text
            re.IGNORECASE
        )

        for msg in self.messages:
            if not msg.body:
                continue
            first = msg.body.strip().split('\n')[0].strip()
            if len(first.split()) > 8:
                continue
            fl = first.lower().rstrip(',!.')
            if not any(fl.startswith(g) for g in greeting_words):
                continue

            raw = first.rstrip(',!. ')
            # Try to normalize: "Hi George..." -> "Hi [Name]..."
            m = name_pattern.match(raw)
            if m:
                prefix = m.group(1).strip()
                trailing = m.group(3).strip()
                normalized = f"{prefix} [Name]{' ' + trailing if trailing else ''}"
                # Clean up double spaces
                normalized = re.sub(r'\s+', ' ', normalized).strip()
            else:
                normalized = raw

            pattern_freq[normalized] += 1
            if len(raw_examples[normalized]) < 5:
                raw_examples[normalized].append(raw)
            domain = msg.to_addresses[0][1].split('@')[-1] if msg.to_addresses else 'unknown'
            if len(pattern_domains[normalized]) < 5:
                pattern_domains[normalized].append(domain)

        return [
            {
                "greeting_pattern": g,
                "frequency": c,
                "examples": raw_examples[g],
                "used_with_domains": pattern_domains[g],
            }
            for g, c in pattern_freq.most_common(20) if c >= 2
        ]

    def _closing_expressions(self):
        closings = Counter()
        closing_words = (
            'thanks', 'thank', 'best', 'regards', 'cheers', 'sincerely',
            'take care', 'talk soon', 'chat soon', 'looking forward',
            'let me know', 'have a', 'all the best', 'warm', 'kind',
            'respectfully', 'appreciate', 'grateful', 'speak soon',
        )
        for msg in self.messages:
            if not msg.body:
                continue
            lines = [l.strip() for l in msg.body.strip().split('\n') if l.strip()]
            for line in lines[-3:]:
                ll = line.lower().rstrip(',!.')
                if any(ll.startswith(c) for c in closing_words):
                    closings[line.rstrip(',!. ')] += 1

        return [{"closing": c, "frequency": n} for c, n in closings.most_common(20) if n >= 2]

    def _transition_phrases(self):
        transitions = [
            'however', 'that said', 'having said that', 'on the other hand',
            'at the same time', 'with that in mind', 'that being said',
            'on another note', 'in any case', 'either way',
            'bottom line', 'at the end of the day', 'long story short',
            'the thing is', "here's the thing", 'the reality is',
            'to be clear', 'to be fair', 'to be honest',
            "for what it's worth", 'in other words',
            'on a separate note', 'one more thing', 'also worth noting',
            'meanwhile', 'in the meantime', 'as a result',
            'furthermore', 'moreover', 'additionally', 'alternatively',
        ]
        text = self.all_text_lower
        found = {}
        for t in transitions:
            count = text.count(t)
            if count >= 2:
                examples = []
                for msg in self.messages:
                    if t in msg.body.lower():
                        for sent in sent_tokenize(msg.body):
                            if t in sent.lower() and len(examples) < 2:
                                examples.append(sent)
                found[t] = {"frequency": count, "per_100_emails": round(count / len(self.messages) * 100, 1), "examples": examples}

        return [{"phrase": k, **v} for k, v in sorted(found.items(), key=lambda x: -x[1]["frequency"])]

    def _intensifiers_and_softeners(self):
        intensifiers = [
            'really', 'very', 'extremely', 'incredibly', 'absolutely',
            'definitely', 'certainly', 'completely', 'totally',
            'particularly', 'especially', 'significantly',
            'truly', 'deeply', 'highly', 'strongly',
            'super', 'tremendous', 'phenomenal', 'outstanding',
        ]
        softeners = [
            'maybe', 'perhaps', 'possibly', 'might', 'could',
            'sort of', 'kind of', 'a bit', 'a little', 'somewhat',
            'i think', 'i believe', 'i feel', 'i guess', 'i suppose',
            'it seems', 'apparently', 'not sure if', 'i wonder',
            'would it be possible', 'if possible', 'if you could',
            'no worries if', 'no pressure', 'when you get a chance',
            'at your convenience', 'feel free to',
        ]
        text = self.all_text_lower
        total_words = sum(m.word_count for m in self.messages)

        i_usage = {w: len(re.findall(r'\b' + re.escape(w) + r'\b', text)) for w in intensifiers}
        i_usage = {k: v for k, v in i_usage.items() if v >= 2}
        s_usage = {p: text.count(p) for p in softeners}
        s_usage = {k: v for k, v in s_usage.items() if v >= 2}

        i_total = sum(i_usage.values())
        s_total = sum(s_usage.values())

        return {
            "intensifiers": dict(sorted(i_usage.items(), key=lambda x: -x[1])),
            "softeners": dict(sorted(s_usage.items(), key=lambda x: -x[1])),
            "intensifier_per_1000_words": round(i_total / total_words * 1000, 1) if total_words else 0,
            "softener_per_1000_words": round(s_total / total_words * 1000, 1) if total_words else 0,
            "confidence_ratio": round(i_total / (i_total + s_total), 3) if (i_total + s_total) > 0 else 0.5,
            "style": "assertive" if i_total > s_total * 1.5 else "diplomatic" if s_total > i_total * 1.5 else "balanced",
        }

    def _action_phrases(self):
        starters = [
            "can you", "could you", "would you", "will you", "please",
            "i need", "we need", "would you mind", "if you could",
            "go ahead and", "feel free to", "make sure",
            "i'd appreciate", "let's", "how about", "what if we",
            "i suggest", "i recommend", "next steps",
        ]
        text = self.all_text_lower
        found = {}
        for phrase in starters:
            count = text.count(phrase)
            if count >= 2:
                examples = []
                for msg in self.messages:
                    if phrase in msg.body.lower():
                        for sent in sent_tokenize(msg.body):
                            if phrase in sent.lower() and len(examples) < 2:
                                examples.append(sent)
                found[phrase] = {"frequency": count, "examples": examples}

        return [{"phrase": k, **v} for k, v in sorted(found.items(), key=lambda x: -x[1]["frequency"])]

    def _agreement_disagreement(self):
        agreement = ['i agree', 'agreed', 'absolutely', 'exactly', 'makes sense', 'for sure',
                     'love it', 'perfect', 'works for me', 'sounds good', 'sounds great']
        disagreement = ["i don't think", "i'm not sure", 'my concern is', 'the issue is',
                        'the problem is', 'the challenge is', 'that said', 'to be fair',
                        'on the flip side', 'not quite', 'hold on']

        text = self.all_text_lower
        af = {p: text.count(p) for p in agreement if text.count(p) >= 1}
        df = {p: text.count(p) for p in disagreement if text.count(p) >= 1}

        return {
            "agreement_phrases": dict(sorted(af.items(), key=lambda x: -x[1])),
            "disagreement_phrases": dict(sorted(df.items(), key=lambda x: -x[1])),
            "disagreement_style": (
                "direct" if any(text.count(p) >= 2 for p in ['i disagree', "i don't think", 'the problem is'])
                else "diplomatic" if any(text.count(p) >= 2 for p in ["i'm not sure", 'my concern is', 'to be fair'])
                else "rare" if sum(df.values()) < 5
                else "balanced"
            ),
        }

    def _temporal_expressions(self):
        temporal = [
            'asap', 'as soon as possible', 'right away', 'immediately',
            'at your convenience', 'when you get a chance', 'no rush',
            'take your time', 'by end of day', 'this week', 'next week',
            'moving forward', 'going forward', 'urgent', 'priority',
        ]
        text = self.all_text_lower
        found = {p: text.count(p) for p in temporal if text.count(p) >= 1}
        urgent = sum(found.get(p, 0) for p in ['asap', 'immediately', 'urgent', 'right away'])
        relaxed = sum(found.get(p, 0) for p in ['no rush', 'take your time', 'when you get a chance', 'at your convenience'])

        return {
            "expressions": dict(sorted(found.items(), key=lambda x: -x[1])),
            "urgency_style": "pressing" if urgent > relaxed * 2 else "relaxed" if relaxed > urgent * 2 else "balanced",
        }

    def _prepare_embedding_data(self):
        phrase_freq = Counter(pi.phrase for pi in self.phrase_instances)
        min_count = max(3, len(self.messages) * 0.005)
        records = []
        seen = set()

        for phrase, count in phrase_freq.most_common(200):
            if count < min_count or phrase in seen:
                continue
            words = phrase.split()
            if all(w in self.stop_words for w in words):
                continue
            seen.add(phrase)

            ctx = []
            domains = Counter()
            positions = Counter()
            for pi in self.phrase_instances:
                if pi.phrase == phrase:
                    if len(ctx) < 5 and pi.full_sentence not in ctx:
                        ctx.append(pi.full_sentence)
                    domains[pi.recipient_domain] += 1
                    positions[pi.position_in_email] += 1

            records.append({
                "phrase": phrase, "frequency": count,
                "context_sentences": ctx,
                "position_distribution": dict(positions),
                "source_distribution": dict(domains.most_common(5)),
                "embedding_text": f"The user frequently says '{phrase}'. Example: {ctx[0] if ctx else phrase}",
            })
        return records

    @property
    def all_text_lower(self):
        if not hasattr(self, '_all_text_lower'):
            self._all_text_lower = ' '.join(m.body.lower() for m in self.messages if m.body)
        return self._all_text_lower


# ============================================================
#  MAIN TW-0 ANALYZER
# ============================================================

class TrueWritingAnalyzer:

    def __init__(self, messages):
        self.messages = messages
        self.all_bodies = [m.body for m in messages if m.body]
        self.all_text = ' '.join(self.all_bodies)
        self.stop_words = set(stopwords.words('english'))
        self.fingerprinter = PhraseFingerprinter(messages)

    def analyze(self):
        if not self.messages:
            return {"error": "No messages to analyze"}

        # Detect input type
        self._source_type = getattr(self.messages[0], 'source_type', 'email')
        label = 'transcripts' if self._source_type == 'transcript' else 'emails'
        print(f"Analyzing {len(self.messages)} {label}...")

        profile = {
            "tw_version": "0.4.0",
            "tw_score": "TW-0",
            "source_type": self._source_type,
            "generated_at": datetime.now().isoformat(),
            "corpus_stats": self._corpus_stats(),
            "vocabulary": self._vocabulary_analysis(),
            "readability": self._readability_analysis(),
            "sentence_structure": self._sentence_analysis(),
            "paragraph_structure": self._paragraph_analysis(),
            "grammar_signature": self._grammar_signature(),
            "punctuation_profile": self._punctuation_profile(),
            "phrase_fingerprint": self.fingerprinter.extract(),
            "tone_indicators": self._tone_indicators(),
        }

        # Email-specific sections (skip for transcripts)
        if self._source_type == 'email':
            profile["email_structure"] = self._email_structure()
            profile["recipients"] = self._recipient_summary()
        else:
            profile["content_structure"] = self._content_structure()

        print(f"TW-0 profile generated: {len(json.dumps(profile, default=str))} bytes")
        return profile

    def _corpus_stats(self):
        dates = [m.date for m in self.messages if m.date]
        label = 'segment' if getattr(self, '_source_type', 'email') == 'transcript' else 'email'
        return {
            "total_messages_analyzed": len(self.messages),
            "source_type": getattr(self, '_source_type', 'email'),
            "total_words": sum(m.word_count for m in self.messages),
            f"avg_words_per_{label}": round(sum(m.word_count for m in self.messages) / len(self.messages), 1),
            "date_range": {
                "earliest": min(dates).isoformat() if dates else None,
                "latest": max(dates).isoformat() if dates else None,
            },
            "emails_per_month": dict(sorted(
                Counter(m.date.strftime('%Y-%m') for m in self.messages if m.date).items()
            )),
        }

    def _vocabulary_analysis(self):
        words = [w for w in word_tokenize(self.all_text.lower()) if w.isalpha() and len(w) > 1]
        total = len(words)
        unique = set(words)
        content = [w for w in words if w not in self.stop_words]
        freq = Counter(content)
        complex_words = [w for w in content if textstat.syllable_count(w) >= 3]
        complex_ratio = len(complex_words) / len(content) if content else 0
        avg_len = sum(len(w) for w in words) / total if total else 0

        per_email = defaultdict(int)
        for body in self.all_bodies:
            for w in set(w.lower() for w in word_tokenize(body) if w.isalpha() and w.lower() not in self.stop_words):
                per_email[w] += 1
        sig_threshold = len(self.messages) * 0.15
        sig_words = dict(sorted(
            {w: c for w, c in per_email.items() if c >= sig_threshold and len(w) > 3}.items(),
            key=lambda x: -x[1]
        )[:40])

        return {
            "total_words": total, "unique_words": len(unique),
            "type_token_ratio": round(len(unique) / total, 4) if total else 0,
            "avg_word_length": round(avg_len, 2),
            "complex_word_ratio": round(complex_ratio, 4),
            "top_content_words": [{"word": w, "count": c} for w, c in freq.most_common(50)],
            "signature_words": sig_words,
            "vocabulary_level": "advanced" if complex_ratio > 0.12 and avg_len > 5.0
                else "intermediate" if complex_ratio > 0.06 else "conversational",
        }

    def _readability_analysis(self):
        if len(self.all_text) < 100:
            return {"note": "Insufficient text"}
        per_email_fk = []
        for body in self.all_bodies:
            if len(body.split()) >= 20:
                try: per_email_fk.append(textstat.flesch_kincaid_grade(body))
                except: pass
        grade = textstat.flesch_kincaid_grade(self.all_text)
        return {
            "flesch_reading_ease": round(textstat.flesch_reading_ease(self.all_text), 1),
            "flesch_kincaid_grade": round(grade, 1),
            "gunning_fog": round(textstat.gunning_fog(self.all_text), 1),
            "coleman_liau": round(textstat.coleman_liau_index(self.all_text), 1),
            "per_email_grade": {
                "min": round(min(per_email_fk), 1) if per_email_fk else None,
                "max": round(max(per_email_fk), 1) if per_email_fk else None,
                "mean": round(sum(per_email_fk) / len(per_email_fk), 1) if per_email_fk else None,
            },
            "difficulty_label": "simple" if grade <= 6 else "standard" if grade <= 9
                else "professional" if grade <= 12 else "academic",
        }

    def _sentence_analysis(self):
        sents = []
        lengths = []
        for body in self.all_bodies:
            for s in sent_tokenize(body):
                sents.append(s)
                lengths.append(len(s.split()))
        if not lengths:
            return {"note": "Insufficient data"}

        starters = Counter(s.split()[0].lower() for s in sents if s.split() and s.split()[0].isalpha())
        total = len(lengths)
        return {
            "total_sentences": total,
            "avg_length": round(sum(lengths) / total, 1),
            "median_length": sorted(lengths)[total // 2],
            "length_distribution": {
                "short_pct": round(sum(1 for l in lengths if l <= 8) / total * 100, 1),
                "medium_pct": round(sum(1 for l in lengths if 9 <= l <= 20) / total * 100, 1),
                "long_pct": round(sum(1 for l in lengths if 21 <= l <= 35) / total * 100, 1),
                "very_long_pct": round(sum(1 for l in lengths if l > 35) / total * 100, 1),
            },
            "type_distribution": {
                "questions_pct": round(sum(1 for s in sents if s.strip().endswith('?')) / total * 100, 1),
                "exclamations_pct": round(sum(1 for s in sents if s.strip().endswith('!')) / total * 100, 1),
            },
            "top_sentence_starters": [{"word": w, "count": c} for w, c in starters.most_common(15)],
        }

    @staticmethod
    def _split_paragraphs(body):
        """Split body into paragraphs, handling \r\n\r\n, \n\n, and multiple blank lines."""
        # Normalize line endings
        text = body.replace('\r\n', '\n').replace('\r', '\n')
        # Split on 2+ consecutive newlines (possibly with whitespace between)
        paragraphs = re.split(r'\n\s*\n', text)
        return [p.strip() for p in paragraphs if p.strip()]

    def _paragraph_analysis(self):
        para_lengths = []
        para_counts = []
        for body in self.all_bodies:
            paras = self._split_paragraphs(body)
            para_counts.append(len(paras))
            for p in paras:
                para_lengths.append(len(sent_tokenize(p)))
        if not para_lengths:
            return {"note": "Insufficient data"}
        single = sum(1 for l in para_lengths if l == 1)
        ratio = single / len(para_lengths)
        return {
            "avg_paragraphs_per_email": round(sum(para_counts) / len(self.all_bodies), 1),
            "avg_sentences_per_paragraph": round(sum(para_lengths) / len(para_lengths), 1),
            "single_sentence_ratio": round(ratio, 3),
            "style": "fragmented" if ratio > 0.6 else "balanced" if ratio > 0.3 else "dense",
        }

    def _grammar_signature(self):
        contraction_pat = re.compile(r"\b\w+n['\u2019]t\b|\b\w+['\u2019](?:ve|re|ll|d|s|m)\b", re.I)
        expanded_pat = re.compile(r'\b(?:do not|does not|did not|will not|would not|could not|should not|cannot|is not|are not|was not|were not|have not|has not|I am|I have|I will|we are|we have|they are)\b', re.I)
        passive_pat = re.compile(r'\b(?:is|are|was|were|been|being)\s+\w+ed\b', re.I)

        contractions = sum(len(contraction_pat.findall(b)) for b in self.all_bodies)
        expanded = sum(len(expanded_pat.findall(b)) for b in self.all_bodies)
        passive = sum(len(passive_pat.findall(b)) for b in self.all_bodies)
        tw = sum(m.word_count for m in self.messages)
        tl = self.all_text.lower()

        ic = len(re.findall(r'\b(?:i|me|my|mine)\b', tl))
        wc = len(re.findall(r'\b(?:we|us|our|ours)\b', tl))
        yc = len(re.findall(r'\b(?:you|your|yours)\b', tl))
        cr = contractions / (contractions + expanded) if (contractions + expanded) > 0 else 0

        return {
            "contraction_ratio": round(cr, 3),
            "contraction_style": "informal" if cr > 0.7 else "mixed" if cr > 0.3 else "formal",
            "passive_per_1000": round(passive / tw * 1000, 1) if tw else 0,
            "perspective": {
                "i_per_1000": round(ic / tw * 1000, 1) if tw else 0,
                "we_per_1000": round(wc / tw * 1000, 1) if tw else 0,
                "you_per_1000": round(yc / tw * 1000, 1) if tw else 0,
                "dominant": "self_focused" if ic > wc and ic > yc else "team_focused" if wc > yc else "audience_focused",
            },
        }

    def _punctuation_profile(self):
        tw = sum(m.word_count for m in self.messages)
        excl = sum(b.count('!') for b in self.all_bodies)
        ques = sum(b.count('?') for b in self.all_bodies)
        ellip = sum(b.count('...') + b.count('\u2026') for b in self.all_bodies)
        em = sum(b.count('\u2014') + b.count(' - ') + b.count('--') for b in self.all_bodies)
        caps = sum(len(re.findall(r'\b[A-Z]{2,}\b', b)) for b in self.all_bodies)
        epm = excl / tw * 1000 if tw else 0

        return {
            "exclamation_per_1000": round(epm, 1),
            "question_per_1000": round(ques / tw * 1000, 1) if tw else 0,
            "ellipsis_per_email": round(ellip / len(self.messages), 2),
            "em_dash_per_email": round(em / len(self.messages), 2),
            "caps_per_1000": round(caps / tw * 1000, 1) if tw else 0,
            "energy": "enthusiastic" if epm > 8 else "moderate" if epm > 3 else "restrained",
        }

    def _email_structure(self):
        greet = close = 0
        for body in self.all_bodies:
            lines = body.strip().split('\n')
            if not lines:
                continue
            first = lines[0].strip().lower()
            if any(first.startswith(g) for g in ['hi', 'hey', 'hello', 'good', 'dear', 'hope', 'howdy']):
                greet += 1
            last = ' '.join(lines[-3:]).strip().lower()
            if any(c in last for c in ['thanks', 'thank', 'best', 'regards', 'cheers', 'take care', 'sincerely']):
                close += 1
        t = len(self.messages)
        return {"greeting_pct": round(greet / t * 100, 1), "closing_pct": round(close / t * 100, 1)}

    def _content_structure(self):
        """Transcript equivalent of email_structure — analyzes opening/closing patterns."""
        open_count = close_count = 0
        for body in self.all_bodies:
            sents = sent_tokenize(body)
            if not sents:
                continue
            first = sents[0].lower()
            if any(first.startswith(g) for g in ['so ', 'today ', 'hey ', 'hi ', 'welcome', "what's up", 'alright']):
                open_count += 1
            last = sents[-1].lower() if len(sents) > 1 else ''
            if any(c in last for c in ['subscribe', 'follow', 'like', 'comment', 'see you', 'bye', 'peace', 'thanks for watching', 'catch you']):
                close_count += 1
        t = len(self.messages)
        return {
            "opening_hook_pct": round(open_count / t * 100, 1) if t else 0,
            "closing_cta_pct": round(close_count / t * 100, 1) if t else 0,
        }

    def _tone_indicators(self):
        g = self._grammar_signature()
        p = self._punctuation_profile()
        r = self._readability_analysis()
        f = 5.0
        if g["contraction_style"] == "formal": f += 1.5
        elif g["contraction_style"] == "informal": f -= 1.5
        if p["energy"] == "enthusiastic": f -= 1
        elif p["energy"] == "restrained": f += 0.5
        grade = r.get("flesch_kincaid_grade", 8)
        if grade > 12: f += 1
        elif grade < 6: f -= 1
        f = max(0, min(10, f))
        return {
            "baseline_formality": round(f, 1),
            "formality_label": "formal" if f > 7 else "professional" if f > 5 else "conversational" if f > 3 else "casual",
            "energy": p["energy"],
            "perspective": g["perspective"]["dominant"],
        }

    def _recipient_summary(self):
        all_r = set()
        dc = Counter()
        for m in self.messages:
            for name, addr in m.to_addresses:
                all_r.add(addr)
                dc[addr.split('@')[-1] if '@' in addr else 'unknown'] += 1
        return {
            "unique_recipients": len(all_r),
            "top_domains": [{"domain": d, "count": c} for d, c in dc.most_common(20)],
            "recipients_for_calibration": [
                {"email_hash": hash(a) % 10**8, "domain": a.split('@')[-1], "email_count": 0}
                for a in list(all_r)[:50]
            ],
        }
