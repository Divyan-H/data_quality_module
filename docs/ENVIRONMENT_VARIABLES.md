# Environment variables

Environment variables override `config/settings.yaml` (`service:` section). Secrets are marked **secret**.

## Service

| Variable | Default | Meaning |
|---|---|---|
| `DQ_ENV` | `development` | `development`, `test`, `staging` or `production`. `production` refuses to start without `DQ_API_TOKEN` and `STRUCTURE_HASH_KEY`, forbids `DQ_ALLOW_UNAUTHENTICATED`, and turns off cross-origin access |
| `DQ_API_TOKEN` | none | **secret.** Access token, 16+ characters. Without it the service only listens on 127.0.0.1 |
| `DQ_HOST` / `DQ_PORT` | `127.0.0.1` / `8000` | Listen address |
| `DQ_SETTINGS` | `config/settings.yaml` | Settings file |
| `DQ_WORKSPACE` | `data/runs` | Run folders |
| `DQ_AUDIT_DB` | `<workspace>/../audit.db` | Audit database file |
| `DQ_MAX_CONCURRENT_JOBS` | `2` | Worker processes at once |
| `DQ_RETENTION_DAYS` | none | Delete runs untouched for this many days |
| `DQ_SHUTDOWN_GRACE_S` | `0` | On shutdown, let running jobs finish for this long |
| `DQ_TRUSTED_PROXIES` | `127.0.0.1` | Proxies whose `X-Forwarded-*` headers are trusted (uvicorn `forwarded_allow_ips`) |
| `DQ_CORS_ORIGINS` | dev servers | Comma-separated origins allowed cross-origin; empty in production |
| `DQ_ALLOW_UNAUTHENTICATED` | unset | `1` allows a network address without a token. Development only |
| `DQ_LOG_LEVEL` / `DQ_LOG_FORMAT` | `INFO` / `json` | `text` gives plain lines |

## Local model

| Variable | Default | Meaning |
|---|---|---|
| `STRUCTURE_LLM_URL` | `http://localhost:11434` | Ollama address |
| `DQ_LLM_ALLOWED_HOSTS` | none | Hosts other than this machine that prompts may be sent to (exact names, comma-separated) |
| `DQ_LLM_MODEL` | `llama3.2:3b` | Model for Phase A's review |
| `DQ_AGENT_MODEL` | same as above | Model for the agent |
| `STRUCTURE_LLM_ALLOW_REMOTE` | unset | Legacy: `1` allows **any** host. Prefer the allow-list. In production it also needs `DQ_ACK_REMOTE_LLM=1` |

## Phase A and the Job Contract

| Variable | Default | Meaning |
|---|---|---|
| `STRUCTURE_HASH_KEY` | development key | **secret.** Key for value hashes. Required in production |
| `STRUCTURE_STORAGE_ROOT` | `./data/storage` | Root that Job Contract refs resolve under (and are confined to) |
| `STRUCTURE_CALLBACK_HOSTS` | any http(s) host | Hosts a job callback may be sent to. Set it on any shared server |

## Compose only (`.env`)

`DQ_DOMAIN` (the name in the browser), `DQ_VERSION` (image tag).
