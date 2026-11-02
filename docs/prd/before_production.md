# Things to Take Care of Later: Local Now, Production Later

> **Status:** Living list · **Started:** 26 September 2026 · **Covers:** the
> Data Pipeline service (Spec DP-A v0.2, and phases B–G as their specs are
> written) and the platform specs it depends on (Architecture v2.1,
> Specs 01–03)

For now the Data Pipeline microservice runs **locally**: the service, its
object storage and its LLM on one machine, as both workflow diagrams draw it
("runs locally – data stays on this machine"). Several decisions in the specs
are right only because of that. This file lists every one of them, so none is
forgotten when the service is pushed to production.

**How to use it**

* Each spec that makes a local-only decision points here by id (`L-xx` for
  the Data Pipeline service, `P-xx` for platform items that affect it).
* An entry says what the spec does **now**, what must **change for
  production**, why it was deferred, and when it counts as **done**.
* When a new phase spec (B–G) makes a local-only decision, add an entry in
  its section below and reference it from the spec.
* Before the first production deployment, every entry must be done or
  explicitly accepted as a known risk (the checklist at the end).

---

## Summary

| Id | Topic | Now (local) | For production | Spec |
|---|---|---|---|---|
| L-01 | LLM endpoint and routing | Direct call to Ollama at `localhost:11434`; loopback only | A model server on the private network with a service credential and TLS, or the platform's LLM orchestration | DP-A §10.2, §3.3 |
| L-02 | Model hosting and hardware | The model runs on the local machine; chosen by a local benchmark | Sized GPU hosting, pre-pulled pinned models, concurrency limits; benchmark re-run on it | DP-A §10.9, §10.10 |
| L-03 | Default privacy level `full` | The LLM reads cell values by default, because it is on the same machine | Re-decide the default once the model is on another machine; client agreements must allow it | DP-A §10.3 |
| L-04 | Who may set the privacy level | The Analyst, per run; no tenant cap | Tenant settings `ai.enabled` and `ai.structure_privacy_max`, enforced by the gateway | DP-A §8.2, §12 change 14; Spec 01 |
| L-05 | Where the service runs | One local machine | The private network next to the platform (Hetzner, then AWS), TLS on every hop | DP-A §2.2; Spec 02 §2.1 |
| L-06 | Object storage and credentials | Local storage with development keys | Shared MinIO, then Amazon S3, with per-job STS credentials scoped to prefixes | DP-A §3.3, §6.1; Spec 02 §6.3 |
| L-07 | Connectors and API ingestion | Files only; the seam is kept open | Job Contract change for connector credentials (V1.1) | DP-A §2.5; Spec 03 §1 |
| L-08 | Legacy-format converter | LibreOffice headless as a local process with limits | A locked-down sandbox, patched and monitored | DP-A §3.1, §11.9 |
| L-09 | Review-upload pre-check | Runs inside the gateway process | An isolated worker; the gateway waits on it briefly | DP-A §9.4 |
| L-10 | Example library and decision records | Stored under the project prefix, no retention rules | Retention, purge, isolation checks, a way to remove a wrong correction | DP-A §10.7, §10.8, §12 change 18 |
| L-11 | Calibration data | Value-free facts and labelled regions from pilot files, kept locally | Client consent, retention period, access control | DP-A §10.9, §16.1 |
| L-12 | Performance and budgets | Targets measured on the local machine | Measured again on production workers; timeouts and LLM budget re-set | DP-A §11.7, §11.8, §10.10 |
| L-13 | Logs and monitoring | Local logs, value-free by rule | Central logging with the value-free rule enforced; no prompt logging anywhere; dashboards and alerts | DP-A §2.4 P7, §11.3 |
| L-14 | LLM calls outside Structure | Mapping suggestions (Phase E) not yet specified for the service | One privacy rule for every model that reads client data | DP-A §6.2; Spec 03 §3.3 |
| L-15 | Model licences | Any candidate model may be benchmarked | Licence review of the pinned model for commercial use | DP-A §10.2, §10.9 |
| L-16 | Question-hash key | `STRUCTURE_HASH_KEY` is a local secret | Held in a secrets manager, rotated with a plan for old decision records | DP-A §10.8 |
| P-01 | Hosting move | — | Hetzner first, AWS as the target: configuration only | Spec 02 §2.1 |
| P-02 | Gateway → service credential | Not required on one machine | Per-service signed JWT or mTLS (Spec 02 leaves the choice open) | Spec 02 §6.1 |
| P-03 | Storage durability | Single local store | MinIO distributed mode and backups, then S3 | Spec 02 §2.1 |

---

## 1. Data Pipeline service: Phase A (Structure), Spec DP-A v0.2

### L-01 LLM endpoint and routing

* **Spec:** DP-A §10.2, §3.3 (decision 1).
* **Now:** the pipeline calls a local Ollama server directly at
  `STRUCTURE_LLM_URL` (default `http://localhost:11434`). The job refuses a
  URL that is not a loopback address unless the service is started with
  `STRUCTURE_LLM_ALLOW_REMOTE=1`. There is no authentication on the call.
* **For production:** choose one of:
  1. a model server on the private network, called directly with a service
     credential and TLS, with `STRUCTURE_LLM_ALLOW_REMOTE=1` and an
     allow-list of model hosts; or
  2. the platform's LLM orchestration (`llm_orchestration.md`), which today
     serves browser-side suggestions. It would need pinned models, per-call
     seeds and structured output for jobs.

  Either way, keep the digest check, the decision record and reuse
  (§10.8), so answers stay pinned and replayable.
* **Why deferred:** decision 1, "for now let's call the LLM from local".
* **Risk if forgotten:** with the loopback guard in place, a remote model
  server is refused and every job quietly runs rules-only (`llm_fallback`,
  info). Accuracy drops without an error.
* **Done when:** the endpoint is set per environment, authenticated and
  encrypted, and criterion 31 (canary) and criterion 32 pass against it.

### L-02 Model hosting, hardware and pinning

* **Spec:** DP-A §10.9, §10.10, §11.8.
* **Now:** the model runs on the machine that runs the service. It is chosen
  by the benchmark of §10.9 on that machine, and the latency target (p95
  ≤ 15 s per call) is measured there.
* **For production:**
  * size the model hosting for concurrent jobs: a job makes about
    (reviewed regions × votes) calls, so GPU capacity decides job time;
  * a registry of approved models with their digests, pre-pulled into the
    server image, and a pinned Ollama (or other server) version that
    supports structured output. Locally, pin the Ollama version too: each
    decision records it, and the benchmark's release gate covers it;
  * a queue or concurrency limit per job and per tenant, so one large job
    doesn't starve others;
  * re-run the benchmark on the production hosting before pinning.
* **Risk if forgotten:** CPU-only inference makes jobs slow and spends the
  LLM budget, so later questions fall back to the rules.
* **Done when:** capacity is sized from measured numbers and the benchmark
  passes on the production model server.

### L-03 Default privacy level `full`

* **Spec:** DP-A §10.3, §8.2 (decisions 1 and 2, correction C2).
* **Now:** the default is `full`: the LLM reads the values of the first 30
  and last 5 rows of each region. This is acceptable because the model runs
  on the same machine as the data and keeps nothing.
* **For production:** once the model runs on another machine, the values
  cross a network. Re-decide the default (for example `labels_only` unless
  the model server sits inside the same trust boundary with logging off),
  and make sure client agreements allow a model to read their data.
* **Risk if forgotten:** client values reach a model server that the client
  never agreed to.
* **Done when:** the default is confirmed for the production hosting, and the
  data-processing terms name the model server.

### L-04 Tenant cap on the privacy level

* **Spec:** DP-A §8.2, §3.3, §12 change 14; Spec 01 (tenant settings)
  (decision 2).
* **Now:** the Analyst sets `structure.llm` (mode, privacy, votes, review)
  per run. There is no tenant setting and no cap.
* **For production:** add tenant settings `ai.enabled` and
  `ai.structure_privacy_max` (`structure_only`, `labels_only`, `full`). The
  gateway rejects a run setting above the cap with
  `422 structure_privacy_not_allowed`, and changes of the level are audited
  (who, when). A client can switch AI off entirely.
* **Done when:** Spec 01 carries the settings, the gateway enforces them, and
  an acceptance criterion covers a run above the cap.

### L-05 Where the service runs

* **Spec:** DP-A §2.2, §11 (decision 10); Spec 02 §2.1.
* **Now:** the Data Pipeline service, its object storage and its LLM run on
  one local machine.
* **For production:** deploy the service on the platform's private network
  (Hetzner first, AWS as the target, Spec 02 §2.1), reachable only from the
  gateway. TLS on every hop, private ones included, because job tokens and
  storage credentials cross them. Health checks, restarts, and workers that
  scale out (a job processes up to 4 files in parallel).
* **Done when:** the service passes the Job Contract v1 checks (Spec 02 §6)
  from the gateway over the private network.

### L-06 Object storage and credentials

* **Spec:** DP-A §3.3, §6.1, §12 change 18; Spec 02 §2.1, §6.3.
* **Now:** local object storage with development keys.
* **For production:** the shared MinIO (V1 on Hetzner), then Amazon S3, with
  per-job STS credentials limited to the job's input prefixes and output
  prefix (Spec 02 §6.3). The policy must also cover the new input of kind
  `examples` (the project's example library, `library/structure_examples/`).
  The service never holds long-lived keys to the bucket.
* **Done when:** a job reads only its listed inputs and writes only under its
  `output_prefix`, and a write outside it is refused by storage.

### L-07 Connectors and API ingestion

* **Spec:** DP-A §2.5 (decision 10); Spec 03 §1 (connectors are V1.1);
  Spec 02 §5.5, §6.
* **Now:** V1 takes uploaded files only. The seam is the shape of the
  Structure output, which a connector adapter would write.
* **For production (V1.1):** a Job Contract change to pass connector
  credentials (as secret references, not secrets), an extraction window or
  watermark instead of the file hash for reuse, and the adapter itself.
* **Done when:** Job Contract v1.1 is agreed with the platform.

### L-08 Converter for XLS, XLSB and ODS

* **Spec:** DP-A §3.1, §11.7, §11.9 (decision 3).
* **Now:** LibreOffice in headless mode runs as a separate local process,
  with no network, macros disabled, a fresh profile per file, 5 minutes and
  4 GB per conversion.
* **For production:** run it in a locked-down sandbox (its own container,
  read-only file system except scratch, no network namespace, system-call
  filtering), keep the pinned version patched, give it its own worker pool
  and limits, and monitor crashes and timeouts. Legacy formats are a known
  attack surface for untrusted uploads.
* **Done when:** a malformed legacy file can crash only its own conversion,
  and security updates of the converter have an owner.

### L-09 Review-upload pre-check in the gateway

* **Spec:** DP-A §9.4, §12 change 17 (decision 13).
* **Now:** at `complete`, the gateway opens the uploaded review workbook in
  its own process, with a 10-second cap.
* **For production:** move the parsing to an isolated worker (a sandboxed
  task queue, or a validate-only endpoint of the Data Pipeline service),
  with memory and time limits. The gateway waits on it briefly and still
  answers `422` synchronously.
* **Risk if forgotten:** a crafted workbook can exhaust the gateway's memory
  or CPU.
* **Done when:** no uploaded file is parsed inside the gateway process.

### L-10 Example library and decision records

* **Spec:** DP-A §10.7, §10.8, §12 change 18.
* **Now:** the project's example library lives under
  `{tenant}/{project}/library/structure_examples/`, and each job keeps its
  `llm/decisions.jsonl`. Both are value-free (class grids, structural text,
  options and answers). There are no retention rules.
* **For production:** retention and purge (project deletion and client
  offboarding remove the library), a check that a job only ever receives its
  own project's library, a size cap (500 corrections per project is the
  spec's), and a way for an Analyst to remove a wrong correction. Built-in
  examples in the rule pack must never be learned from client data without
  consent.
* **Done when:** retention is defined and tested, and removal works.

### L-11 Calibration data from pilot runs

* **Spec:** DP-A §10.9, §16.1 (decision 11).
* **Now:** value-free facts and labelled regions from pilot files are
  collected on the local machine to tune the rule pack and build the LLM
  benchmark suite. The pilot files themselves stay on the benchmark machine,
  because a `full` benchmark reads their values (DP-A §10.9).
* **For production:** client consent in the contract (for the labels and for
  keeping the pilot files), a retention period for both, a named store with
  access control, and removal on request. Project corrections are never used
  to benchmark for other projects.
* **Done when:** the consent wording and retention are agreed.

### L-12 Performance targets and budgets

* **Spec:** DP-A §11.7, §11.8, §10.10 (decision 10).
* **Now:** the reference worker (4 vCPU, 8 GB) is nominal; the numbers are
  measured on the local machine. The LLM budget is 300 calls per job with a
  75-minute cap on LLM time, and the job timeout is 2 hours.
* **For production:** measure again on production workers and the production
  model server, add p99 and memory, and set the job timeout, the LLM budget
  and the per-job parallelism from the results.
* **Done when:** the CI benchmark runs on production-like hardware.

### L-13 Logs and monitoring

* **Spec:** DP-A §2.4 P7, §11.3, §4.11 I-07.
* **Now:** logs are local files. They are value-free by rule, prompts are
  never logged, and Ollama's request logging is off.
* **For production:** central log shipping with the value-free rule enforced
  (the canary test of criterion 31 runs against the production logging
  path); no prompt capture in the model server, proxies or APM tools;
  dashboards for the LLM review (agreement, change and overrule rates,
  fallbacks) and alerts on `llm_fallback` spikes.
* **Done when:** a canary run in the production environment leaves no token
  in any log or trace.

### L-14 LLM calls outside Structure

* **Spec:** DP-A §6.2, §12 change 9; Spec 03 §3.3.
* **Now:** Structure's model reads values at the Analyst's level, on the
  local machine. The mapping step (Phase E) will read `samples.json`, and
  Spec 03 §3.3 has mapping suggestions go through `llm_orchestration.md`
  with sample values. The proposal is that the Analyst's level governs those
  samples too.
* **For production:** one rule for every model that reads client data:
  where it runs, who sets the level, the tenant cap (L-04). Settle it in the
  Phase E spec.
* **Done when:** the Phase E spec states it and shares the cap.

### L-15 Model licences

* **Spec:** DP-A §10.2, §10.9.
* **Now:** any candidate model can be benchmarked locally.
* **For production:** review the licence of the pinned model for commercial
  use in a hosted service (the candidates come under different licences),
  and record it with the pin.
* **Done when:** the pinned model's licence is approved.

### L-16 Key for question hashes

* **Spec:** DP-A §10.8.
* **Now:** the question hash of each LLM decision is an HMAC keyed per
  project from the service secret `STRUCTURE_HASH_KEY`, set on the local
  machine. It keeps value-derived hashes from being tested against guesses.
* **For production:** hold the secret in a secrets manager, never in the
  image or the repository, and decide how to rotate it. A new key makes old
  decision records unmatchable, so rotation means the next runs ask the model
  again (or records are re-keyed).
* **Done when:** the secret has an owner, a store and a rotation procedure.

---

## 2. Platform specs that affect the microservice

### P-01 Hosting: Hetzner first, AWS as the target

* **Spec:** Spec 02 §2.1, §12.
* **Now:** local.
* **For production:** the platform starts on Hetzner with MinIO and moves to
  AWS with S3. Spec 02 is written against the S3 API and STS, so the move is
  configuration only. The service must take the storage endpoint from the
  job (`storage.endpoint_url`) and never hard-code it.

### P-02 Gateway → service credential

* **Spec:** Spec 02 §6.1.
* **Now:** on one machine the service credential is not needed; a static
  development key is enough.
* **For production:** Spec 02 allows a per-service signed JWT
  (`iss=cuvity-gateway`, `aud=<service>`, 5 minutes) or mTLS. Pick one, set up
  key rotation, and reject unauthenticated `POST /jobs`.

### P-03 Storage durability

* **Spec:** Spec 02 §2.1.
* **Now:** a single local store.
* **For production:** MinIO starts single-node with scheduled off-server
  backups and moves to distributed mode when volume or uptime needs it; then
  S3. Staging outputs, review uploads and the example library all live
  there.

---

## 3. Other phases (B–G)

Add entries here as each phase spec is written. Known so far:

* **Phase B (Clean):** reads the selected Structure tables, and the raw file
  for Parquet uploads (DP-A §4.3). No local-only decision recorded yet.
* **Phase C (Time-series checks), Phase D (Trends):** none yet.
* **Gate after Phase D (client report):** none yet.
* **Phase E (Mapping):** L-14.
* **Phases G and F (database checks and loading):** none yet.

---

## 4. Checklist before the first production deployment

1. L-05, L-06, P-01, P-02: the service runs on the private network with TLS,
   scoped storage credentials and an authenticated `POST /jobs`.
2. L-01, L-02, L-15, L-16: the model server is chosen, hosted, licensed,
   pinned and benchmarked, and the hash key is managed.
3. L-03, L-04, L-14: the privacy default, the tenant cap and the rule for
   every model that reads client data are decided and enforced.
4. L-08, L-09: untrusted files are parsed and converted only in sandboxes.
5. L-10, L-11, L-13: retention, consent and value-free logging are in place.
6. L-12: timeouts and budgets are set from production measurements.
7. L-07: only when connectors (V1.1) are in scope.
