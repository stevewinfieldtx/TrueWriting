"""
TrueWriting Shield - Database Layer

SQLite storage for CPP profiles, scoring history, and DLP hits.
Single file deployment: shield.db contains everything.
"""

import sqlite3
import json
import os
from datetime import datetime
from typing import Optional, List, Dict
from contextlib import contextmanager


class ShieldDB:
    """SQLite database for TrueWriting Shield."""

    def __init__(self, db_path: str = "shield.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Create tables if they don't exist."""
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tenants (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now')),
                    active INTEGER DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    email TEXT NOT NULL,
                    display_name TEXT,
                    platform_user_id TEXT,
                    department TEXT,
                    job_title TEXT,
                    role_risk TEXT DEFAULT 'standard',
                    last_cpp_build TEXT,
                    cpp_email_count INTEGER DEFAULT 0,
                    active INTEGER DEFAULT 1,
                    created_at TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (tenant_id) REFERENCES tenants(id),
                    UNIQUE(tenant_id, email)
                );

                CREATE TABLE IF NOT EXISTS cpp_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    profile_json TEXT NOT NULL,
                    built_at TEXT DEFAULT (datetime('now')),
                    email_count INTEGER,
                    word_count INTEGER,
                    version TEXT DEFAULT '0.4.0',
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS score_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    sender_email TEXT NOT NULL,
                    user_id INTEGER,
                    direction TEXT NOT NULL,
                    score REAL,
                    verdict TEXT NOT NULL,
                    deviations_json TEXT,
                    email_subject TEXT,
                    email_word_count INTEGER,
                    scored_at TEXT DEFAULT (datetime('now')),
                    resolved_by TEXT,
                    resolved_at TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS dlp_hits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    sender_email TEXT NOT NULL,
                    score_log_id INTEGER,
                    pattern_type TEXT NOT NULL,
                    match_count INTEGER DEFAULT 1,
                    redacted_preview TEXT,
                    action_taken TEXT,
                    detected_at TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (score_log_id) REFERENCES score_log(id)
                );

                CREATE TABLE IF NOT EXISTS thresholds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    role_risk TEXT NOT NULL,
                    hold_below REAL DEFAULT 0.6,
                    flag_below REAL DEFAULT 0.75,
                    verification_routing TEXT DEFAULT 'sender',
                    auto_release_minutes INTEGER DEFAULT 0,
                    FOREIGN KEY (tenant_id) REFERENCES tenants(id),
                    UNIQUE(tenant_id, role_risk)
                );

                CREATE INDEX IF NOT EXISTS idx_users_email ON users(tenant_id, email);
                CREATE INDEX IF NOT EXISTS idx_cpp_user ON cpp_profiles(user_id);
                CREATE INDEX IF NOT EXISTS idx_score_sender ON score_log(tenant_id, sender_email);
                CREATE INDEX IF NOT EXISTS idx_score_time ON score_log(scored_at);
                CREATE INDEX IF NOT EXISTS idx_dlp_time ON dlp_hits(detected_at);
            """)

    @contextmanager
    def _conn(self):
        """Connection context manager with WAL mode for concurrent reads."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Tenant Operations ───────────────────────────────

    def upsert_tenant(self, tenant_id: str, name: str, platform: str, config: Dict) -> str:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO tenants (id, name, platform, config_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, platform=excluded.platform,
                    config_json=excluded.config_json
            """, (tenant_id, name, platform, json.dumps(config)))
        return tenant_id

    def get_tenant(self, tenant_id: str) -> Optional[Dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
            if row:
                d = dict(row)
                d['config'] = json.loads(d.pop('config_json'))
                return d
        return None

    # ── User Operations ─────────────────────────────────

    def upsert_user(self, tenant_id: str, email: str, display_name: str = '',
                    platform_user_id: str = '', department: str = '',
                    job_title: str = '', role_risk: str = 'standard') -> int:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO users (tenant_id, email, display_name, platform_user_id,
                                   department, job_title, role_risk)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, email) DO UPDATE SET
                    display_name=excluded.display_name,
                    platform_user_id=excluded.platform_user_id,
                    department=excluded.department,
                    job_title=excluded.job_title,
                    role_risk=excluded.role_risk
            """, (tenant_id, email.lower(), display_name, platform_user_id,
                  department, job_title, role_risk))
            row = conn.execute(
                "SELECT id FROM users WHERE tenant_id = ? AND email = ?",
                (tenant_id, email.lower())
            ).fetchone()
            return row['id']

    def get_user_by_email(self, tenant_id: str, email: str) -> Optional[Dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE tenant_id = ? AND email = ?",
                (tenant_id, email.lower())
            ).fetchone()
            return dict(row) if row else None

    def list_users(self, tenant_id: str, active_only: bool = True) -> List[Dict]:
        with self._conn() as conn:
            q = "SELECT * FROM users WHERE tenant_id = ?"
            if active_only:
                q += " AND active = 1"
            return [dict(r) for r in conn.execute(q, (tenant_id,)).fetchall()]

    # ── CPP Profile Operations ──────────────────────────

    def store_cpp(self, user_id: int, profile: Dict, email_count: int = 0,
                  word_count: int = 0) -> int:
        with self._conn() as conn:
            cursor = conn.execute("""
                INSERT INTO cpp_profiles (user_id, profile_json, email_count, word_count)
                VALUES (?, ?, ?, ?)
            """, (user_id, json.dumps(profile, default=str), email_count, word_count))
            conn.execute("""
                UPDATE users SET last_cpp_build = datetime('now'), cpp_email_count = ?
                WHERE id = ?
            """, (email_count, user_id))
            return cursor.lastrowid

    def get_latest_cpp(self, user_id: int) -> Optional[Dict]:
        with self._conn() as conn:
            row = conn.execute("""
                SELECT * FROM cpp_profiles WHERE user_id = ?
                ORDER BY built_at DESC LIMIT 1
            """, (user_id,)).fetchone()
            if row:
                d = dict(row)
                d['profile'] = json.loads(d.pop('profile_json'))
                return d
        return None

    def get_cpp_by_email(self, tenant_id: str, email: str) -> Optional[Dict]:
        user = self.get_user_by_email(tenant_id, email)
        if not user:
            return None
        return self.get_latest_cpp(user['id'])

    # ── Score Logging ───────────────────────────────────

    def log_score(self, tenant_id: str, sender_email: str, direction: str,
                  score: float, verdict: str, deviations: Dict = None,
                  subject: str = '', word_count: int = 0,
                  user_id: int = None) -> int:
        with self._conn() as conn:
            cursor = conn.execute("""
                INSERT INTO score_log (tenant_id, sender_email, user_id, direction,
                                       score, verdict, deviations_json,
                                       email_subject, email_word_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (tenant_id, sender_email.lower(), user_id, direction,
                  score, verdict, json.dumps(deviations) if deviations else None,
                  subject, word_count))
            return cursor.lastrowid

    def resolve_score(self, score_id: int, resolved_by: str):
        with self._conn() as conn:
            conn.execute("""
                UPDATE score_log SET resolved_by = ?, resolved_at = datetime('now')
                WHERE id = ?
            """, (resolved_by, score_id))

    # ── DLP Hit Logging ─────────────────────────────────

    def log_dlp_hit(self, tenant_id: str, sender_email: str,
                    pattern_type: str, match_count: int = 1,
                    redacted_preview: str = '', action_taken: str = 'logged',
                    score_log_id: int = None) -> int:
        with self._conn() as conn:
            cursor = conn.execute("""
                INSERT INTO dlp_hits (tenant_id, sender_email, score_log_id,
                                      pattern_type, match_count,
                                      redacted_preview, action_taken)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (tenant_id, sender_email.lower(), score_log_id,
                  pattern_type, match_count, redacted_preview, action_taken))
            return cursor.lastrowid

    # ── Threshold Operations ────────────────────────────

    def set_threshold(self, tenant_id: str, role_risk: str,
                      hold_below: float = 0.6, flag_below: float = 0.75,
                      verification_routing: str = 'sender',
                      auto_release_minutes: int = 0):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO thresholds (tenant_id, role_risk, hold_below, flag_below,
                                        verification_routing, auto_release_minutes)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, role_risk) DO UPDATE SET
                    hold_below=excluded.hold_below, flag_below=excluded.flag_below,
                    verification_routing=excluded.verification_routing,
                    auto_release_minutes=excluded.auto_release_minutes
            """, (tenant_id, role_risk, hold_below, flag_below,
                  verification_routing, auto_release_minutes))

    def get_threshold(self, tenant_id: str, role_risk: str) -> Dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM thresholds WHERE tenant_id = ? AND role_risk = ?",
                (tenant_id, role_risk)
            ).fetchone()
            if row:
                return dict(row)
        return {
            'hold_below': 0.6, 'flag_below': 0.75,
            'verification_routing': 'sender', 'auto_release_minutes': 0,
        }

    # ── Dashboard Queries ───────────────────────────────

    def get_score_stats(self, tenant_id: str, days: int = 30) -> Dict:
        with self._conn() as conn:
            row = conn.execute("""
                SELECT
                    COUNT(*) as total_scored,
                    SUM(CASE WHEN verdict = 'pass' THEN 1 ELSE 0 END) as passed,
                    SUM(CASE WHEN verdict = 'hold' THEN 1 ELSE 0 END) as held,
                    SUM(CASE WHEN verdict = 'flag' THEN 1 ELSE 0 END) as flagged,
                    AVG(score) as avg_score
                FROM score_log
                WHERE tenant_id = ? AND scored_at >= datetime('now', ?)
            """, (tenant_id, f'-{days} days')).fetchone()
            return dict(row) if row else {}

    def get_dlp_stats(self, tenant_id: str, days: int = 30) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT pattern_type, COUNT(*) as hits, SUM(match_count) as total_matches
                FROM dlp_hits
                WHERE tenant_id = ? AND detected_at >= datetime('now', ?)
                GROUP BY pattern_type ORDER BY hits DESC
            """, (tenant_id, f'-{days} days')).fetchall()
            return [dict(r) for r in rows]

    def get_recent_alerts(self, tenant_id: str, limit: int = 20) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT s.*, u.display_name, u.role_risk
                FROM score_log s
                LEFT JOIN users u ON s.user_id = u.id
                WHERE s.tenant_id = ? AND s.verdict IN ('hold', 'flag')
                ORDER BY s.scored_at DESC LIMIT ?
            """, (tenant_id, limit)).fetchall()
            return [dict(r) for r in rows]
