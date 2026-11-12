# Security

## What is protected

Client spreadsheets and everything derived from them (tables, findings, review workbooks, exports), the access
token, and the audit trail.

## Threats and controls

| Threat | Control | Where |
|---|---|---|
| Anyone on the network reads or deletes runs | Access token on every `/api/`, `/jobs`, `/review/` path; refusal to listen beyond loopback without one; `DQ_ENV=production` refuses to start without a token and a hash key | `security/auth.py`, `api/api_server.py`, `orchestration/settings.py` |
| Token guessing | 1 s delay per failure; an address is locked out after 5 failures in 5 minutes (sign-in and wrong bearer tokens); constant-time comparison | `security/auth.py` |
| Session theft / CSRF | Cookie is HttpOnly, SameSite=Strict, Secure over HTTPS, and holds an HMAC of the token, not the token | `security/auth.py` |
| XSS, clickjacking, MIME sniffing | Content-Security-Policy (`script-src 'self'`, `frame-ancestors 'none'`), `X-Frame-Options: DENY`, `nosniff`, `no-referrer`, HSTS over HTTPS; React escapes output | `observability/http.py` |
| Path traversal | Upload names and relative paths validated; run and file ids matched against patterns; Job Contract refs resolved and confined to the storage root | `security/uploads.py`, `structure/job.py` |
| Malicious workbooks | XML parsed with entities, DTDs and network off; zip-bomb limits (size, ratio, sheets, cells); every job in its own process with a timeout | `structure/xlsx.py`, `orchestration/runner.py` |
| Oversized requests | Streaming uploads with byte limits; proxy body limits | `api/v1.py`, `deploy/Caddyfile` |
| Server-side request forgery | Job callbacks only to http(s) hosts on `STRUCTURE_CALLBACK_HOSTS`; a callback needs a job token | `api/api_server.py` |
| Code injection through the agent | The model can only return typed operations; formulas go through an AST allow-list; no `eval`, no shell, no SQL from the model | `agent/operations.py`, `agent/executors.py` |
| Prompt injection from cell text | Cell text is JSON data in the prompt; the plan is validated against the registry and real column names; a person approves the preview | `agent/planner/llm.py` |
| Regex denial of service | Patterns limited to 500 characters and 0.25 s per match | `agent/safe_regex.py` |
| Destructive agent changes | Guardrails require an explicit confirmation; undo | `agent/guardrails.py` |
| Data leaving the host through the model | Model address must be loopback or on `DQ_LLM_ALLOWED_HOSTS`; the Compose model network has no route out | `structure/llm/client.py`, `docker-compose.yml` |
| Secrets in logs | Logs carry ids, counts and codes; audit rows never hold values, prompts or tokens (tested) | `observability/logs.py`, `storage/audit.py` |
| Container escape / privilege | Non-root user, read-only root file system, all capabilities dropped, `no-new-privileges` | `Dockerfile`, `docker-compose.yml` |
| Supply chain | Exact versions with hashes; `pip-audit`, `npm audit`, `bandit` in CI | `requirements*.txt`, `.github/workflows/ci.yml` |
| Data kept forever | Optional retention purge; delete on request; both audited | `orchestration/orchestrator.py` |

## Secrets

`DQ_API_TOKEN` and `STRUCTURE_HASH_KEY` come from the environment (`.env` for Compose, never committed; a secrets
manager in a larger set-up). Rotating the token signs every browser out. Do not put the token in `settings.yaml`.

## Not covered (known gaps)

| Gap | Consequence | What would close it |
|---|---|---|
| One shared token, no user accounts or roles | The audit trail cannot name a person; no reviewer-only approvals | Single sign-on (OIDC) |
| No separation between clients | Everyone with the token sees every run | Tenant id on runs, checks on every access |
| No virus scan of uploads | A malicious file is stored (it is parsed defensively, not executed) | ClamAV at the proxy or in the upload path |
| LibreOffice runs in the service container | A LibreOffice exploit runs as the service user | A separate, network-less converter container |
| Lockout state is in memory | A restart clears it; it is per instance | Shared store once there are several instances |
| Review workbooks and `pipeline.log` of Phases B–D contain cell values | They are client data on disk, by design | Disk encryption on the host |
| No penetration test, no external review | Unknown unknowns | Schedule one before wider use |

## Reporting

Report a suspected vulnerability privately to the repository owner; do not open a public issue.
