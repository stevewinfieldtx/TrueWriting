import json
import hashlib
import re
import os
import sys
from datetime import datetime
from collections import Counter

# CONFIG
PST_PATH = r"C:\Users\steve\Documents\backup.pst"
OUTPUT_PATH = r"C:\Users\steve\Documents\TrueWriting\corpus_sent.json"
MAX_EMAILS = 5000
MIN_BODY_LENGTH = 20

try:
    import pypff
except ImportError:
    print("ERROR: pypff not installed.")
    print("Run: pip install libpff-python")
    sys.exit(1)


def normalize_autoformat(text):
    """Reverse Outlook and email client auto-formatting.
    This is critical for accurate CPP analysis. Without this step,
    the analyzer measures Microsoft's formatting choices, not the user's."""
    # Em dash (U+2014) back to double hyphen
    text = text.replace('\u2014', '--')
    # En dash (U+2013) back to single hyphen
    text = text.replace('\u2013', '-')
    # Smart double quotes back to straight
    text = text.replace('\u201c', '"')
    text = text.replace('\u201d', '"')
    # Smart single quotes / apostrophes back to straight
    text = text.replace('\u2018', "'")
    text = text.replace('\u2019', "'")
    # Ellipsis character back to three dots
    text = text.replace('\u2026', '...')
    # Non-breaking space to regular space
    text = text.replace('\u00a0', ' ')
    # Bullet character to hyphen (auto-formatted lists)
    text = text.replace('\u2022', '-')
    return text


def strip_signature(text):
    sig_markers = [
        r'\n---\s*Steve.*$',
        r'\n---\s*S\.?\s*$',
        r'\n--\s*Steve.*$',
        r'\n--\s*\n',
        r'\nSent from my ',
        r'\nGet Outlook for ',
        r'\n_{3,}',
        r'\nBest regards,?\s*\n',
        r'\nRegards,?\s*\n',
        r'\nThanks,?\s*\n',
        r'\nThank you,?\s*\n',
        r'\nCheers,?\s*\n',
        r'\nSteve Winfield.*$',
        r'\nSteve\s*$',
        r'\nS\.\s*$',
    ]
    for marker in sig_markers:
        match = re.search(marker, text, re.IGNORECASE | re.DOTALL)
        if match:
            if match.start() > len(text) * 0.3:
                text = text[:match.start()]
    return text.strip()


def strip_reply_chain(text):
    reply_markers = [
        r'\nOn .+wrote:\s*\n',
        r'\n-{3,}\s*Original Message\s*-{3,}',
        r'\nFrom:\s+.+\n',
        r'\n>{1,}\s',
    ]
    for marker in reply_markers:
        match = re.search(marker, text, re.IGNORECASE)
        if match:
            text = text[:match.start()]
    return text.strip()


def clean_html(text):
    if '<html' in text.lower() or '<div' in text.lower() or '<p>' in text.lower():
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'&nbsp;', ' ', text, flags=re.IGNORECASE)
        text = re.sub(r'&amp;', '&', text, flags=re.IGNORECASE)
        text = re.sub(r'&lt;', '<', text, flags=re.IGNORECASE)
        text = re.sub(r'&gt;', '>', text, flags=re.IGNORECASE)
        text = re.sub(r'&quot;', '"', text, flags=re.IGNORECASE)
        text = re.sub(r'&#\d+;', '', text)
    text = re.sub(r'\r\n', '\n', text)
    text = re.sub(r'\r', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text.strip()


def is_bulk_or_automated(subject, body):
    bulk_signals = [
        r'unsubscribe',
        r'auto-reply',
        r'out of office',
        r'automatic reply',
        r'do not reply',
        r'noreply',
        r'newsletter',
        r'invitation:',
        r'accepted:',
        r'declined:',
        r'tentative:',
        r'canceled:',
        r'updated invitation',
    ]
    combined = (subject or '') + ' ' + (body or '')
    combined_lower = combined.lower()
    for signal in bulk_signals:
        if re.search(signal, combined_lower):
            return True
    return False


def hash_recipient(email_addr):
    if not email_addr:
        return "unknown"
    normalized = email_addr.strip().lower()
    return hashlib.sha256(normalized.encode()).hexdigest()[:12]


def extract_recipient_domain(email_addr):
    if not email_addr:
        return "unknown"
    match = re.search(r'@([\w.-]+)', email_addr)
    return match.group(1) if match else "unknown"


def find_sent_folder(root):
    sent_names = ['sent items', 'sent', 'sent mail', 'sent messages']
    for i in range(root.number_of_sub_folders):
        folder = root.get_sub_folder(i)
        name = (folder.name or '').strip().lower()
        if name in sent_names:
            return folder
        result = find_sent_folder(folder)
        if result:
            return result
    return None


def extract_messages(folder, messages, depth=0):
    for i in range(folder.number_of_sub_messages):
        if len(messages) >= MAX_EMAILS:
            break
        try:
            msg = folder.get_sub_message(i)
            subject = msg.subject or ''
            body = msg.plain_text_body
            if body:
                body = body.decode('utf-8', errors='replace') if isinstance(body, bytes) else body
            else:
                html_body = msg.html_body
                if html_body:
                    body = html_body.decode('utf-8', errors='replace') if isinstance(html_body, bytes) else html_body
                    body = clean_html(body)
                else:
                    continue

            delivery_time = None
            try:
                dt = msg.delivery_time
                if dt:
                    delivery_time = dt.isoformat() if hasattr(dt, 'isoformat') else str(dt)
            except Exception:
                pass

            recipients_hashed = []
            recipient_domains = []
            try:
                for r in range(msg.number_of_recipients):
                    recip = msg.get_recipient(r)
                    addr = recip.email_address if recip else None
                    if addr:
                        recipients_hashed.append(hash_recipient(addr))
                        recipient_domains.append(extract_recipient_domain(addr))
            except Exception:
                pass

            if is_bulk_or_automated(subject, body):
                continue

            # CRITICAL: normalize auto-formatting BEFORE any analysis
            body = normalize_autoformat(body)
            body = clean_html(body)
            body = strip_reply_chain(body)
            body = strip_signature(body)

            if len(body) < MIN_BODY_LENGTH:
                continue

            word_count = len(body.split())

            messages.append({
                'body': body,
                'word_count': word_count,
                'char_count': len(body),
                'sentence_count': len(re.split(r'[.!?]+', body)),
                'paragraph_count': len([p for p in body.split('\n\n') if p.strip()]),
                'subject_length': len(subject),
                'has_subject': bool(subject and subject.strip()),
                'is_reply': subject.lower().startswith(('re:', 're :')),
                'is_forward': subject.lower().startswith(('fw:', 'fwd:', 'fw :', 'fwd :')),
                'recipient_hashes': recipients_hashed,
                'recipient_domains': recipient_domains,
                'recipient_count': len(recipients_hashed),
                'timestamp': delivery_time,
            })

            if len(messages) % 100 == 0:
                print(f"  Extracted {len(messages)} messages...")

        except Exception:
            continue


def main():
    print("=" * 60)
    print("TrueWriting PST Sent Email Extractor")
    print("=" * 60)
    print()

    if not os.path.exists(PST_PATH):
        print(f"ERROR: PST file not found at {PST_PATH}")
        sys.exit(1)

    file_size_mb = os.path.getsize(PST_PATH) / (1024 * 1024)
    print(f"PST file: {PST_PATH}")
    print(f"File size: {file_size_mb:.1f} MB")
    print(f"Max emails to extract: {MAX_EMAILS}")
    print()

    print("Opening PST file...")
    pst = pypff.file()
    pst.open(PST_PATH)

    root = pst.get_root_folder()

    print("\nTop-level folders found:")
    for i in range(root.number_of_sub_folders):
        folder = root.get_sub_folder(i)
        name = folder.name or '(unnamed)'
        msg_count = folder.number_of_sub_messages
        sub_count = folder.number_of_sub_folders
        print(f"  - {name} ({msg_count} messages, {sub_count} subfolders)")

    print("\nSearching for Sent Items folder...")
    sent_folder = find_sent_folder(root)

    if not sent_folder:
        print("WARNING: Could not find Sent Items folder.")
        print("Extracting from ALL folders...")
        messages = []
        def extract_all(folder, msgs):
            extract_messages(folder, msgs)
            for i in range(folder.number_of_sub_folders):
                if len(msgs) >= MAX_EMAILS:
                    break
                extract_all(folder.get_sub_folder(i), msgs)
        extract_all(root, messages)
    else:
        print(f"Found: {sent_folder.name} ({sent_folder.number_of_sub_messages} messages)")
        messages = []
        extract_messages(sent_folder, messages)
        for i in range(sent_folder.number_of_sub_folders):
            if len(messages) >= MAX_EMAILS:
                break
            sub = sent_folder.get_sub_folder(i)
            print(f"  Subfolder: {sub.name} ({sub.number_of_sub_messages} messages)")
            extract_messages(sub, messages)

    pst.close()

    print(f"\nExtracted {len(messages)} messages total.")

    if not messages:
        print("ERROR: No messages extracted.")
        print("The PST may have a different folder structure.")
        print("Check the folder names listed above.")
        sys.exit(1)

    total_words = sum(m['word_count'] for m in messages)
    total_chars = sum(m['char_count'] for m in messages)
    avg_words = total_words / len(messages)
    avg_chars = total_chars / len(messages)

    word_counts = sorted([m['word_count'] for m in messages])
    median_words = word_counts[len(word_counts) // 2]

    reply_count = sum(1 for m in messages if m['is_reply'])
    forward_count = sum(1 for m in messages if m['is_forward'])
    original_count = len(messages) - reply_count - forward_count

    all_domains = []
    for m in messages:
        all_domains.extend(m['recipient_domains'])
    domain_counts = Counter(all_domains).most_common(20)

    corpus = {
        'metadata': {
            'subject': 'Steve Winfield',
            'source': 'backup.pst',
            'extracted_at': datetime.now().isoformat(),
            'total_messages': len(messages),
            'total_words': total_words,
            'total_characters': total_chars,
            'avg_words_per_message': round(avg_words, 1),
            'median_words_per_message': median_words,
            'avg_chars_per_message': round(avg_chars, 1),
            'reply_percentage': round(reply_count / len(messages) * 100, 1),
            'forward_percentage': round(forward_count / len(messages) * 100, 1),
            'original_percentage': round(original_count / len(messages) * 100, 1),
            'top_recipient_domains': domain_counts,
            'autoformat_normalization': 'Applied: em dash to --, en dash to -, smart quotes to straight, ellipsis char to ..., non-breaking space to space',
        },
        'messages': messages,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(corpus, f, indent=2, ensure_ascii=False, default=str)

    output_size_mb = os.path.getsize(OUTPUT_PATH) / (1024 * 1024)

    print()
    print("=" * 60)
    print("EXTRACTION COMPLETE")
    print("=" * 60)
    print(f"Messages extracted: {len(messages)}")
    print(f"Total words: {total_words:,}")
    print(f"Avg words/message: {avg_words:.1f}")
    print(f"Median words/message: {median_words}")
    print(f"Replies: {reply_count} ({reply_count/len(messages)*100:.0f}%)")
    print(f"Forwards: {forward_count} ({forward_count/len(messages)*100:.0f}%)")
    print(f"Original: {original_count} ({original_count/len(messages)*100:.0f}%)")
    print(f"Output file: {OUTPUT_PATH}")
    print(f"Output size: {output_size_mb:.1f} MB")
    print()
    print("Auto-format normalization applied:")
    print("  Em dashes -> double hyphens")
    print("  En dashes -> hyphens")
    print("  Smart quotes -> straight quotes")
    print("  Ellipsis char -> three dots")
    print()
    print("NEXT: Upload corpus_sent.json to Claude for full CPP analysis.")


if __name__ == '__main__':
    main()
