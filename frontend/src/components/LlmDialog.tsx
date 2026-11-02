import type { LlmStatus } from "../types";
import { Modal } from "./ui";

/**
 * Shown when a phase should use the local LLM but cannot. The analyst decides: fix Ollama and retry,
 * continue with the rule-based fallback, or stop.
 */
export function LlmDialog({
  status,
  phaseTitle,
  busy,
  onRetry,
  onContinueWithout,
  onCancel,
}: {
  status: LlmStatus;
  phaseTitle: string;
  busy: boolean;
  onRetry: () => void;
  onContinueWithout: () => void;
  onCancel: () => void;
}) {
  return (
    <Modal
      title="The local LLM is not available"
      onClose={onCancel}
      actions={
        <>
          <button className="btn ghost" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button className="btn" onClick={onRetry} disabled={busy}>
            Check again
          </button>
          <button className="btn primary" onClick={onContinueWithout} disabled={busy}>
            Continue without the LLM
          </button>
        </>
      }
    >
      <p>{status.message.replace(/`/g, "")}</p>
      <p className="muted small">
        {phaseTitle} normally asks <b>{status.model}</b> at <code>{status.url}</code> to review uncertain decisions. Without it, the
        fixed rule-based logic decides on its own: it still runs the same checks, but doubtful cases are not second-checked by the model. The
        run records that the fallback was used.
      </p>
      {status.reason === "llm_unreachable" && (
        <p className="small">
          Start it in a terminal with <code>ollama serve</code>, then choose <b>Check again</b>.
        </p>
      )}
      {status.reason === "llm_model_missing" && (
        <p className="small">
          Install the model with <code>ollama pull {status.model}</code>, then choose <b>Check again</b>.
          {status.installed && status.installed.length > 0 && <> Installed now: {status.installed.join(", ")}.</>}
        </p>
      )}
    </Modal>
  );
}
