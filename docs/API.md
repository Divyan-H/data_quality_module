# API

Base path `/api/v1`. The machine-readable contract is `GET /openapi.json`. Interactive docs pages are switched off
(their scripts come from a CDN, which the Content-Security-Policy forbids).

## Conventions

- **Authentication:** `Authorization: Bearer <token>` or the `dq_session` cookie from `POST /session`.
  `/health`, `/ready` and `/session` need none.
- **Errors:** always `{"error": {"code": "...", "message": "...", ...}}`. `code` is stable; `message` is for people.
- **Request id:** every answer carries `X-Request-ID` (send your own, 8–64 characters of `A-Z a-z 0-9 . _ -`,
  to trace a call). It appears in the logs and in audit rows.
- **Long work:** `start`, `review` and `apply` return at once; poll `GET /runs/{id}` until the phase leaves `running`.
- Answers under `/api/` are `Cache-Control: no-store`.

| Status | Codes you will see |
|---|---|
| 400 | `bad_file`, `no_files`, `bad_inputs`, `bad_pattern`, `pattern_too_slow` |
| 401 | `auth_required`, `bad_token` |
| 404 | `run_not_found`, `unknown_phase`, `no_such_changeset`, `no_such_file` |
| 409 | `busy`, `locked`, `restart_required`, `config_required`, `not_reviewable`, `stale_proposal`, `confirmation_required`, `llm_unavailable` |
| 413 | `too_large` |
| 422 | `bad_request`, `inputs_invalid`, config problems |
| 429 | `busy` (job limit or shutting down), `too_many_attempts` |
| 500 / 503 | `internal_error` (with `request_id`), `launch_failed` |
| 507 | `disk_full` |

## Endpoints

| Method and path | Purpose |
|---|---|
| `GET /health` | Liveness; active job count |
| `GET /ready` | 200 / 503 with checks: workspace, disk, jobs, converter, model |
| `GET /metrics` | Prometheus text (token required; not exposed by the proxy) |
| `GET /audit?run_id=&limit=&before_id=` | Audit events, newest first |
| `GET /audit/llm?run_id=&limit=` | Model calls and a summary |
| `GET /session` · `POST /session` · `DELETE /session` | Signed in? · sign in `{token}` · sign out |
| `GET /options` · `GET /llm/status` | Limits, phases, fix switches · model availability |
| `GET /runs` · `POST /runs` | List · create (multipart: `files`, `paths`, `name`, `llm`) |
| `GET /runs/{id}` · `DELETE /runs/{id}` | State of a run · delete it |
| `GET /runs/{id}/events?after=` | Live progress lines |
| `POST /runs/{id}/cancel` | Stop the running job |
| `GET` / `PUT /runs/{id}/inputs` | Project answers (expected columns, cities, currency) |
| `POST /runs/{id}/phases/{key}/start` | Start a phase `{llm, llm_review, fixes, zero_expected, restart}` |
| `POST /runs/{id}/phases/{key}/accept` · `/skip` | Accept · skip (C, D, T) |
| `POST /runs/{id}/phases/A/selection` | Choose the tables later phases use |
| `GET /runs/{id}/phases/{key}/summary` | Result summary |
| `POST /runs/{id}/phases/{key}/review` | Upload an edited review workbook |
| `GET …/tables` · `GET …/config-template` · `POST …/config` · `POST …/config/recheck` | Client config workbook (C, D) |
| `GET …/columns` | Numeric columns for Phase B's start screen |
| `GET …/agent` | Conversation, change-sets with previews and `review` |
| `POST …/agent/messages` `{text}` | Plan and preview an instruction |
| `POST …/agent/changesets/{cs}/apply` `{confirm}` | Apply; `confirm: true` for an elevated change |
| `POST …/agent/changesets/{cs}/discard` · `POST …/agent/undo` | Drop a proposal · undo the last change |
| `GET …/agent/issues` · `GET …/agent/rows` | Grouped open items · a page of rows with cell states |
| `GET /runs/{id}/results` · `/files/{artifact}` · `/bundle` | File list · one file · everything as a zip |

Outside `/api/v1`, for the platform gateway: `POST /jobs` (Job Contract v1), `POST /jobs/{job_id}/cancel`,
`POST /review/precheck`.

## Example

```bash
curl -H "Authorization: Bearer $DQ_API_TOKEN" -F "files=@spend.xlsx" -F llm=off https://dq.example.com/api/v1/runs
```

```bash
curl -H "Authorization: Bearer $DQ_API_TOKEN" -H "Content-Type: application/json" \
     -d '{"text": "Spend is in thousands"}' https://dq.example.com/api/v1/runs/$RUN/phases/B/agent/messages
```
