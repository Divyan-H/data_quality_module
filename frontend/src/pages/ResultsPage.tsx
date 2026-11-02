import { useEffect, useState } from "react";
import { api } from "../api";
import { Alert, DownloadLink } from "../components/ui";
import { bytes, plural } from "../format";
import type { Results, Run } from "../types";

const GROUP_ORDER = ["Final data", "Reports", "Audit trail", "Configs used", "Logs"];
const GROUP_NOTE: Record<string, string> = {
  "Final data": "The cleaned, checked tables. Every row keeps its permanent _row_id.",
  Reports: "The review workbooks of each phase, as you left them.",
  "Audit trail": "Machine-readable record of every finding, fix and decision.",
  "Configs used": "The client config workbooks the checks ran with.",
  Logs: "What ran, in order.",
};

export function ResultsPage({ run }: { run: Run }) {
  const [res, setRes] = useState<Results | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api.results(run.id).then(setRes).catch((e) => setErr(e.message));
  }, [run.id, run.updated_at]);

  if (err) return <Alert kind="error">{err}</Alert>;
  if (!res) return <p className="muted">Preparing the final files…</p>;

  const groups = GROUP_ORDER.map((g) => ({ g, files: res.files.filter((f) => f.group === g) })).filter((x) => x.files.length);
  const total = res.files.reduce((a, f) => a + f.bytes, 0);
  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <div className="crumbs">All four phases accepted</div>
          <h1>Final files</h1>
          <p>{plural(res.files.length, "file")} · {bytes(total)}. Download them one by one, or everything together with a manifest of checksums.</p>
        </div>
        <DownloadLink primary href={api.bundleUrl(run.id)}>Download everything (.zip)</DownloadLink>
      </div>
      {groups.map(({ g, files }) => (
        <div className="card" key={g}>
          <div className="card-head"><div><h2>{g}</h2><p className="muted small">{GROUP_NOTE[g]}</p></div></div>
          <ul className="files" style={{ padding: "0 22px" }}>
            {files.map((f) => (
              <li key={f.id}>
                <span className="file-icon">{f.label.split(".").pop()?.slice(0, 4).toUpperCase()}</span>
                <div className="grow">
                  <b>{f.label}</b> <span className="muted small">· Phase {f.phase} · {bytes(f.bytes)}</span>
                  {f.description && <div className="muted small">{f.description}</div>}
                </div>
                <a className="btn sm" href={api.fileUrl(run.id, f.id)} download>Download</a>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}
