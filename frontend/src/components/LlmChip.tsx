import type { LlmStatus } from "../types";

export function LlmChip({ status, failed, onRecheck }: { status: LlmStatus | null; failed: boolean; onRecheck: () => void }) {
  let tone = "";
  let label = "Checking LLM…";
  let title = "Checking the local LLM";
  if (failed) {
    tone = "bad";
    label = "Service unreachable";
    title = "The browser cannot reach the Data Pipeline service";
  } else if (status) {
    tone = status.available ? "ok" : "warn";
    label = status.available ? `LLM ready · ${status.model}` : "LLM offline";
    title = `${status.message} Click to check again.`;
  }
  return (
    <button className={`chip llm-chip ${tone}`} title={title} onClick={onRecheck} aria-label={`${label}. Click to check again.`}>
      <span className="dot" />
      {label}
    </button>
  );
}
