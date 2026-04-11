"""
TrueWriting Shield - Main Service (MSP / Distributor Ready)

Hierarchy: Distributor → Reseller → Tenant → Security Group → Policy

Endpoints:

  SCORING (called by mail transport rules):
    POST /score                          - Score an email (CPP + DLP)
    POST /dlp/scan                       - DLP-only scan

  DISTRIBUTOR ADMIN (Nathan's view):
    POST /admin/distributors             - Create distributor
    GET  /admin/distributors             - List distributors
    GET  /admin/distributors/{id}/stats  - Distributor-wide stats

  RESELLER ADMIN:
    POST /admin/resellers                - Create reseller under a distributor
    GET  /admin/distributors/{id}/resellers - List resellers

  TENANT ADMIN:
    POST /admin/tenants                  - Create tenant (end customer)
    GET  /admin/resellers/{id}/tenants   - List tenants for a reseller
    POST /admin/tenants/{id}/sync        - Sync users + groups from M365
    POST /admin/tenants/{id}/build-cpps  - Build CPPs for all users
    GET  /admin/tenants/{id}/stats       - Tenant stats
    GET  /admin/tenants/{id}/users       - List users

  POLICY ADMIN:
    POST /admin/tenants/{id}/policies       - Create policy
    GET  /admin/tenants/{id}/policies       - List policies
    GET  /admin/tenants/{id}/groups         - List M365 security groups
    POST /admin/tenants/{id}/groups/map     - Map a group to a policy

  HEALTH:
    GET  /health                         - Health check
"""

import os
from contextlib import asynccontextmanager
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List

import database as db
from scoring import ScoringEngine
from dlp.scanner import DLPScanner
from cpp_builder import (
    build_cpp_for_user, build_all_cpps,
    sync_tenant_users, sync_tenant_groups,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    print("=" * 60)
    print("  TrueWriting Shield v0.2.0 (MSP Edition)")
    print("  Distributor → Reseller → Tenant → Group → Policy")
    print("=" * 60)
    yield

app = FastAPI(title="TrueWriting Shield", version="0.2.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

scoring_engine = ScoringEngine()


# ── Request Models ───────────────────────────────────────────

class ScoreRequest(BaseModel):
    sender_email: str
    body: str
    subject: Optional[str] = ''
    direction: Optional[str] = 'outbound'

class DLPScanRequest(BaseModel):
    text: str
    subject: Optional[str] = ''

class CreateDistributorRequest(BaseModel):
    name: str
    contact_email: Optional[str] = ''
    contact_name: Optional[str] = ''

class CreateResellerRequest(BaseModel):
    distributor_id: int
    name: str
    is_direct: Optional[bool] = False
    contact_email: Optional[str] = ''
    contact_name: Optional[str] = ''

class CreateTenantRequest(BaseModel):
    reseller_id: int
    name: str
    domain: Optional[str] = ''
    platform: Optional[str] = 'm365'
    ms_tenant_id: Optional[str] = ''
    ms_client_id: Optional[str] = ''
    ms_client_secret: Optional[str] = ''

class CreatePolicyRequest(BaseModel):
    name: str
    description: Optional[str] = ''
    score_threshold_warn: Optional[float] = 0.35
    score_threshold_hold: Optional[float] = 0.55
    dlp_enabled: Optional[int] = 1
    dlp_min_confidence: Optional[str] = 'medium'
    dlp_action: Optional[str] = 'warn'
    notify_sender: Optional[int] = 1
    notify_manager: Optional[int] = 0
    notify_it: Optional[int] = 0
    notify_emails: Optional[List[str]] = []
    auto_release_minutes: Optional[int] = 0

class MapGroupRequest(BaseModel):
    group_id: str
    group_name: str
    policy_id: int
    priority: Optional[int] = 0

class BuildCPPsRequest(BaseModel):
    months_back: Optional[int] = 12


# ══════════════════════════════════════════════════════════════
#  SCORING ENDPOINTS (called by mail transport rules)
# ══════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {"status": "ok", "service": "truewriting-shield", "version": "0.2.0-msp"}


@app.post("/score")
async def score_email(req: ScoreRequest):
    """Score an email. Policy is auto-resolved from sender's tenant + security groups."""
    result = await scoring_engine.score_email(
        sender_email=req.sender_email, body=req.body,
        subject=req.subject or '', direction=req.direction or 'outbound')
    return result.to_dict()


@app.post("/dlp/scan")
async def dlp_scan(req: DLPScanRequest):
    """Standalone DLP scan (no CPP, no policy resolution)."""
    scanner = DLPScanner(min_confidence="medium")
    result = scanner.scan(req.text, req.subject or '')
    return result.to_dict()


# ══════════════════════════════════════════════════════════════
#  DISTRIBUTOR ADMIN
# ══════════════════════════════════════════════════════════════

@app.post("/admin/distributors")
async def create_distributor(req: CreateDistributorRequest):
    dist_id = await db.create_distributor(req.name, req.contact_email, req.contact_name)
    return {"id": dist_id, "name": req.name}


@app.get("/admin/distributors")
async def list_distributors():
    return {"distributors": await db.list_distributors()}


@app.get("/admin/distributors/{distributor_id}/stats")
async def distributor_stats(distributor_id: int, hours: int = 24):
    """Nathan's view: stats across ALL resellers and ALL tenants."""
    return await db.get_stats(distributor_id=distributor_id, hours=hours)


@app.get("/admin/distributors/{distributor_id}/resellers")
async def list_resellers(distributor_id: int):
    return {"resellers": await db.list_resellers(distributor_id)}


# ══════════════════════════════════════════════════════════════
#  RESELLER ADMIN
# ══════════════════════════════════════════════════════════════

@app.post("/admin/resellers")
async def create_reseller(req: CreateResellerRequest):
    reseller_id = await db.create_reseller(
        req.distributor_id, req.name, req.is_direct,
        req.contact_email, req.contact_name)
    return {"id": reseller_id, "name": req.name}


@app.get("/admin/resellers/{reseller_id}/tenants")
async def list_tenants(reseller_id: int):
    return {"tenants": await db.list_tenants(reseller_id)}


@app.get("/admin/resellers/{reseller_id}/stats")
async def reseller_stats(reseller_id: int, hours: int = 24):
    """Reseller's view: stats across their tenants only."""
    return await db.get_stats(reseller_id=reseller_id, hours=hours)


# ══════════════════════════════════════════════════════════════
#  TENANT ADMIN
# ══════════════════════════════════════════════════════════════

@app.post("/admin/tenants")
async def create_tenant(req: CreateTenantRequest):
    tenant_id = await db.create_tenant(
        reseller_id=req.reseller_id, name=req.name, domain=req.domain,
        platform=req.platform, ms_tenant_id=req.ms_tenant_id,
        ms_client_id=req.ms_client_id, ms_client_secret=req.ms_client_secret)
    return {"id": tenant_id, "name": req.name, "note": "Default policy created automatically"}


@app.post("/admin/tenants/{tenant_id}/sync")
async def sync_tenant(tenant_id: int):
    """Sync users and their security group memberships from M365."""
    result = await sync_tenant_users(tenant_id)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/admin/tenants/{tenant_id}/build-cpps")
async def build_tenant_cpps(tenant_id: int, req: BuildCPPsRequest = None):
    """Build CPPs for all users in a tenant."""
    months = req.months_back if req else 12
    result = await build_all_cpps(tenant_id, months_back=months)
    return result


@app.get("/admin/tenants/{tenant_id}/stats")
async def tenant_stats(tenant_id: int, hours: int = 24):
    return await db.get_stats(tenant_id=tenant_id, hours=hours)


@app.get("/admin/tenants/{tenant_id}/users")
async def list_tenant_users(tenant_id: int):
    return {"users": await db.list_users(tenant_id)}


# ══════════════════════════════════════════════════════════════
#  POLICY ADMIN
# ══════════════════════════════════════════════════════════════

@app.post("/admin/tenants/{tenant_id}/policies")
async def create_policy(tenant_id: int, req: CreatePolicyRequest):
    """Create a policy for a tenant. Assign it to security groups separately."""
    policy_id = await db.create_policy(
        tenant_id=tenant_id, name=req.name,
        description=req.description,
        score_threshold_warn=req.score_threshold_warn,
        score_threshold_hold=req.score_threshold_hold,
        dlp_enabled=req.dlp_enabled,
        dlp_min_confidence=req.dlp_min_confidence,
        dlp_action=req.dlp_action,
        notify_sender=req.notify_sender,
        notify_manager=req.notify_manager,
        notify_it=req.notify_it,
        notify_emails=req.notify_emails,
        auto_release_minutes=req.auto_release_minutes,
    )
    return {"id": policy_id, "name": req.name}


@app.get("/admin/tenants/{tenant_id}/policies")
async def list_tenant_policies(tenant_id: int):
    return {"policies": await db.list_policies(tenant_id)}


@app.get("/admin/tenants/{tenant_id}/groups")
async def list_tenant_groups(tenant_id: int):
    """
    List security groups from M365 (live query).
    Use this to discover groups, then map them to policies.
    """
    groups = await sync_tenant_groups(tenant_id)
    # Also show what's already mapped
    mapped = await db.list_security_groups(tenant_id)
    mapped_ids = {m["group_id"]: m for m in mapped}

    result = []
    for g in groups:
        entry = {
            "group_id": g["id"],
            "group_name": g["name"],
            "description": g.get("description", ""),
            "mapped": g["id"] in mapped_ids,
        }
        if g["id"] in mapped_ids:
            entry["mapped_policy"] = mapped_ids[g["id"]].get("policy_name", "")
            entry["priority"] = mapped_ids[g["id"]].get("priority", 0)
        result.append(entry)

    return {"groups": result, "total": len(result)}


@app.post("/admin/tenants/{tenant_id}/groups/map")
async def map_group_to_policy(tenant_id: int, req: MapGroupRequest):
    """Map a security group to a policy. Higher priority wins when user is in multiple groups."""
    sg_id = await db.map_security_group(
        tenant_id=tenant_id, policy_id=req.policy_id,
        group_id=req.group_id, group_name=req.group_name,
        priority=req.priority)
    return {"id": sg_id, "group": req.group_name, "policy_id": req.policy_id}


@app.get("/admin/tenants/{tenant_id}/groups/mapped")
async def list_mapped_groups(tenant_id: int):
    """List all security group → policy mappings for a tenant."""
    return {"mappings": await db.list_security_groups(tenant_id)}


# ══════════════════════════════════════════════════════════════
#  CONVENIENCE: Resolve policy for a specific user
# ══════════════════════════════════════════════════════════════

@app.get("/admin/resolve-policy/{email}")
async def resolve_user_policy(email: str):
    """
    Show exactly which policy would apply to this user and why.
    Useful for debugging: "Why did Steve's email get held?"
    """
    user = await db.get_user_by_email(email)
    if not user:
        raise HTTPException(status_code=404, detail=f"User {email} not found")

    policy = await db.resolve_effective_policy(user["tenant_id"], user.get("group_ids", []))
    return {
        "user_email": email,
        "tenant_id": user["tenant_id"],
        "group_ids": user.get("group_ids", []),
        "effective_policy": policy,
    }


# ══════════════════════════════════════════════════════════════
#  RUN
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("SHIELD_PORT", "8300"))
    uvicorn.run(app, host="0.0.0.0", port=port)
