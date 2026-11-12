# Changelog

## 1.1.0 — 2026-11-12 · production-hardening audit

### Fixed
- **Security:** Job Contract refs (`inputs[].ref`, `output_prefix`, `cell_edits_ref`) accepted `..` and absolute
  paths; they are now confined to the storage root.
- **Data loss:** restarting a phase while the service was at its job limit deleted the phase's results and then
  refused the restart. The job slot is now reserved first.
- **Denial of service:** a backtracking pattern in a chat instruction could hold a worker; patterns are now length-
  and time-limited.
- Phases C and D passed file descriptions as the phase label: wrong text on the Results page, sentence-long paths
  in the results zip, and no report marked as the one to download.
- `rename column X to Y in <table>` put "in <table>" into the new column name.

### Added
- Security headers (CSP, frame, sniffing, referrer, HSTS), request ids, one error shape, JSON logs.
- Sign-in lockout per client address.
- Audit database (`audit.db`): every accepted action and every local-model call. Endpoints `/api/v1/audit`,
  `/api/v1/audit/llm`. **Schema version 1** (first migration).
- `/api/v1/metrics` (Prometheus text).
- Graceful drain on shutdown (`DQ_SHUTDOWN_GRACE_S`).
- `DQ_ENV`; production refuses to start without a token and a hash key.
- `DQ_LLM_ALLOWED_HOSTS`: prompts go only to this machine or a named host.
- Agent: tool registry, guardrails with explicit confirmation for destructive or large changes, conversation
  memory, one repair attempt, phase limits at planning time, model and prompt version on change-sets.
- Agent evaluation: 110 cases, CLI, CI gate.
- Dockerfile, Compose (Caddy + service + isolated Ollama), `.env.example`, CI with security scans and image smoke test.
- Pinned dependencies with hashes; `pyproject.toml`.
- Documentation set under `docs/`.

### Changed
- **API:** `POST /jobs` validates its body (422 on a malformed one) and needs `job_token` with `callback_url`.
- **API:** applying an elevated change needs `{"confirm": true}` (409 `confirmation_required` otherwise).
- **API:** validation and HTTP errors use `{"error": {...}}` instead of FastAPI's `{"detail": ...}`.
- `/docs` and `/redoc` are off; `/openapi.json` remains.
- Frontend build tools: vite 8, vitest 5 (0 known vulnerabilities).
- Prompt version `agent-plan/3`: the model is shown only the tools of the current phase.

### Upgrade notes
- Set `STRUCTURE_HASH_KEY` and `DQ_API_TOKEN` before using `DQ_ENV=production`.
- Clients of `POST …/apply` must send `confirm` for changes marked `review.level = "elevated"`.

## 1.0.0 — 2026-11-03
Phases A–D, Phase T, Fix-with-agent, recipes, access token, readiness, retention. See git history.
