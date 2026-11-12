# Deployment

Target: **one Linux server** with Docker and Docker Compose. Three containers: Caddy (TLS), the service, Ollama.

## Prerequisites

| Item | Minimum | Notes |
|---|---|---|
| CPU / memory for the service | 4 cores, 8 GB | Phase A needs about 1.4 GB per 400,000 rows |
| Model | 8 GB memory for a 7B model; a GPU with 8+ GB is strongly recommended | CPU-only works with `llama3.2:3b`, slowly |
| Disk | 50 GB+ | Runs, models (4–10 GB each), backups |
| Name | A DNS name for TLS, or `localhost` for a trial | |

## First deployment

```bash
cp .env.example .env
```

Set `DQ_API_TOKEN` (`openssl rand -base64 32`), `STRUCTURE_HASH_KEY` (`openssl rand -hex 32`) and `DQ_DOMAIN`.

```bash
docker compose up -d --build
```

```bash
docker compose --profile setup run --rm model-pull qwen2.5:7b-instruct
```

```bash
docker compose --profile setup run --rm model-pull llama3.2:3b
```

The model container has no internet access; `model-pull` is a one-off helper that shares its volume.

## Verify

```bash
docker compose ps
```

```bash
docker compose exec app python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/ready').read().decode())"
```

`ready: true`, `disk_ok: true`, and `llm_available: true` once a model is pulled. Then open `https://<DQ_DOMAIN>`,
sign in with the token, upload a file and run Phase A.

## Without LibreOffice / with a GPU

`docker build --build-arg WITH_LIBREOFFICE=0 …` gives a smaller image without `.xls / .xlsb / .ods` support.
For a GPU, install the NVIDIA container toolkit and uncomment the `reservations` block of the `ollama` service.

## Pinning images

`ollama/ollama:latest` and the base images move. For production, replace tags with digests
(`docker inspect --format '{{index .RepoDigests 0}}' ollama/ollama:latest`) and rebuild deliberately.

## Upgrade and rollback

```bash
git pull && DQ_VERSION=$(git rev-parse --short HEAD) docker compose up -d --build
```

Running jobs get 30 s to finish (`DQ_SHUTDOWN_GRACE_S`); anything still running shows as *interrupted* and can be
started again. To roll back, check out the previous commit and run the same command with its version. The audit
database migrates forward only: an older build refuses a newer `audit.db`, so restore the backup taken before the
upgrade if a release added a migration (the changelog says when one does).

## Backup

Nightly, while no job runs: see `docs/DATABASE.md` (`sqlite3 .backup` for `audit.db`, `tar` for `runs` and
`recipes`). Volumes: `dq_data`, `ollama_models` (re-downloadable), `caddy_data` (certificates).

## Monitoring

- Logs: JSON lines on stdout (`docker compose logs app`); ship them with your collector.
- Metrics: `GET /api/v1/metrics` with the token, from inside the `edge` network (the proxy does not expose it).
- Alert on: `ready` not 200, `dq_jobs_finished_total{outcome="crash"}` rising, `dq_workspace_free_mb` low,
  `dq_llm_available` 0.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Refusing to start with DQ_ENV=production` | A required secret is missing; the message lists which |
| Browser shows a certificate warning | `DQ_DOMAIN` is not public: trust Caddy's local CA or supply your certificate |
| `llm_available: false` | No model pulled, or the name in `.env` differs from the pulled one |
| `507 disk_full` | Free space is under `min_free_disk_mb`; delete old runs or set `DQ_RETENTION_DAYS` |
| `429 busy` | The job limit is reached; raise `DQ_MAX_CONCURRENT_JOBS` if memory allows |
| `.xls` files rejected | The image was built without LibreOffice |

## What has and has not been verified

Verified on 2026-11-12, by running it:

- The image builds (without LibreOffice) and runs as uid 10001 with a read-only root file system, no capabilities
  and `no-new-privileges`.
- Inside the container: upload → Phase A → Phase B → agent proposal → apply, with the audit trail written.
- Data and audit rows survive replacing the container; SIGTERM runs the shutdown hooks.
- `DQ_ENV=production` refuses to start without the token and the hash key.
- `docker compose config` and `caddy validate` accept the files.
- Proxy + service through Compose: HTTP redirects to HTTPS, HSTS and the security headers are present, the API needs
  the token, `/metrics` is hidden (404), an oversized body is refused by the proxy (413), an upload works.

**Not verified:** the LibreOffice image layer (the build environment could not reach Debian's package servers), the
Ollama container and `model-pull` helper, public TLS certificate issuance, a GPU, a restore drill, behaviour under
load. See `docs/audit/02_final_assessment.md`.
