"""
TrueWriting Shield - Database Layer (MSP / Distributor Ready)

Three-tier hierarchy:
  Distributor (Rain Networks)
    └── Reseller (small MSPs under Rain)
        └── Tenant (end customer M365/Google org)
            └── Security Groups → Policies → Users

Policies cascade: distributor defaults → reseller overrides → tenant overrides → group overrides.
"""

import aiosqlite
import json
import os
from typing import Optional, Dict, List

DB_PATH = os.getenv("SHIELD_DB_PATH", "shield.db")


async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    db = await get_db()
    try:
        await db.executescript("""

            -- ═══════════════════════════════════════════════════
            --  DISTRIBUTORS
            --  Top level. Rain Networks is a distributor.
            --  Sees all resellers and all end customers.
            -- ═══════════════════════════════════════════════════

            CREATE TABLE IF NOT EXISTS distributors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                contact_email TEXT DEFAULT '',
                contact_name TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                -- Default policy values that cascade down unless overridden
                default_score_warn REAL DEFAULT 0.35,
                default_score_hold REAL DEFAULT 0.55,
                default_dlp_enabled INTEGER DEFAULT 1,
                default_dlp_min_confidence TEXT DEFAULT 'medium',
                default_dlp_action TEXT DEFAULT 'warn',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            -- ═══════════════════════════════════════════════════
            --  RESELLERS
            --  Under a distributor. Can also be "direct" (the disti
            --  acting as its own reseller). Sees only their tenants.
            -- ═══════════════════════════════════════════════════

            CREATE TABLE IF NOT EXISTS resellers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                distributor_id INTEGER NOT NULL REFERENCES distributors(id),
                name TEXT NOT NULL,
                contact_email TEXT DEFAULT '',
                contact_name TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                is_direct INTEGER DEFAULT 0,
                -- Override distributor defaults (NULL = inherit from distributor)
                override_score_warn REAL,
                override_score_hold REAL,
                override_dlp_enabled INTEGER,
                override_dlp_min_confidence TEXT,
                override_dlp_action TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                UNIQUE(distributor_id, name)
            );

            -- ═══════════════════════════════════════════════════
            --  TENANTS (end customers)
            --  The actual M365 or Google Workspace org.
            --  Each has its own mail credentials and mailboxes.
            -- ═══════════════════════════════════════════════════

            CREATE TABLE IF NOT EXISTS tenants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reseller_id INTEGER NOT NULL REFERENCES resellers(id),
                name TEXT NOT NULL,
                domain TEXT DEFAULT '',
                platform TEXT NOT NULL DEFAULT 'm365',
                -- M365 credentials
                ms_tenant_id TEXT DEFAULT '',
                ms_client_id TEXT DEFAULT '',
                ms_client_secret TEXT DEFAULT '',
                -- Google Workspace credentials (future)
                google_service_account_json TEXT DEFAULT '',
                google_delegated_user TEXT DEFAULT '',
                -- Status and sync
                status TEXT DEFAULT 'active',
                last_sync TEXT,
                user_count INTEGER DEFAULT 0,
                -- Override reseller/distributor defaults (NULL = inherit)
                override_score_warn REAL,
                override_score_hold REAL,
                override_dlp_enabled INTEGER,
                override_dlp_min_confidence TEXT,
                override_dlp_action TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                UNIQUE(reseller_id, name)
            );

            -- ═══════════════════════════════════════════════════
            --  POLICIES (per-tenant, assigned to security groups)
            --  Override tenant-level defaults for specific groups
            --  of users. E.g., "Finance Team" gets tighter thresholds.
            -- ═══════════════════════════════════════════════════

            CREATE TABLE IF NOT EXISTS policies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL REFERENCES tenants(id),
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                -- Behavioral scoring thresholds
                score_threshold_warn REAL DEFAULT 0.35,
                score_threshold_hold REAL DEFAULT 0.55,
                -- DLP settings
                dlp_enabled INTEGER DEFAULT 1,
                dlp_min_confidence TEXT DEFAULT 'medium',
                dlp_action TEXT DEFAULT 'warn',
                -- Notification routing
                notify_sender INTEGER DEFAULT 1,
                notify_manager INTEGER DEFAULT 0,
                notify_it INTEGER DEFAULT 0,
                notify_emails TEXT DEFAULT '[]',
                -- Auto-release (minutes, 0 = manual only)
                auto_release_minutes INTEGER DEFAULT 0,
                -- Is this the default policy for the tenant?
                is_default INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                UNIQUE(tenant_id, name)
            );

            -- ═══════════════════════════════════════════════════
            --  SECURITY GROUP MAPPINGS
            --  Maps platform security groups (Azure AD groups,
            --  Google groups) to Shield policies.
            -- ═══════════════════════════════════════════════════

            CREATE TABLE IF NOT EXISTS security_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL REFERENCES tenants(id),
                policy_id INTEGER NOT NULL REFERENCES policies(id),
                group_id TEXT NOT NULL,
                group_name TEXT DEFAULT '',
                -- Priority: higher = takes precedence when user is in multiple groups
                priority INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(tenant_id, group_id)
            );

            -- ═══════════════════════════════════════════════════
            --  USERS (mailbox users within a tenant)
            -- ═══════════════════════════════════════════════════

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL REFERENCES tenants(id),
                email TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                platform_user_id TEXT DEFAULT '',
                department TEXT DEFAULT '',
                job_title TEXT DEFAULT '',
                -- Resolved policy (from security group or tenant default)
                policy_id INTEGER REFERENCES policies(id),
                -- Security group memberships (JSON array of group_ids)
                group_ids TEXT DEFAULT '[]',
                -- CPP status
                cpp_status TEXT DEFAULT 'pending',
                last_cpp_build TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                UNIQUE(tenant_id, email)
            );

            -- ═══════════════════════════════════════════════════
            --  CPP PROFILES
            -- ═══════════════════════════════════════════════════

            CREATE TABLE IF NOT EXISTS cpp_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                profile_json TEXT NOT NULL,
                email_count INTEGER DEFAULT 0,
                word_count INTEGER DEFAULT 0,
                built_at TEXT DEFAULT (datetime('now')),
                version TEXT DEFAULT '0.4.0'
            );

            -- ═══════════════════════════════════════════════════
            --  SCORE LOG
            -- ═══════════════════════════════════════════════════

            CREATE TABLE IF NOT EXISTS score_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER REFERENCES tenants(id),
                sender_email TEXT NOT NULL,
                user_id INTEGER REFERENCES users(id),
                direction TEXT NOT NULL DEFAULT 'outbound',
                subject TEXT DEFAULT '',
                score REAL,
                verdict TEXT NOT NULL DEFAULT 'pass',
                policy_name TEXT DEFAULT '',
                deviations_json TEXT DEFAULT '{}',
                email_word_count INTEGER DEFAULT 0,
                scored_at TEXT DEFAULT (datetime('now'))
            );

            -- ═══════════════════════════════════════════════════
            --  DLP HITS
            -- ═══════════════════════════════════════════════════

            CREATE TABLE IF NOT EXISTS dlp_hits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER REFERENCES tenants(id),
                sender_email TEXT NOT NULL,
                score_log_id INTEGER REFERENCES score_log(id),
                pattern_type TEXT NOT NULL,
                match_count INTEGER DEFAULT 1,
                confidence TEXT DEFAULT 'medium',
                compliance_tags TEXT DEFAULT '[]',
                action_taken TEXT DEFAULT 'log',
                details_json TEXT DEFAULT '{}',
                detected_at TEXT DEFAULT (datetime('now'))
            );

            -- ═══════════════════════════════════════════════════
            --  INDEXES
            -- ═══════════════════════════════════════════════════

            CREATE INDEX IF NOT EXISTS idx_resellers_disti ON resellers(distributor_id);
            CREATE INDEX IF NOT EXISTS idx_tenants_reseller ON tenants(reseller_id);
            CREATE INDEX IF NOT EXISTS idx_tenants_domain ON tenants(domain);
            CREATE INDEX IF NOT EXISTS idx_policies_tenant ON policies(tenant_id);
            CREATE INDEX IF NOT EXISTS idx_secgroups_tenant ON security_groups(tenant_id);
            CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id);
            CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
            CREATE INDEX IF NOT EXISTS idx_cpp_user ON cpp_profiles(user_id);
            CREATE INDEX IF NOT EXISTS idx_score_tenant ON score_log(tenant_id);
            CREATE INDEX IF NOT EXISTS idx_score_sender ON score_log(sender_email);
            CREATE INDEX IF NOT EXISTS idx_score_verdict ON score_log(verdict);
            CREATE INDEX IF NOT EXISTS idx_dlp_tenant ON dlp_hits(tenant_id);
            CREATE INDEX IF NOT EXISTS idx_dlp_type ON dlp_hits(pattern_type);
        """)
        await db.commit()
        print(f"  Database initialized: {DB_PATH}")
    finally:
        await db.close()


# ══════════════════════════════════════════════════════════════
#  DISTRIBUTOR OPERATIONS
# ══════════════════════════════════════════════════════════════

async def create_distributor(name: str, contact_email: str = '', contact_name: str = '') -> int:
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO distributors (name, contact_email, contact_name) VALUES (?, ?, ?)",
            (name, contact_email, contact_name))
        await db.commit()
        return cursor.lastrowid
    finally:
        await db.close()


async def get_distributor(distributor_id: int) -> Optional[Dict]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM distributors WHERE id = ?", (distributor_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def list_distributors() -> List[Dict]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM distributors")
        return [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()


# ══════════════════════════════════════════════════════════════
#  RESELLER OPERATIONS
# ══════════════════════════════════════════════════════════════

async def create_reseller(distributor_id: int, name: str, is_direct: bool = False,
                          contact_email: str = '', contact_name: str = '') -> int:
    db = await get_db()
    try:
        cursor = await db.execute(
            """INSERT INTO resellers (distributor_id, name, is_direct, contact_email, contact_name)
               VALUES (?, ?, ?, ?, ?)""",
            (distributor_id, name, 1 if is_direct else 0, contact_email, contact_name))
        await db.commit()
        return cursor.lastrowid
    finally:
        await db.close()


async def list_resellers(distributor_id: int) -> List[Dict]:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM resellers WHERE distributor_id = ?", (distributor_id,))
        return [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()


# ══════════════════════════════════════════════════════════════
#  TENANT OPERATIONS
# ══════════════════════════════════════════════════════════════

async def create_tenant(reseller_id: int, name: str, domain: str = '',
                        platform: str = 'm365', ms_tenant_id: str = '',
                        ms_client_id: str = '', ms_client_secret: str = '') -> int:
    db = await get_db()
    try:
        cursor = await db.execute(
            """INSERT INTO tenants (reseller_id, name, domain, platform,
                                   ms_tenant_id, ms_client_id, ms_client_secret)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (reseller_id, name, domain, platform, ms_tenant_id, ms_client_id, ms_client_secret))
        tenant_id = cursor.lastrowid
        # Create default policy for this tenant
        await db.execute(
            """INSERT INTO policies (tenant_id, name, description, is_default)
               VALUES (?, 'Default', 'Default policy for all users', 1)""",
            (tenant_id,))
        await db.commit()
        return tenant_id
    finally:
        await db.close()


async def get_tenant(tenant_id: int) -> Optional[Dict]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def get_tenant_by_domain(domain: str) -> Optional[Dict]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM tenants WHERE domain = ?", (domain,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def list_tenants(reseller_id: int = None) -> List[Dict]:
    db = await get_db()
    try:
        if reseller_id:
            cursor = await db.execute(
                "SELECT * FROM tenants WHERE reseller_id = ?", (reseller_id,))
        else:
            cursor = await db.execute("SELECT * FROM tenants")
        return [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()


async def update_tenant_user_count(tenant_id: int, count: int):
    db = await get_db()
    try:
        await db.execute(
            "UPDATE tenants SET user_count = ?, last_sync = datetime('now') WHERE id = ?",
            (count, tenant_id))
        await db.commit()
    finally:
        await db.close()


# ══════════════════════════════════════════════════════════════
#  POLICY OPERATIONS
# ══════════════════════════════════════════════════════════════

async def create_policy(tenant_id: int, name: str, **kwargs) -> int:
    db = await get_db()
    try:
        cols = ["tenant_id", "name"]
        vals = [tenant_id, name]
        allowed = [
            "description", "score_threshold_warn", "score_threshold_hold",
            "dlp_enabled", "dlp_min_confidence", "dlp_action",
            "notify_sender", "notify_manager", "notify_it", "notify_emails",
            "auto_release_minutes", "is_default"
        ]
        for k, v in kwargs.items():
            if k in allowed:
                cols.append(k)
                vals.append(json.dumps(v) if isinstance(v, (list, dict)) else v)
        placeholders = ",".join("?" * len(vals))
        col_names = ",".join(cols)
        cursor = await db.execute(
            f"INSERT INTO policies ({col_names}) VALUES ({placeholders})", vals)
        await db.commit()
        return cursor.lastrowid
    finally:
        await db.close()


async def get_policy(policy_id: int) -> Optional[Dict]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM policies WHERE id = ?", (policy_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def get_default_policy(tenant_id: int) -> Optional[Dict]:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM policies WHERE tenant_id = ? AND is_default = 1", (tenant_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def list_policies(tenant_id: int) -> List[Dict]:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM policies WHERE tenant_id = ?", (tenant_id,))
        return [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()


# ══════════════════════════════════════════════════════════════
#  SECURITY GROUP OPERATIONS
# ══════════════════════════════════════════════════════════════

async def map_security_group(tenant_id: int, policy_id: int,
                             group_id: str, group_name: str = '',
                             priority: int = 0) -> int:
    db = await get_db()
    try:
        cursor = await db.execute(
            """INSERT INTO security_groups (tenant_id, policy_id, group_id, group_name, priority)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(tenant_id, group_id) DO UPDATE SET
                   policy_id = excluded.policy_id, group_name = excluded.group_name,
                   priority = excluded.priority""",
            (tenant_id, policy_id, group_id, group_name, priority))
        await db.commit()
        return cursor.lastrowid
    finally:
        await db.close()


async def list_security_groups(tenant_id: int) -> List[Dict]:
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT sg.*, p.name as policy_name
            FROM security_groups sg
            JOIN policies p ON sg.policy_id = p.id
            WHERE sg.tenant_id = ?
            ORDER BY sg.priority DESC
        """, (tenant_id,))
        return [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()


# ══════════════════════════════════════════════════════════════
#  RESOLVE EFFECTIVE POLICY
#  Cascade: distributor → reseller → tenant → security group
# ══════════════════════════════════════════════════════════════

async def resolve_effective_policy(tenant_id: int, user_group_ids: List[str] = None) -> Dict:
    """
    Resolve the effective policy for a user, cascading through the hierarchy.

    Priority:
      1. Security group policy (highest priority group wins)
      2. Tenant-level overrides
      3. Reseller-level overrides
      4. Distributor defaults
    """
    db = await get_db()
    try:
        # Start with distributor defaults
        cursor = await db.execute("""
            SELECT d.default_score_warn, d.default_score_hold,
                   d.default_dlp_enabled, d.default_dlp_min_confidence, d.default_dlp_action
            FROM tenants t
            JOIN resellers r ON t.reseller_id = r.id
            JOIN distributors d ON r.distributor_id = d.id
            WHERE t.id = ?
        """, (tenant_id,))
        row = await cursor.fetchone()
        if not row:
            # Fallback hardcoded defaults
            policy = {
                "score_threshold_warn": 0.35, "score_threshold_hold": 0.55,
                "dlp_enabled": 1, "dlp_min_confidence": "medium", "dlp_action": "warn",
                "notify_sender": 1, "notify_manager": 0, "notify_it": 0,
                "notify_emails": [], "auto_release_minutes": 0,
                "policy_name": "System Default", "policy_source": "hardcoded",
            }
            return policy

        policy = {
            "score_threshold_warn": row["default_score_warn"],
            "score_threshold_hold": row["default_score_hold"],
            "dlp_enabled": row["default_dlp_enabled"],
            "dlp_min_confidence": row["default_dlp_min_confidence"],
            "dlp_action": row["default_dlp_action"],
            "notify_sender": 1, "notify_manager": 0, "notify_it": 0,
            "notify_emails": [], "auto_release_minutes": 0,
            "policy_name": "Distributor Default", "policy_source": "distributor",
        }

        # Layer reseller overrides
        cursor = await db.execute("""
            SELECT r.override_score_warn, r.override_score_hold,
                   r.override_dlp_enabled, r.override_dlp_min_confidence, r.override_dlp_action
            FROM tenants t JOIN resellers r ON t.reseller_id = r.id WHERE t.id = ?
        """, (tenant_id,))
        reseller = await cursor.fetchone()
        if reseller:
            for field in ["score_warn", "score_hold", "dlp_enabled", "dlp_min_confidence", "dlp_action"]:
                override_key = f"override_{field}"
                policy_key = f"score_threshold_{field.replace('score_', '')}" if field.startswith("score_") else field
                val = reseller[override_key]
                if val is not None:
                    policy[policy_key] = val
                    policy["policy_source"] = "reseller"

        # Layer tenant overrides
        cursor = await db.execute("""
            SELECT override_score_warn, override_score_hold,
                   override_dlp_enabled, override_dlp_min_confidence, override_dlp_action
            FROM tenants WHERE id = ?
        """, (tenant_id,))
        tenant = await cursor.fetchone()
        if tenant:
            for field in ["score_warn", "score_hold", "dlp_enabled", "dlp_min_confidence", "dlp_action"]:
                override_key = f"override_{field}"
                policy_key = f"score_threshold_{field.replace('score_', '')}" if field.startswith("score_") else field
                val = tenant[override_key]
                if val is not None:
                    policy[policy_key] = val
                    policy["policy_source"] = "tenant"

        # Layer tenant default policy (has notification routing, auto-release)
        default_policy = await get_default_policy(tenant_id)
        if default_policy:
            for key in ["notify_sender", "notify_manager", "notify_it",
                        "notify_emails", "auto_release_minutes",
                        "score_threshold_warn", "score_threshold_hold",
                        "dlp_enabled", "dlp_min_confidence", "dlp_action"]:
                if key in default_policy and default_policy[key] is not None:
                    val = default_policy[key]
                    if key == "notify_emails" and isinstance(val, str):
                        val = json.loads(val)
                    policy[key] = val
            policy["policy_name"] = default_policy["name"]
            policy["policy_source"] = "tenant_policy"

        # Finally, check security group policies (highest priority wins)
        if user_group_ids:
            cursor = await db.execute("""
                SELECT sg.group_id, sg.group_name, sg.priority, p.*
                FROM security_groups sg
                JOIN policies p ON sg.policy_id = p.id
                WHERE sg.tenant_id = ? AND sg.group_id IN ({})
                ORDER BY sg.priority DESC LIMIT 1
            """.format(",".join("?" * len(user_group_ids))),
                (tenant_id, *user_group_ids))
            group_policy = await cursor.fetchone()
            if group_policy:
                gp = dict(group_policy)
                for key in ["score_threshold_warn", "score_threshold_hold",
                            "dlp_enabled", "dlp_min_confidence", "dlp_action",
                            "notify_sender", "notify_manager", "notify_it",
                            "notify_emails", "auto_release_minutes"]:
                    if key in gp and gp[key] is not None:
                        val = gp[key]
                        if key == "notify_emails" and isinstance(val, str):
                            val = json.loads(val)
                        policy[key] = val
                policy["policy_name"] = gp["name"]
                policy["policy_source"] = f"security_group:{gp['group_name']}"

        return policy
    finally:
        await db.close()


# ══════════════════════════════════════════════════════════════
#  USER OPERATIONS
# ══════════════════════════════════════════════════════════════

async def upsert_user(tenant_id: int, email: str, display_name: str = '',
                      platform_user_id: str = '', department: str = '',
                      job_title: str = '', group_ids: List[str] = None) -> int:
    db = await get_db()
    try:
        group_json = json.dumps(group_ids or [])
        await db.execute("""
            INSERT INTO users (tenant_id, email, display_name, platform_user_id,
                             department, job_title, group_ids)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, email) DO UPDATE SET
                display_name = excluded.display_name,
                platform_user_id = excluded.platform_user_id,
                department = excluded.department,
                job_title = excluded.job_title,
                group_ids = excluded.group_ids,
                updated_at = datetime('now')
        """, (tenant_id, email, display_name, platform_user_id,
              department, job_title, group_json))
        await db.commit()
        cursor = await db.execute(
            "SELECT id FROM users WHERE tenant_id = ? AND email = ?", (tenant_id, email))
        row = await cursor.fetchone()
        return row[0]
    finally:
        await db.close()


async def get_user_by_email(email: str) -> Optional[Dict]:
    """Look up a user by email across all tenants."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM users WHERE email = ?", (email,))
        row = await cursor.fetchone()
        if row:
            result = dict(row)
            result['group_ids'] = json.loads(result.get('group_ids', '[]'))
            return result
        return None
    finally:
        await db.close()


async def list_users(tenant_id: int) -> List[Dict]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM users WHERE tenant_id = ?", (tenant_id,))
        return [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()


# ══════════════════════════════════════════════════════════════
#  CPP PROFILE OPERATIONS
# ══════════════════════════════════════════════════════════════

async def store_cpp(user_id: int, profile: Dict, email_count: int = 0, word_count: int = 0):
    db = await get_db()
    try:
        await db.execute("""
            INSERT INTO cpp_profiles (user_id, profile_json, email_count, word_count)
            VALUES (?, ?, ?, ?)
        """, (user_id, json.dumps(profile, default=str), email_count, word_count))
        await db.execute("""
            UPDATE users SET cpp_status = 'ready', last_cpp_build = datetime('now'),
                           updated_at = datetime('now')
            WHERE id = ?
        """, (user_id,))
        await db.commit()
    finally:
        await db.close()


async def get_latest_cpp(user_id: int) -> Optional[Dict]:
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT * FROM cpp_profiles WHERE user_id = ?
            ORDER BY built_at DESC LIMIT 1
        """, (user_id,))
        row = await cursor.fetchone()
        if row:
            result = dict(row)
            result['profile_json'] = json.loads(result['profile_json'])
            return result
        return None
    finally:
        await db.close()


async def get_cpp_by_email(email: str) -> Optional[Dict]:
    user = await get_user_by_email(email)
    if not user:
        return None
    return await get_latest_cpp(user['id'])


# ══════════════════════════════════════════════════════════════
#  SCORE & DLP LOG OPERATIONS
# ══════════════════════════════════════════════════════════════

async def log_score(tenant_id: int, sender_email: str, direction: str, subject: str,
                    score: float, verdict: str, policy_name: str, deviations: Dict,
                    word_count: int = 0, user_id: int = None) -> int:
    db = await get_db()
    try:
        cursor = await db.execute("""
            INSERT INTO score_log (tenant_id, sender_email, user_id, direction, subject,
                                  score, verdict, policy_name, deviations_json, email_word_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (tenant_id, sender_email, user_id, direction, subject,
              score, verdict, policy_name, json.dumps(deviations, default=str), word_count))
        await db.commit()
        return cursor.lastrowid
    finally:
        await db.close()


async def log_dlp_hit(tenant_id: int, sender_email: str, pattern_type: str,
                      match_count: int, confidence: str, compliance_tags: List[str],
                      action_taken: str, details: Dict, score_log_id: int = None):
    db = await get_db()
    try:
        await db.execute("""
            INSERT INTO dlp_hits (tenant_id, sender_email, score_log_id, pattern_type,
                                 match_count, confidence, compliance_tags,
                                 action_taken, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (tenant_id, sender_email, score_log_id, pattern_type, match_count,
              confidence, json.dumps(compliance_tags), action_taken,
              json.dumps(details, default=str)))
        await db.commit()
    finally:
        await db.close()


# ══════════════════════════════════════════════════════════════
#  DASHBOARD / STATS QUERIES
# ══════════════════════════════════════════════════════════════

async def get_stats(tenant_id: int = None, reseller_id: int = None,
                    distributor_id: int = None, hours: int = 24) -> Dict:
    """
    Dashboard stats. Scope depends on which ID is provided:
      - tenant_id: stats for one end customer
      - reseller_id: stats across all that reseller's customers
      - distributor_id: stats across everything (Nathan's view)
    """
    db = await get_db()
    try:
        time_filter = f"datetime('now', '-{hours} hours')"

        # Build tenant filter based on scope
        if tenant_id:
            tenant_clause = f"tenant_id = {tenant_id}"
        elif reseller_id:
            tenant_clause = f"tenant_id IN (SELECT id FROM tenants WHERE reseller_id = {reseller_id})"
        elif distributor_id:
            tenant_clause = (
                f"tenant_id IN (SELECT t.id FROM tenants t "
                f"JOIN resellers r ON t.reseller_id = r.id "
                f"WHERE r.distributor_id = {distributor_id})"
            )
        else:
            tenant_clause = "1=1"

        # Scoring stats
        cursor = await db.execute(f"""
            SELECT verdict, COUNT(*) as count FROM score_log
            WHERE {tenant_clause} AND scored_at > {time_filter}
            GROUP BY verdict
        """)
        score_stats = {r['verdict']: r['count'] for r in await cursor.fetchall()}

        # DLP stats
        cursor = await db.execute(f"""
            SELECT pattern_type, SUM(match_count) as total FROM dlp_hits
            WHERE {tenant_clause} AND detected_at > {time_filter}
            GROUP BY pattern_type
        """)
        dlp_stats = {r['pattern_type']: r['total'] for r in await cursor.fetchall()}

        # User counts
        cursor = await db.execute(f"""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN cpp_status = 'ready' THEN 1 ELSE 0 END) as cpp_ready
            FROM users WHERE {tenant_clause}
        """)
        user_row = await cursor.fetchone()

        # Tenant count (for reseller/distributor view)
        cursor = await db.execute(f"""
            SELECT COUNT(*) as count FROM tenants
            WHERE id IN (SELECT DISTINCT tenant_id FROM users WHERE {tenant_clause})
        """)
        tenant_count = (await cursor.fetchone())['count']

        return {
            "period_hours": hours,
            "tenant_count": tenant_count,
            "user_count": user_row['total'],
            "cpp_ready": user_row['cpp_ready'],
            "scoring": score_stats,
            "dlp": dlp_stats,
        }
    finally:
        await db.close()
