"""
TrueWriting Shield - Scoring Engine (Policy-Aware)

Resolves the effective policy for the sender (distributor → reseller → tenant → group),
then scores the email against their CPP using that policy's thresholds.
"""

import os
import re
import json
from typing import Dict, Optional, List
from dataclasses import dataclass

import database as db
from dlp.scanner import DLPScanner, DLPResult


@dataclass
class ScoreResult:
    score: float
    verdict: str
    deviations: Dict
    policy: Dict
    dlp_result: Optional[DLPResult] = None
    sender_email: str = ''
    has_cpp: bool = False

    def to_dict(self) -> Dict:
        result = {
            "score": round(self.score, 3),
            "verdict": self.verdict,
            "deviations": self.deviations,
            "sender_email": self.sender_email,
            "has_cpp": self.has_cpp,
            "policy_applied": {
                "name": self.policy.get("policy_name", ""),
                "source": self.policy.get("policy_source", ""),
                "warn_threshold": self.policy.get("score_threshold_warn"),
                "hold_threshold": self.policy.get("score_threshold_hold"),
            },
        }
        if self.dlp_result and self.dlp_result.has_sensitive_data:
            result["dlp"] = self.dlp_result.to_dict()
        return result


class ScoringEngine:

    async def score_email(self, sender_email: str, body: str,
                          subject: str = '', direction: str = 'outbound') -> ScoreResult:
        """
        Score an email:
        1. Look up sender → get tenant + security groups
        2. Resolve effective policy from the hierarchy
        3. Run DLP with policy's DLP settings
        4. Run CPP behavioral scoring with policy's thresholds
        5. Log everything
        """
        # Look up sender
        user = await db.get_user_by_email(sender_email)
        tenant_id = user["tenant_id"] if user else None
        user_group_ids = user.get("group_ids", []) if user else []

        # Resolve effective policy
        if tenant_id:
            policy = await db.resolve_effective_policy(tenant_id, user_group_ids)
        else:
            policy = {
                "score_threshold_warn": 0.35, "score_threshold_hold": 0.55,
                "dlp_enabled": 1, "dlp_min_confidence": "medium", "dlp_action": "warn",
                "notify_sender": 1, "notify_manager": 0, "notify_it": 0,
                "notify_emails": [], "auto_release_minutes": 0,
                "policy_name": "System Default", "policy_source": "no_tenant",
            }

        # DLP scan (using policy's DLP settings)
        dlp_result = DLPResult()
        if policy.get("dlp_enabled", 1):
            scanner = DLPScanner(min_confidence=policy.get("dlp_min_confidence", "medium"))
            dlp_result = scanner.scan(body, subject)

        # CPP lookup
        cpp_data = await db.get_cpp_by_email(sender_email)

        if not cpp_data:
            verdict = "unknown"
            if dlp_result.has_sensitive_data:
                verdict = dlp_result.recommended_action
            result = ScoreResult(
                score=0.0, verdict=verdict,
                deviations={"note": "No CPP on file for this sender"},
                policy=policy, dlp_result=dlp_result,
                sender_email=sender_email, has_cpp=False,
            )
            if tenant_id:
                await db.log_score(
                    tenant_id=tenant_id, sender_email=sender_email,
                    direction=direction, subject=subject, score=0.0,
                    verdict=verdict, policy_name=policy.get("policy_name", ""),
                    deviations={"note": "No CPP"}, word_count=len(body.split()),
                    user_id=user["id"] if user else None)
            return result

        profile = cpp_data["profile_json"]
        warn_threshold = policy.get("score_threshold_warn", 0.35)
        hold_threshold = policy.get("score_threshold_hold", 0.55)

        # ── Behavioral scoring ───────────────────────────────
        deviations = {}
        scores = []

        # 1. Word count
        avg_words = profile.get("corpus_stats", {}).get("avg_words_per_email", 100)
        email_words = len(body.split())
        word_ratio = email_words / max(avg_words, 1)
        if word_ratio > 3.0 or word_ratio < 0.2:
            word_dev = min(1.0, abs(word_ratio - 1.0) / 3.0)
            deviations["word_count"] = {
                "expected_avg": round(avg_words), "actual": email_words,
                "deviation": round(word_dev, 3)}
            scores.append(word_dev * 0.1)

        # 2. Readability
        email_read = self._quick_readability(body)
        cpp_read = profile.get("readability", {}).get("flesch_kincaid_grade")
        if cpp_read and email_read:
            grade_diff = abs(email_read - cpp_read)
            if grade_diff > 3:
                read_dev = min(1.0, grade_diff / 10.0)
                deviations["readability"] = {
                    "expected_grade": round(cpp_read, 1), "actual_grade": round(email_read, 1),
                    "deviation": round(read_dev, 3)}
                scores.append(read_dev * 0.15)

        # 3. Formality
        email_form = self._estimate_formality(body)
        cpp_form = profile.get("tone_indicators", {}).get("baseline_formality", 5.0)
        form_diff = abs(email_form - cpp_form)
        if form_diff > 2.0:
            form_dev = min(1.0, form_diff / 7.0)
            deviations["formality"] = {
                "expected": round(cpp_form, 1), "actual": round(email_form, 1),
                "deviation": round(form_dev, 3)}
            scores.append(form_dev * 0.15)

        # 4. Contractions
        email_contr = self._contraction_ratio(body)
        cpp_contr = profile.get("grammar_signature", {}).get("contraction_ratio", 0.5)
        contr_diff = abs(email_contr - cpp_contr)
        if contr_diff > 0.3:
            contr_dev = min(1.0, contr_diff / 0.7)
            deviations["contractions"] = {
                "expected_ratio": round(cpp_contr, 3), "actual_ratio": round(email_contr, 3),
                "deviation": round(contr_dev, 3)}
            scores.append(contr_dev * 0.1)

        # 5. Greeting
        email_greeting = self._extract_greeting(body)
        cpp_greetings = profile.get("phrase_fingerprint", {}).get("greeting_expressions", [])
        if cpp_greetings and email_greeting:
            known = [g.get("greeting_pattern", "").lower() for g in cpp_greetings]
            if not any(email_greeting.lower().startswith(k.split('[')[0].strip().lower())
                       for k in known if k):
                deviations["greeting"] = {
                    "expected_patterns": known[:5], "actual": email_greeting, "deviation": 0.8}
                scores.append(0.8 * 0.15)

        # 6. Closing
        email_closing = self._extract_closing(body)
        cpp_closings = profile.get("phrase_fingerprint", {}).get("closing_expressions", [])
        if cpp_closings and email_closing:
            known_c = [c.get("closing", "").lower() for c in cpp_closings]
            if not any(email_closing.lower().startswith(k.lower()) for k in known_c if k):
                deviations["closing"] = {
                    "expected_patterns": known_c[:5], "actual": email_closing, "deviation": 0.6}
                scores.append(0.6 * 0.1)

        # 7. Exclamation energy
        email_excl = (body.count('!') / max(email_words, 1)) * 1000
        cpp_excl = profile.get("punctuation_profile", {}).get("exclamation_per_1000", 5.0)
        excl_diff = abs(email_excl - cpp_excl)
        if excl_diff > 5:
            excl_dev = min(1.0, excl_diff / 15.0)
            deviations["exclamation_energy"] = {
                "expected_per_1000": round(cpp_excl, 1), "actual_per_1000": round(email_excl, 1),
                "deviation": round(excl_dev, 3)}
            scores.append(excl_dev * 0.1)

        # 8. Perspective shift
        perspective = self._perspective_ratios(body)
        cpp_persp = profile.get("grammar_signature", {}).get("perspective", {})
        cpp_dom = cpp_persp.get("dominant", "")
        email_dom = max(perspective, key=perspective.get) if perspective else ""
        if cpp_dom and email_dom and cpp_dom != email_dom:
            deviations["perspective_shift"] = {
                "expected": cpp_dom, "actual": email_dom, "deviation": 0.4}
            scores.append(0.4 * 0.15)

        # Aggregate
        total_score = min(1.0, sum(scores)) if scores else 0.0

        # Verdict using policy thresholds
        if total_score >= hold_threshold:
            verdict = "hold"
        elif total_score >= warn_threshold:
            verdict = "warn"
        else:
            verdict = "pass"

        # DLP escalation
        if dlp_result.has_sensitive_data:
            dlp_action = dlp_result.recommended_action
            if dlp_action == "hold":
                verdict = "hold"
            elif dlp_action == "warn" and verdict == "pass":
                verdict = "warn"

        result = ScoreResult(
            score=total_score, verdict=verdict, deviations=deviations,
            policy=policy, dlp_result=dlp_result,
            sender_email=sender_email, has_cpp=True,
        )

        # Log
        if tenant_id:
            score_log_id = await db.log_score(
                tenant_id=tenant_id, sender_email=sender_email,
                direction=direction, subject=subject, score=total_score,
                verdict=verdict, policy_name=policy.get("policy_name", ""),
                deviations=deviations, word_count=email_words,
                user_id=user["id"] if user else None)
            if dlp_result.has_sensitive_data:
                for match in dlp_result.matches:
                    await db.log_dlp_hit(
                        tenant_id=tenant_id, sender_email=sender_email,
                        pattern_type=match.pattern_type, match_count=match.match_count,
                        confidence=match.confidence, compliance_tags=match.compliance_tags,
                        action_taken=dlp_result.recommended_action,
                        details={"redacted_sample": match.redacted_sample},
                        score_log_id=score_log_id)

        return result

    # ── Quick analysis helpers ───────────────────────────────

    @staticmethod
    def _quick_readability(text):
        sentences = [s.strip() for s in re.split(r'[.!?]+', text) if s.strip()]
        words = text.split()
        if not sentences or len(words) < 10:
            return None
        avg_sent = len(words) / len(sentences)
        syllables = sum(max(1, len(re.findall(r'[aeiouy]+', w, re.I))) for w in words)
        avg_syl = syllables / len(words)
        return max(0, 0.39 * avg_sent + 11.8 * avg_syl - 15.59)

    @staticmethod
    def _estimate_formality(text):
        score = 5.0
        lower = text.lower()
        words = len(text.split()) or 1
        contractions = len(re.findall(r"\b\w+n['\u2019]t\b|\b\w+['\u2019](?:ve|re|ll|d|s|m)\b", text, re.I))
        contr_rate = contractions / words
        if contr_rate > 0.03: score -= 1.5
        elif contr_rate < 0.005: score += 1.0
        if re.match(r'^dear\s', lower): score += 1.5
        elif re.match(r'^(?:hey|yo|sup)\b', lower): score -= 1.5
        if text.count('!') > words * 0.01: score -= 0.5
        return max(0, min(10, score))

    @staticmethod
    def _contraction_ratio(text):
        c = len(re.findall(r"\b\w+n['\u2019]t\b|\b\w+['\u2019](?:ve|re|ll|d|s|m)\b", text, re.I))
        e = len(re.findall(r'\b(?:do not|does not|did not|will not|would not|could not|should not|cannot|is not|are not|was not|were not|have not|has not|I am|I have|I will|we are|we have|they are)\b', text, re.I))
        return c / (c + e) if (c + e) > 0 else 0.5

    @staticmethod
    def _extract_greeting(text):
        lines = text.strip().split('\n')
        if not lines: return None
        first = lines[0].strip()
        if len(first.split()) > 8: return None
        lower = first.lower().rstrip(',!.')
        if any(lower.startswith(g) for g in ('hi','hey','hello','good','dear','hope','howdy','greetings')):
            return first.rstrip(',!. ')
        return None

    @staticmethod
    def _extract_closing(text):
        lines = [l.strip() for l in text.strip().split('\n') if l.strip()]
        if len(lines) < 2: return None
        for line in lines[-3:]:
            lower = line.lower().rstrip(',!.')
            if any(lower.startswith(c) for c in ('thanks','thank','best','regards','cheers','sincerely','take care','talk soon')):
                return line.rstrip(',!. ')
        return None

    @staticmethod
    def _perspective_ratios(text):
        lower = text.lower()
        words = len(text.split()) or 1
        return {
            "self_focused": len(re.findall(r'\b(?:i|me|my|mine)\b', lower)) / words * 1000,
            "team_focused": len(re.findall(r'\b(?:we|us|our|ours)\b', lower)) / words * 1000,
            "audience_focused": len(re.findall(r'\b(?:you|your|yours)\b', lower)) / words * 1000,
        }
