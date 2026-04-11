# TrueWriting Shield v0.2.0 (MSP Edition)

**Behavioral Email Security + DLP — Built for Distributors and MSPs**

## Architecture

```
Rain Networks (Distributor)
├── Reseller A (small MSP)
│   ├── Acme Corp (M365 tenant, 50 users)
│   │   ├── "Finance Team" security group → strict policy (hold at 0.4)
│   │   ├── "Executives" security group → strict + IT notified
│   │   └── Everyone else → default policy (warn at 0.35)
│   └── Beta Inc (M365 tenant, 200 users)
├── Reseller B
│   └── Gamma LLC (Google Workspace, future)
└── Rain Direct (is_direct=true, Rain acting as reseller)
    └── Delta Corp (M365 tenant, 500 users)
```

**Policy cascade:** Distributor defaults → Reseller overrides → Tenant overrides → Security group policy

## Quick Start

### 1. Install + run

```bash
cd C:\Users\steve\Documents\TrueWriting\shield
pip install -r requirements.txt --break-system-packages

# Terminal 1: TrueWriting API
cd C:\Users\steve\Documents\TrueWriting
py -m uvicorn api:app --port 8200

# Terminal 2: Shield
cd C:\Users\steve\Documents\TrueWriting\shield
py -m uvicorn service:app --port 8300 --reload
```

### 2. Set up the hierarchy

```bash
# Create distributor (Rain Networks)
curl -X POST http://localhost:8300/admin/distributors \
  -H "Content-Type: application/json" \
  -d '{"name": "Rain Networks", "contact_name": "Nathan Ware"}'
# Returns: {"id": 1}

# Create reseller (or Rain Direct)
curl -X POST http://localhost:8300/admin/resellers \
  -H "Content-Type: application/json" \
  -d '{"distributor_id": 1, "name": "Rain Direct", "is_direct": true}'
# Returns: {"id": 1}

# Onboard a customer (tenant)
curl -X POST http://localhost:8300/admin/tenants \
  -H "Content-Type: application/json" \
  -d '{
    "reseller_id": 1,
    "name": "Steve Test Org",
    "domain": "yourdomain.com",
    "ms_tenant_id": "719cc79f-94cd-4f2b-ba5e-9b40a5583843",
    "ms_client_id": "81b707d5-f472-4f5f-a555-6e9e85f521ce",
    "ms_client_secret": "YOUR_SECRET_HERE"
  }'
# Returns: {"id": 1, "note": "Default policy created automatically"}
```

### 3. Sync users + build CPPs

```bash
# Pull users and their security groups from M365
curl -X POST http://localhost:8300/admin/tenants/1/sync

# Build CPPs for all users
curl -X POST http://localhost:8300/admin/tenants/1/build-cpps
```

### 4. Configure policies per security group

```bash
# See what security groups exist in the tenant
curl http://localhost:8300/admin/tenants/1/groups

# Create a strict policy for the finance team
curl -X POST http://localhost:8300/admin/tenants/1/policies \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Finance - Strict",
    "score_threshold_warn": 0.25,
    "score_threshold_hold": 0.40,
    "dlp_action": "hold",
    "notify_sender": 1,
    "notify_manager": 1,
    "notify_it": 1,
    "notify_emails": ["security@yourdomain.com"]
  }'
# Returns: {"id": 2}

# Map the Finance security group to this policy
curl -X POST http://localhost:8300/admin/tenants/1/groups/map \
  -H "Content-Type: application/json" \
  -d '{
    "group_id": "azure-ad-group-id-here",
    "group_name": "Finance Team",
    "policy_id": 2,
    "priority": 10
  }'
```

### 5. Score an email

```bash
curl -X POST http://localhost:8300/score \
  -H "Content-Type: application/json" \
  -d '{
    "sender_email": "cfo@yourdomain.com",
    "body": "Dear Team, Please process the attached wire transfer.",
    "subject": "Urgent Wire Transfer"
  }'
```

Response includes which policy was applied and why:
```json
{
  "score": 0.52,
  "verdict": "hold",
  "policy_applied": {
    "name": "Finance - Strict",
    "source": "security_group:Finance Team",
    "warn_threshold": 0.25,
    "hold_threshold": 0.40
  },
  "deviations": { ... }
}
```

### 6. Debug: "Why did this email get held?"

```bash
curl http://localhost:8300/admin/resolve-policy/cfo@yourdomain.com
```

Returns the full cascade: which distributor/reseller/tenant/group produced the effective policy.

## File Structure

```
shield/
├── .env.example          # Config (no tenant creds — those go in DB)
├── requirements.txt
├── service.py            # FastAPI with full MSP admin endpoints
├── database.py           # SQLite: distributors → resellers → tenants → groups → policies
├── scoring.py            # Policy-aware CPP scoring + DLP
├── cpp_builder.py        # Per-tenant user sync + CPP build
├── connectors/
│   ├── base.py           # Abstract interface
│   ├── microsoft.py      # M365 Graph API (users, groups, sent mail)
│   └── google.py         # Placeholder
└── dlp/
    └── scanner.py        # 11 patterns, Luhn, confidence, compliance tags
```

## API Reference

### Scoring (transport rule integration)
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/score` | POST | Score email (auto-resolves policy from sender) |
| `/dlp/scan` | POST | DLP-only scan |

### Distributor Admin
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/admin/distributors` | POST/GET | Create / list distributors |
| `/admin/distributors/{id}/stats` | GET | Cross-reseller stats |
| `/admin/distributors/{id}/resellers` | GET | List resellers |

### Reseller Admin
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/admin/resellers` | POST | Create reseller |
| `/admin/resellers/{id}/tenants` | GET | List tenants |
| `/admin/resellers/{id}/stats` | GET | Cross-tenant stats |

### Tenant Admin
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/admin/tenants` | POST | Onboard end customer |
| `/admin/tenants/{id}/sync` | POST | Sync M365 users + groups |
| `/admin/tenants/{id}/build-cpps` | POST | Build all CPPs |
| `/admin/tenants/{id}/users` | GET | List users + CPP status |
| `/admin/tenants/{id}/stats` | GET | Tenant stats |

### Policy & Security Groups
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/admin/tenants/{id}/policies` | POST/GET | Create / list policies |
| `/admin/tenants/{id}/groups` | GET | List M365 security groups |
| `/admin/tenants/{id}/groups/map` | POST | Map group → policy |
| `/admin/tenants/{id}/groups/mapped` | GET | Show current mappings |
| `/admin/resolve-policy/{email}` | GET | Debug: show effective policy for a user |
