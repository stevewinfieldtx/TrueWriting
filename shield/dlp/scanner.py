"""
TrueWriting Shield - DLP Scanner
Content-aware scanning of email text for sensitive data patterns.
11 pattern types with Luhn validation, confidence scoring, compliance tagging,
and false positive suppression.
"""

import re
from dataclasses import dataclass, field
from typing import List, Dict, Optional


@dataclass
class DLPMatch:
    pattern_type: str
    confidence: str
    compliance_tags: List[str]
    match_count: int = 1
    redacted_sample: str = ''
    context_hint: str = ''


@dataclass
class DLPResult:
    has_sensitive_data: bool = False
    total_matches: int = 0
    highest_confidence: str = 'none'
    matches: List[DLPMatch] = field(default_factory=list)
    compliance_tags: List[str] = field(default_factory=list)
    recommended_action: str = 'pass'

    def to_dict(self) -> Dict:
        return {
            "has_sensitive_data": self.has_sensitive_data,
            "total_matches": self.total_matches,
            "highest_confidence": self.highest_confidence,
            "matches": [{"pattern_type": m.pattern_type, "confidence": m.confidence,
                         "compliance_tags": m.compliance_tags, "match_count": m.match_count,
                         "redacted_sample": m.redacted_sample} for m in self.matches],
            "compliance_tags": self.compliance_tags,
            "recommended_action": self.recommended_action,
        }


def _luhn_check(number: str) -> bool:
    digits = [int(d) for d in number if d.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    checksum = 0
    for i, d in enumerate(digits[::-1]):
        if i % 2 == 1:
            d *= 2
            if d > 9: d -= 9
        checksum += d
    return checksum % 10 == 0


def _aba_checksum(number: str) -> bool:
    digits = [int(d) for d in number if d.isdigit()]
    if len(digits) != 9: return False
    weights = [3, 7, 1, 3, 7, 1, 3, 7, 1]
    return sum(d * w for d, w in zip(digits, weights)) % 10 == 0


PATTERNS = {
    "credit_card": {
        "regex": re.compile(
            r'\b(?:4\d{3}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}'
            r'|5[1-5]\d{2}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}'
            r'|3[47]\d{1}[\s\-]?\d{6}[\s\-]?\d{5}'
            r'|6(?:011|5\d{2})[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4})\b'),
        "validator": lambda m: _luhn_check(re.sub(r'[\s\-]', '', m)),
        "confidence_base": "high",
        "compliance": ["PCI-DSS"],
        "redact": lambda m: re.sub(r'[\s\-]', '', m)[-4:].rjust(len(re.sub(r'[\s\-]', '', m)), '*'),
    },
    "ssn": {
        "regex": re.compile(r'\b(?!000|666|9\d{2})\d{3}[\s\-]\d{2}[\s\-]\d{4}\b'),
        "validator": lambda m: True,
        "confidence_base": "medium",
        "context_boost": re.compile(r'(?:ssn|social\s*security|ss#|soc\s*sec)', re.I),
        "compliance": ["PII", "State Breach Notification"],
        "redact": lambda m: "***-**-" + m[-4:],
    },
    "routing_number": {
        "regex": re.compile(r'\b[0-3]\d{8}\b'),
        "validator": lambda m: _aba_checksum(m),
        "confidence_base": "medium",
        "context_boost": re.compile(r'(?:routing|aba|bank|transit|wire)', re.I),
        "compliance": ["Financial Data", "PII"],
        "redact": lambda m: "****" + m[-4:],
    },
    "iban": {
        "regex": re.compile(r'\b[A-Z]{2}\d{2}[\s]?[\dA-Z]{4}[\s]?[\dA-Z]{4}[\s]?[\dA-Z]{4}(?:[\s]?[\dA-Z]{1,4}){0,5}\b'),
        "validator": lambda m: len(re.sub(r'\s', '', m)) >= 15,
        "confidence_base": "high",
        "compliance": ["Financial Data", "GDPR", "PII"],
        "redact": lambda m: m[:4] + "****" + m[-4:],
    },
    "api_key_aws": {
        "regex": re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
        "validator": lambda m: True,
        "confidence_base": "high",
        "compliance": ["Credentials", "Security"],
        "redact": lambda m: m[:8] + "****" + m[-4:],
    },
    "api_key_stripe": {
        "regex": re.compile(r'\b(?:sk_live|pk_live|sk_test|pk_test)_[A-Za-z0-9]{24,}\b'),
        "validator": lambda m: True,
        "confidence_base": "high",
        "compliance": ["Credentials", "PCI-DSS", "Security"],
        "redact": lambda m: m[:12] + "****",
    },
    "api_key_github": {
        "regex": re.compile(r'\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b'),
        "validator": lambda m: True,
        "confidence_base": "high",
        "compliance": ["Credentials", "Security"],
        "redact": lambda m: m[:8] + "****",
    },
    "api_key_generic": {
        "regex": re.compile(
            r'(?:api[_\-]?key|apikey|secret[_\-]?key|access[_\-]?token|auth[_\-]?token)'
            r'[\s:=]+["\']?([A-Za-z0-9\-_]{32,})["\']?', re.I),
        "validator": lambda m: True,
        "confidence_base": "medium",
        "compliance": ["Credentials", "Security"],
        "redact": lambda m: m[:8] + "****",
    },
    "private_key": {
        "regex": re.compile(
            r'-----BEGIN\s+(?:RSA\s+)?(?:PRIVATE|EC)\s+KEY-----[\s\S]{20,}'
            r'-----END\s+(?:RSA\s+)?(?:PRIVATE|EC)\s+KEY-----', re.MULTILINE),
        "validator": lambda m: True,
        "confidence_base": "high",
        "compliance": ["Credentials", "Security"],
        "redact": lambda m: "-----BEGIN PRIVATE KEY----- [REDACTED]",
    },
    "us_passport": {
        "regex": re.compile(r'\b[A-Z]\d{8}\b'),
        "validator": lambda m: True,
        "confidence_base": "low",
        "context_boost": re.compile(r'(?:passport|travel\s*doc)', re.I),
        "compliance": ["PII", "GDPR"],
        "redact": lambda m: m[0] + "****" + m[-3:],
    },
    "medical_record": {
        "regex": re.compile(r'(?:MRN|medical\s*record|patient\s*id|health\s*id)[\s:#]*(\d{6,12})', re.I),
        "validator": lambda m: True,
        "confidence_base": "medium",
        "compliance": ["HIPAA", "PHI"],
        "redact": lambda m: "MRN:****" + m[-3:] if len(m) > 3 else "****",
    },
}

FALSE_POSITIVE_PATTERNS = {
    "credit_card": [re.compile(r'(?:invoice|order|po|ref|tracking|ticket|confirmation)[\s#:]*\d{13,19}', re.I)],
    "ssn": [re.compile(r'(?:phone|tel|fax|ext|zip|postal)[\s:]*\d{3}[\s\-]\d{2}[\s\-]\d{4}', re.I)],
    "routing_number": [re.compile(r'(?:zip|postal|phone|ext)[\s:]*\d{9}', re.I)],
}

CONFIDENCE_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}


def _is_false_positive(pattern_type: str, text: str, match_start: int) -> bool:
    context_start = max(0, match_start - 50)
    context = text[context_start:match_start + 50].lower()
    for fp in FALSE_POSITIVE_PATTERNS.get(pattern_type, []):
        if fp.search(context):
            return True
    return False


class DLPScanner:

    def __init__(self, min_confidence: str = "medium"):
        self.min_confidence = min_confidence
        self.min_rank = CONFIDENCE_RANK.get(min_confidence, 2)

    def scan(self, text: str, subject: str = '') -> DLPResult:
        if not text:
            return DLPResult()

        full_text = f"{subject}\n{text}" if subject else text
        all_matches: List[DLPMatch] = []

        for pattern_name, pdef in PATTERNS.items():
            found = list(pdef["regex"].finditer(full_text))
            if not found:
                continue

            valid = []
            for m in found:
                matched = m.group(0) if not m.groups() else m.group(1)
                try:
                    if not pdef["validator"](matched):
                        continue
                except Exception:
                    continue
                if _is_false_positive(pattern_name, full_text, m.start()):
                    continue
                valid.append(matched)

            if not valid:
                continue

            confidence = pdef["confidence_base"]
            ctx_boost = pdef.get("context_boost")
            if ctx_boost and ctx_boost.search(full_text):
                if confidence == "low": confidence = "medium"
                elif confidence == "medium": confidence = "high"
            if len(valid) >= 3 and confidence != "high":
                confidence = "high"
            if CONFIDENCE_RANK.get(confidence, 0) < self.min_rank:
                continue

            try:
                redacted = pdef["redact"](valid[0])
            except Exception:
                redacted = "****"

            all_matches.append(DLPMatch(
                pattern_type=pattern_name, confidence=confidence,
                compliance_tags=pdef["compliance"], match_count=len(valid),
                redacted_sample=redacted))

        if not all_matches:
            return DLPResult()

        all_compliance = list(set(t for m in all_matches for t in m.compliance_tags))
        highest = max(all_matches, key=lambda m: CONFIDENCE_RANK.get(m.confidence, 0))
        total = sum(m.match_count for m in all_matches)

        if highest.confidence == "high" or total >= 5:
            action = "hold"
        elif highest.confidence == "medium" or total >= 2:
            action = "warn"
        else:
            action = "log"

        return DLPResult(
            has_sensitive_data=True, total_matches=total,
            highest_confidence=highest.confidence, matches=all_matches,
            compliance_tags=all_compliance, recommended_action=action)
