import { useEffect, useRef, useState, type DragEvent, type ReactNode } from "react";
import { bytes } from "../format";
import type { Problem, Status } from "../types";

export function Modal({ title, children, actions, onClose }: { title: string; children: ReactNode; actions: ReactNode; onClose?: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    ref.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose?.();
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      prev?.focus?.();
    };
  }, [onClose]);
  return (
    <div className="overlay" role="presentation" onMouseDown={(e) => e.target === e.currentTarget && onClose?.()}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title" tabIndex={-1} ref={ref}>
        <h2 id="modal-title">{title}</h2>
        <div className="stack" style={{ gap: 10, marginTop: 8 }}>{children}</div>
        <div className="actions">{actions}</div>
      </div>
    </div>
  );
}

export function Alert({ kind, title, children }: { kind: "error" | "warn" | "info" | "ok"; title?: string; children?: ReactNode }) {
  return (
    <div className={`alert ${kind}`} role={kind === "error" ? "alert" : "status"}>
      <div>
        {title && <b>{title}</b>}
        {children}
      </div>
    </div>
  );
}

export function ProblemList({ problems, limit = 12 }: { problems: Problem[]; limit?: number }) {
  const [all, setAll] = useState(false);
  const shown = all ? problems : problems.slice(0, limit);
  return (
    <ul>
      {shown.map((p, i) => (
        <li key={i}>
          {(p.sheet || p.cell) && (
            <code>
              {[p.sheet, p.cell].filter(Boolean).join(" ")}
              {": "}
            </code>
          )}
          {p.message}
        </li>
      ))}
      {problems.length > limit && !all && (
        <li>
          <button className="btn sm ghost" onClick={() => setAll(true)}>
            Show {problems.length - limit} more
          </button>
        </li>
      )}
    </ul>
  );
}

const STATUS_LABEL: Record<Status, [string, string]> = {
  locked: ["Locked", ""],
  ready: ["Ready", "info"],
  running: ["Running", "info"],
  review: ["Needs your review", "warn"],
  accepted: ["Accepted", "ok"],
  failed: ["Failed", "bad"],
  cancelled: ["Cancelled", ""],
};

export function StatusChip({ status }: { status: Status }) {
  const [label, tone] = STATUS_LABEL[status];
  return (
    <span className={`chip ${tone}`}>
      <span className="dot" />
      {label}
    </span>
  );
}

export function Stat({ value, label, tone }: { value: ReactNode; label: string; tone?: "ok" | "warn" | "bad" }) {
  return (
    <div className={`stat ${tone ?? ""}`}>
      <div className="v">{value}</div>
      <div className="l">{label}</div>
    </div>
  );
}

export function Spinner() {
  return <span className="tl-mark" style={{ borderColor: "var(--accent)", borderTopColor: "transparent", animation: "spin .9s linear infinite" }} aria-hidden />;
}

/** A drop target + file picker for one or more files. `accept` is a list of extensions (".xlsx"). */
export function FileDrop({
  accept,
  multiple = false,
  directory = false,
  onFiles,
  title,
  hint,
  compact = false,
  disabled = false,
}: {
  accept: string[];
  multiple?: boolean;
  directory?: boolean;
  onFiles: (files: { file: File; path: string }[]) => void;
  title: string;
  hint?: string;
  compact?: boolean;
  disabled?: boolean;
}) {
  const input = useRef<HTMLInputElement>(null);
  const dirInput = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);

  useEffect(() => {
    dirInput.current?.setAttribute("webkitdirectory", "");
  }, []);

  const emit = (list: { file: File; path: string }[]) => list.length && onFiles(multiple ? list : list.slice(0, 1));

  const fromInput = (files: FileList | null) =>
    emit(Array.from(files ?? []).map((f) => ({ file: f, path: (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name })));

  async function fromDrop(e: DragEvent) {
    e.preventDefault();
    setOver(false);
    if (disabled) return;
    const items = Array.from(e.dataTransfer.items ?? []);
    const entries = items.map((i) => i.webkitGetAsEntry?.()).filter(Boolean) as FileSystemEntry[];
    if (entries.some((en) => en.isDirectory)) {
      const out: { file: File; path: string }[] = [];
      const walk = async (en: FileSystemEntry, prefix: string): Promise<void> => {
        if (en.isFile) {
          const file = await new Promise<File>((res, rej) => (en as FileSystemFileEntry).file(res, rej));
          out.push({ file, path: prefix + file.name });
        } else {
          const reader = (en as FileSystemDirectoryEntry).createReader();
          for (;;) {
            const batch = await new Promise<FileSystemEntry[]>((res, rej) => reader.readEntries(res, rej));
            if (!batch.length) break;
            for (const child of batch) await walk(child, `${prefix}${en.name}/`);
          }
        }
      };
      for (const en of entries) await walk(en, "");
      emit(out);
    } else {
      emit(Array.from(e.dataTransfer.files).map((f) => ({ file: f, path: f.name })));
    }
  }

  return (
    <div
      className={`dropzone ${over ? "over" : ""} ${compact ? "compact" : ""}`}
      onDragOver={(e) => {
        e.preventDefault();
        if (!disabled) setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={fromDrop}
      onClick={() => !disabled && input.current?.click()}
      role="button"
      tabIndex={0}
      aria-disabled={disabled}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && !disabled && input.current?.click()}
    >
      <h3>{title}</h3>
      <p className="muted">{hint ?? "Drag and drop here, or click to browse"}</p>
      <input ref={input} type="file" hidden multiple={multiple} accept={accept.join(",")} onChange={(e) => { fromInput(e.target.files); e.target.value = ""; }} />
      {directory && (
        <>
          <input ref={dirInput} type="file" hidden multiple onChange={(e) => { fromInput(e.target.files); e.target.value = ""; }} />
          <p style={{ marginTop: 10 }}>
            <button type="button" className="btn sm" onClick={(e) => { e.stopPropagation(); dirInput.current?.click(); }} disabled={disabled}>
              Choose a folder…
            </button>
          </p>
        </>
      )}
    </div>
  );
}

export function DownloadLink({ href, children, primary = false }: { href: string; children: ReactNode; primary?: boolean }) {
  return (
    <a className={`btn ${primary ? "primary" : ""}`} href={href} download>
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
        <path d="M8 2v8m0 0L5 7m3 3l3-3M3 13h10" />
      </svg>
      {children}
    </a>
  );
}

export function FileSize({ n }: { n: number }) {
  return <span className="muted small">{bytes(n)}</span>;
}
