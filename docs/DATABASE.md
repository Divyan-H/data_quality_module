# Data storage

There is no database server. Run data lives in **run folders**; the audit trail lives in one **SQLite** file.

## Run folders (`DQ_WORKSPACE`, default `data/runs`)

```
<run id>/
  state.json          the run's state machine; written only by the API process (temp file + atomic replace)
  events.jsonl        live progress lines, appended by the worker
  inputs/             uploaded files
  phase_a/  work/     phase outputs: parquet tables, findings and review items as JSON, review workbooks
  config/             client config workbooks, the Phase T recipe
  uploads/            every uploaded review workbook
  agent/<phase>/      session.json, changesets/cs-0001.json, snapshots/cs-0001/ (for undo)
  jobs/<job id>/      request.json, result.json, stderr.log
  final/              exports
```

A run folder is self-contained: copy it and the run moves with it. Integrity rules: one job per run at a time; one
writer of `state.json`; outputs of later phases are discarded when an earlier phase changes.

## Audit database (`DQ_AUDIT_DB`, default `<workspace>/../audit.db`)

SQLite in WAL mode, `busy_timeout` 5 s, written by the API process and the worker processes. Append-only: the
application never updates or deletes a row. Code: `src/storage/audit.py`.

| `audit_events` | |
|---|---|
| `id` | integer primary key |
| `at` | UTC ISO timestamp |
| `actor` | `analyst`, `service`, `gateway`, or a client address for sign-in events |
| `action` | e.g. `run.created`, `phase.started`, `phase.accepted`, `review.uploaded`, `agent.planned`, `agent.applied`, `agent.undone`, `job.finished`, `run.deleted`, `run.purged`, `auth.sign_in_failed` |
| `run_id`, `phase`, `request_id` | nullable |
| `detail` | small JSON object: counts, operation names, file names. Capped at 4,000 characters |

Indexes: `(run_id, id)`, `(at)`.

| `llm_calls` | |
|---|---|
| `purpose` | `agent.plan` or `structure.review` |
| `model`, `digest`, `prompt_version` | which build answered |
| `latency_ms`, `prompt_chars`, `answer_chars`, `attempt` | |
| `outcome` | `ok`, `invalid`, `no_answer`, `repaired` (a CHECK constraint) |

**Never stored:** cell values, file contents, prompts, model answers, access tokens.

### Migrations

`MIGRATIONS` in `src/storage/audit.py` is a list; entry *n* moves the schema from version *n* to *n+1*
(`PRAGMA user_version`). They run on open inside `BEGIN IMMEDIATE`, so two processes starting together migrate once.
A file newer than the build is refused. Never edit a shipped entry; append one.

## Backup and restore

```bash
sqlite3 /data/audit.db ".backup '/backup/audit-$(date +%F).db'"
```

```bash
tar -C /data -czf /backup/runs-$(date +%F).tgz runs recipes
```

Take the run backup while no job is running (`GET /api/v1/health` → `active_jobs: 0`). Restore = stop the service,
put the files back, start it. A restore drill has not been performed; see the final assessment.

## When to move to PostgreSQL

When more than one service instance is needed, or per-user accounts and client separation arrive. The run state and
the job table would move first; the audit schema above carries over unchanged.
