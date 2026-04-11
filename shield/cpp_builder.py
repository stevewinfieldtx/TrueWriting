"""
TrueWriting Shield - CPP Builder (Multi-Tenant)
Pulls sent email via connector, sends to TrueWriting API,
stores CPP + security group memberships per tenant.
"""

import os
import httpx
from typing import Dict, Optional, List
from connectors.microsoft import MicrosoftConnector
import database as db


TRUEWRITING_API = os.getenv("TRUEWRITING_API_URL", "http://localhost:8200")


def _get_connector_for_tenant(tenant: Dict) -> MicrosoftConnector:
    """Create a connector from tenant credentials."""
    return MicrosoftConnector({
        "tenant_id": tenant["ms_tenant_id"],
        "client_id": tenant["ms_client_id"],
        "client_secret": tenant["ms_client_secret"],
    })


async def sync_tenant_users(tenant_id: int) -> Dict:
    """
    Sync users and their security group memberships from M365.
    Does NOT build CPPs — just enumerates and stores user records.
    """
    tenant = await db.get_tenant(tenant_id)
    if not tenant:
        return {"error": f"Tenant {tenant_id} not found"}

    connector = _get_connector_for_tenant(tenant)
    if not await connector.authenticate():
        return {"error": "M365 authentication failed"}

    # Get all users
    users = await connector.list_users()
    print(f"  Syncing {len(users)} users for tenant '{tenant['name']}'...")

    synced = 0
    for user in users:
        # Get their security group memberships
        groups = await connector.get_user_groups(user.user_id)
        group_ids = [g["id"] for g in groups]

        await db.upsert_user(
            tenant_id=tenant_id,
            email=user.email,
            display_name=user.display_name,
            platform_user_id=user.user_id,
            department=user.department or '',
            job_title=user.job_title or '',
            group_ids=group_ids,
        )
        synced += 1

    await db.update_tenant_user_count(tenant_id, synced)
    print(f"  Synced {synced} users for tenant '{tenant['name']}'")
    return {"synced": synced, "tenant": tenant["name"]}


async def sync_tenant_groups(tenant_id: int) -> List[Dict]:
    """
    List all security groups in a tenant's M365.
    Returns them so the admin can map them to policies.
    """
    tenant = await db.get_tenant(tenant_id)
    if not tenant:
        return []

    connector = _get_connector_for_tenant(tenant)
    if not await connector.authenticate():
        return []

    return await connector.list_security_groups()


async def build_cpp_for_user(tenant_id: int, user_email: str,
                             months_back: int = 12) -> Optional[Dict]:
    """Full pipeline: pull sent email → analyze → store CPP."""
    tenant = await db.get_tenant(tenant_id)
    if not tenant:
        print(f"  Tenant {tenant_id} not found")
        return None

    user = await db.get_user_by_email(user_email)
    if not user:
        print(f"  User {user_email} not found. Run sync_tenant_users first.")
        return None

    connector = _get_connector_for_tenant(tenant)
    if not await connector.authenticate():
        return None

    print(f"\n  Building CPP for {user_email}...")

    # Pull sent emails
    emails = await connector.get_sent_emails(user["platform_user_id"], months_back=months_back)
    if not emails:
        print(f"  No sent emails found for {user_email}")
        return None

    texts = [e.body for e in emails if e.body and e.word_count >= 10]
    if len(texts) < 10:
        print(f"  Too few usable emails ({len(texts)}) for {user_email}")
        return None

    total_words = sum(len(t.split()) for t in texts)
    print(f"  Sending {len(texts)} emails ({total_words:,} words) to TrueWriting API...")

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{TRUEWRITING_API}/analyze",
                json={"source_type": "email", "texts": texts, "min_words": 50})
            if resp.status_code != 200:
                print(f"  TrueWriting API error: {resp.status_code} {resp.text[:200]}")
                return None
            profile = resp.json()
    except httpx.ConnectError:
        print(f"  Cannot reach TrueWriting API at {TRUEWRITING_API}")
        return None
    except Exception as e:
        print(f"  TrueWriting API call failed: {e}")
        return None

    await db.store_cpp(user_id=user["id"], profile=profile,
                       email_count=len(texts), word_count=total_words)

    print(f"  CPP stored for {user_email}: {len(texts)} emails, {total_words:,} words")
    return profile


async def build_all_cpps(tenant_id: int, months_back: int = 12) -> Dict:
    """Build CPPs for ALL users in a tenant."""
    users = await db.list_users(tenant_id)
    if not users:
        # Try syncing first
        await sync_tenant_users(tenant_id)
        users = await db.list_users(tenant_id)

    print(f"\n  Starting CPP build for {len(users)} users in tenant {tenant_id}...")
    results = {"success": 0, "failed": 0, "skipped": 0, "users": []}

    for user in users:
        try:
            profile = await build_cpp_for_user(tenant_id, user["email"], months_back)
            if profile:
                results["success"] += 1
                results["users"].append({"email": user["email"], "status": "ready"})
            else:
                results["skipped"] += 1
                results["users"].append({"email": user["email"], "status": "skipped"})
        except Exception as e:
            results["failed"] += 1
            results["users"].append({"email": user["email"], "status": f"error: {e}"})

    print(f"\n  CPP build complete: {results['success']} ready, "
          f"{results['skipped']} skipped, {results['failed']} failed")
    return results
