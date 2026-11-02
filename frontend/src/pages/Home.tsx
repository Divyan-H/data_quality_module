import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import { Alert, FileDrop, Modal } from "../components/ui";
import { bytes, extension, plural, timeAgo } from "../format";
import { navigate } from "../hooks";
import type { LlmStatus, PhaseKey, RunListItem, ServiceOptions } from "../types";
import { PHASE_KEYS } from "../types";

const FALLBACK_EXT = [".xlsx", ".xlsm", ".xls", ".xlsb", ".ods", ".csv", ".tsv", ".txt", ".parquet"];

export interface Staged {
  file: File;
  path: string;
}

/** Splits dropped files into the ones the service accepts and the ones it will not, with a reason. */
export function checkFiles(items: Staged[], allowed: string[], existing: Staged[], limitMb: number, unreadable: string[] = []) {
  const ok: Staged[] = [];
  const rejected: { name: string; reason: string }[] = [];
  const have = new Set(existing.map((s) => s.path));
  let total = existing.reduce((a, s) => a + s.file.size, 0);
  for (const it of items) {
    const name = it.path.split("/").pop() ?? it.path;
    if (name.startsWith("~$") || name === ".DS_Store" || name.toLowerCase() === "thumbs.db") continue; // Office lock files and OS clutter
    if (!allowed.includes(extension(name))) rejected.push({ name: it.path, reason: `${extension(name) || "no extension"} is not a supported type` });
    else if (unreadable.includes(extension(name))) rejected.push({ name: it.path, reason: `${extension(name)} files need LibreOffice to be converted, and it is not installed on this computer; save the file as .xlsx first` });
    else if (it.file.size === 0) rejected.push({ name: it.path, reason: "the file is empty" });
    else if (have.has(it.path)) rejected.push({ name: it.path, reason: "already added" });
    else if (total + it.file.size > limitMb * 1024 * 1024) rejected.push({ name: it.path, reason: `would pass the ${limitMb} MB limit` });
    else {
      ok.push(it);
      have.add(it.path);
      total += it.file.size;
    }
  }
  return { ok, rejected };
}

export function Home({ llm, options, onLlmRecheck }: { llm: LlmStatus | null; options: ServiceOptions | null; onLlmRecheck: () => void }) {
  const [staged, setStaged] = useState<Staged[]>([]);
  const [rejected, setRejected] = useState<{ name: string; reason: string }[]>([]);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [runs, setRuns] = useState<RunListItem[] | null>(null);
  const [deleting, setDeleting] = useState<RunListItem | null>(null);

  const allowed = options?.limits.extensions ?? FALLBACK_EXT;
  const limit = options?.limits.max_upload_mb ?? 500;
  const total = useMemo(() => staged.reduce((a, s) => a + s.file.size, 0), [staged]);

  const load = () => api.listRuns().then(setRuns).catch(() => setRuns([]));
  useEffect(() => {
    void load();
    const id = window.setInterval(load, 8000);
    return () => window.clearInterval(id);
  }, []);

  function add(items: Staged[]) {
    const r = checkFiles(items, allowed, staged, limit, options && !options.limits.legacy_converter ? options.limits.legacy_extensions : []);
    setStaged([...staged, ...r.ok]);
    setRejected(r.rejected);
  }

  async function begin() {
    setBusy(true);
    setError(null);
    try {
      const run = await api.createRun(staged, name.trim() || suggestName(staged), "on");
      navigate(`/run/${run.id}/A`);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Something went wrong while uploading.");
      setBusy(false);
    }
  }

  return (
    <div className="stack" style={{ gap: 28 }}>
      <div className="page-head">
        <div>
          <h1>Clean a client's data, step by step</h1>
          <p>
            Upload the raw Excel or CSV files. The pipeline fixes their structure (A), their values (B), checks the weekly time series (C) and
            the trends (D). After every phase you download a report, correct it if you wish, upload it back, and move on.
          </p>
        </div>
      </div>

      <div className="card">
        <div className="card-head"><h2>1 · Add files</h2><span className="muted small">{options ? options.limits.extensions.join("  ") : ""}</span></div>
        <div className="card-body stack">
          <FileDrop accept={allowed} multiple directory title="Drop files or a folder here" hint={`Excel, CSV or Parquet · up to ${limit} MB in total · they stay on this computer`} onFiles={add} disabled={busy} />
          {rejected.length > 0 && (
            <Alert kind="warn" title={`${plural(rejected.length, "file")} not added`}>
              <ul>{rejected.slice(0, 8).map((r, i) => <li key={i}><b style={{ display: "inline" }}>{r.name}</b>: {r.reason}</li>)}</ul>
            </Alert>
          )}
          {staged.length > 0 && (
            <>
              <ul className="files" aria-label="Files to upload">
                {staged.map((s) => (
                  <li key={s.path}>
                    <span className="file-icon">{extension(s.path).slice(1, 5).toUpperCase()}</span>
                    <div className="grow"><div>{s.path}</div></div>
                    <span className="muted small">{bytes(s.file.size)}</span>
                    <button className="btn sm ghost" aria-label={`Remove ${s.path}`} onClick={() => setStaged(staged.filter((x) => x !== s))} disabled={busy}>Remove</button>
                  </li>
                ))}
              </ul>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <div className="field" style={{ minWidth: 280 }}>
                  <label htmlFor="rname">Name this run (optional)</label>
                  <input id="rname" type="text" value={name} placeholder={suggestName(staged)} onChange={(e) => setName(e.target.value)} disabled={busy} maxLength={120} />
                </div>
                <div className="row">
                  <span className="muted">{plural(staged.length, "file")} · {bytes(total)}</span>
                  <button className="btn primary lg" onClick={begin} disabled={busy}>{busy ? "Uploading…" : "Start with Phase A"}</button>
                </div>
              </div>
            </>
          )}
          {error && <Alert kind="error">{error}</Alert>}
        </div>
      </div>

      {llm && !llm.available && (
        <Alert kind="warn" title="The local LLM is offline">
          <p>{llm.message} Phases A and B use it to second-check doubtful decisions. You can still run without it; you will be asked at the start of each phase.</p>
          <p style={{ marginTop: 6 }}><button className="btn sm" onClick={onLlmRecheck}>Check again</button></p>
        </Alert>
      )}

      <div className="card">
        <div className="card-head"><h2>Recent runs</h2></div>
        {runs === null ? (
          <p className="empty">Loading…</p>
        ) : runs.length === 0 ? (
          <p className="empty">No runs yet. Add some files above to start one.</p>
        ) : (
          <div>
            {runs.map((r) => (
              <a key={r.id} className="run-card" href={`#/run/${r.id}`}>
                <div className="grow">
                  <b>{r.name}</b>
                  <div className="muted small">{plural(r.files, "file")} · started {timeAgo(r.created_at)}{r.running ? " · running now" : ""}</div>
                </div>
                <div className="pips" aria-label="Phase status">
                  {PHASE_KEYS.map((k: PhaseKey) => <span key={k} className={`pip ${r.phases[k]}`} title={`Phase ${k}: ${r.phases[k]}`}>{k}</span>)}
                </div>
                {r.complete && <span className="chip ok">Complete</span>}
                <button className="btn sm ghost" aria-label={`Delete ${r.name}`} onClick={(e) => { e.preventDefault(); setDeleting(r); }}>Delete</button>
              </a>
            ))}
          </div>
        )}
      </div>

      {deleting && (
        <Modal
          title="Delete this run?"
          onClose={() => setDeleting(null)}
          actions={
            <>
              <button className="btn ghost" onClick={() => setDeleting(null)}>Keep it</button>
              <button className="btn danger" onClick={async () => { await api.deleteRun(deleting.id).catch(() => undefined); setDeleting(null); void load(); }}>Delete run</button>
            </>
          }
        >
          <p><b>{deleting.name}</b> and all its files, reports and audit trail are removed from this computer. This cannot be undone.</p>
        </Modal>
      )}
    </div>
  );
}

function suggestName(staged: Staged[]): string {
  if (staged.length === 0) return "";
  const first = staged[0].path.split("/").pop() ?? "run";
  const stem = first.replace(/\.[^.]+$/, "");
  return staged.length > 1 ? `${stem} + ${staged.length - 1} more` : stem;
}
