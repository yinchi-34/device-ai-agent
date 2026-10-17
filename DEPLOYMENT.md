# Deployment Guide

[中文](DEPLOYMENT.zh-CN.md)

How to run Device AI Agent on a cloud server for internal production use. The
steps are provider-neutral: any cloud with Linux VMs, a layer-7 load balancer
and security groups (firewall rules) works the same way.

## Contents

- [Target layout](#target-layout)
- [What you need](#what-you-need)
- [1. Identity provider](#1-identity-provider)
- [2. Server](#2-server)
- [3. Configuration](#3-configuration)
- [4. First start](#4-first-start)
- [5. Load balancer and firewall](#5-load-balancer-and-firewall)
- [6. Verify](#6-verify)
- [Upgrades](#upgrades)
- [Backups and restore](#backups-and-restore)
- [Logs and monitoring](#logs-and-monitoring)
- [Security checklist](#security-checklist)

## Target layout

```text
users / programs
      │  HTTPS 443
      ▼
load balancer ── terminates HTTPS, holds the certificate, health-checks /health
      │  HTTP, private network only
      ▼
server (security group: only the load balancer may reach port 8000)
  gateway (Caddy) :8000 ── IP allowlist, login via oauth2-proxy
    ├── oauth2-proxy ── talks to the identity provider
    └── control-api ── verifies the JWT again, writes the audit trail
          ├── db (PostgreSQL + pgvector, volume db-data)
          ├── redis
          └── document-worker (volume document-files)
```

Only the gateway listens on the server's private address. The API, database,
Redis and oauth2-proxy are reachable only inside Docker's network (the
database also on `127.0.0.1` for maintenance).

## What you need

- A Linux VM: 2 vCPU and 4 GB RAM is enough to start; add memory if you run
  large OCR jobs. Docker Engine with Compose v2.
- A domain name, for example `device.example.com`, and a certificate for it on
  the load balancer.
- An OIDC identity provider (Keycloak, Feishu, Azure AD, Okta, ...). Do not use
  the bundled local Keycloak in production.
- API keys for the model providers you use (extraction, vision, embeddings,
  diagnosis).

## 1. Identity provider

Register three kinds of things (details in
[Using a real identity provider](DEVELOPMENT.md#using-a-real-identity-provider)):

1. **The gateway**: a confidential OIDC client with redirect URI
   `https://device.example.com/oauth2/callback`. Its client id becomes
   `DEVICE_OIDC_AUDIENCE`; its secret becomes `DEVICE_GATEWAY_CLIENT_SECRET`.
2. **People**: users who may sign in, with a verified email address.
3. **Programs**: one client-credentials client per program, with an audience
   mapper adding the gateway's client id to `aud` and an `email` claim, so the
   audit trail names the program.

Copy `issuer`, `authorization_endpoint`, `token_endpoint` and `jwks_uri` from
the provider's `/.well-known/openid-configuration`.

## 2. Server

```bash
# as a sudo user on the server
sudo apt-get update && sudo apt-get install -y git
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"   # log out and back in

sudo mkdir -p /opt/device-ai-agent && sudo chown "$USER" /opt/device-ai-agent
git clone https://github.com/yinchi-34/device-ai-agent.git /opt/device-ai-agent
cd /opt/device-ai-agent
git checkout <release tag or commit>   # deploy a fixed version, not a moving branch
```

## 3. Configuration

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env`. Every value below must change from the example:

| Variable | Production value |
| --- | --- |
| `COMPOSE_PROFILES` | Delete the line (no local Keycloak) |
| `DEVICE_DATABASE_PASSWORD` | Long random value, URL-safe characters only |
| `DEVICE_ADMIN_PASSWORD` | Long random value for the `/admin` console |
| `DEVICE_OIDC_ISSUER` | `issuer` from the provider |
| `DEVICE_OIDC_AUDIENCE` | The gateway client id |
| `DEVICE_OIDC_LOGIN_URL` | `authorization_endpoint` |
| `DEVICE_OIDC_TOKEN_URL` | `token_endpoint` |
| `DEVICE_OIDC_JWKS_URL` | `jwks_uri` |
| `DEVICE_GATEWAY_PUBLIC_URL` | `https://device.example.com` |
| `DEVICE_GATEWAY_CLIENT_SECRET` | The gateway client secret |
| `DEVICE_GATEWAY_COOKIE_SECRET` | New random value, see below |
| `DEVICE_GATEWAY_COOKIE_SECURE` | `true` |
| `DEVICE_GATEWAY_BIND` | The server's private IP, e.g. `10.0.1.15` |
| `DEVICE_GATEWAY_ALLOWED_CIDRS` | The load balancer subnet, e.g. `10.0.0.0/24` |
| `DEVICE_DOCUMENT_*`, `DEVICE_DIAGNOSIS_*` | Your model endpoints, models and keys |

Generate secrets with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"   # cookie secret (32 chars)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # passwords
```

Keep `.env` out of version control and out of chat. If your cloud has a secret
manager, you can inject the same variables from it instead of keeping them in
the file; the application only reads environment variables.

## 4. First start

```bash
docker compose build
docker compose up -d db redis
docker compose run --rm control-api alembic upgrade head
docker compose up -d
docker compose ps        # every service should be "running" / "healthy"
```

All services restart automatically after a crash or a reboot.

## 5. Load balancer and firewall

Load balancer (layer 7, often called ALB/CLB/Application Load Balancer):

- Listener **HTTPS 443** with the certificate for `device.example.com`.
- Optional listener HTTP 80 that redirects to HTTPS.
- Backend: the server's private IP, **port 8000, protocol HTTP**.
- Health check: `GET /health`, expect `200`.
- Idle timeout of at least 120 s, so long uploads are not cut off.
- Request body limit of at least 30 MB (uploads are capped at 25 MB by
  `DEVICE_DOCUMENT_MAX_FILE_BYTES`).

Server security group, inbound:

| Port | Source | Why |
| --- | --- | --- |
| 8000/tcp | Load balancer subnet only | Gateway |
| 22/tcp | Your admin IPs only (or use the cloud's session manager) | SSH |
| everything else | denied | |

No inbound rule is needed for the database, Redis or the identity provider.
The server needs outbound HTTPS to the identity provider and model providers.

Without a load balancer, set `DEVICE_GATEWAY_SITE_ADDRESS=device.example.com`,
publish ports 80 and 443 instead of 8000, and Caddy obtains and renews the
certificate itself.

## 6. Verify

```bash
curl -s https://device.example.com/health                     # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' https://device.example.com/api/v1/device-models   # 401
```

Then open `https://device.example.com/docs` in a browser: you should be sent to
the identity provider and come back to the API docs after signing in. For a
program, request a client-credentials token from the provider and call the API
with `Authorization: Bearer <token>`; expect `200`.

## Upgrades

```bash
cd /opt/device-ai-agent
./backup.sh                                   # see below; always back up first
git fetch --tags && git checkout <new tag>
docker compose build
docker compose run --rm control-api alembic upgrade head
docker compose up -d
docker compose ps
```

Migrations run before the new containers start. To roll back, check out the
previous tag and restore the backup taken before the upgrade; do not run
`alembic downgrade` on production data without testing it on a copy first.

## Backups and restore

Two things hold state and must be backed up together: the database
(metadata, review history, embeddings) and the `document-files` volume
(original uploads). Save this as `/opt/device-ai-agent/backup.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd /opt/device-ai-agent
set -a; . ./.env; set +a
stamp=$(date +%Y%m%d-%H%M%S)
mkdir -p backups
docker compose exec -T db pg_dump -U "${DEVICE_DATABASE_USER:-device}" -Fc \
  "${DEVICE_DATABASE_NAME:-device_ai_agent}" > "backups/db-$stamp.dump"
docker compose exec -T control-api tar czf - -C /data/documents . \
  > "backups/documents-$stamp.tgz"
find backups -type f -mtime +14 -delete
```

```bash
chmod 700 backup.sh
( crontab -l 2>/dev/null; echo "30 2 * * * /opt/device-ai-agent/backup.sh >> /var/log/device-backup.log 2>&1" ) | crontab -
```

Copy `backups/` off the server as well (object storage or another host); a
backup on the same disk does not survive losing the disk. Cloud disk snapshots
are a good extra layer.

Restore (stop writers first):

```bash
docker compose stop control-api document-worker gateway
docker compose exec -T db pg_restore -U device -d device_ai_agent --clean --if-exists \
  < backups/db-<stamp>.dump
docker compose run --rm --no-deps -T control-api sh -c 'tar xzf - -C /data/documents' \
  < backups/documents-<stamp>.tgz
docker compose up -d
```

Test a restore on a spare server at least once before you rely on it.

## Logs and monitoring

```bash
docker compose logs -f gateway control-api document-worker
docker compose logs --since 1h oauth2-proxy      # sign-in problems
```

Container logs are rotated (10 MB × 5 files per service). For a minimum of
monitoring:

- alert on the load balancer health check failing;
- alert on disk usage above 80% (uploads and backups grow);
- check `/var/log/device-backup.log` or the newest file in `backups/` daily.

## Security checklist

- [ ] `COMPOSE_PROFILES=local-idp` removed; no example secrets left in `.env`.
- [ ] `.env` is `chmod 600` and not in git.
- [ ] `DEVICE_GATEWAY_COOKIE_SECURE=true` and the public URL uses `https://`.
- [ ] Security group allows port 8000 only from the load balancer.
- [ ] `DEVICE_GATEWAY_ALLOWED_CIDRS` is narrowed to the load balancer subnet.
- [ ] Identity provider: only the intended users can sign in to the gateway
      client; each program has its own client and secret.
- [ ] Backups run nightly, are copied off the server, and a restore was tested.
- [ ] The deployed version is a tag or commit, and upgrades follow the steps
      above.
