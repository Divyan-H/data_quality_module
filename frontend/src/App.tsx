import { useEffect, useState } from "react";
import { api } from "./api";
import { LlmChip } from "./components/LlmChip";
import { useLlmStatus, useRoute } from "./hooks";
import { Home } from "./pages/Home";
import { RunPage } from "./pages/RunPage";
import type { ServiceOptions } from "./types";

export function App() {
  const route = useRoute();
  const llm = useLlmStatus();
  const [options, setOptions] = useState<ServiceOptions | null>(null);

  useEffect(() => {
    api.options().then(setOptions).catch(() => undefined);
  }, []);

  return (
    <div className="shell">
      <header className="topbar">
        <a className="brand" href="#/">
          <span className="brand-mark" aria-hidden>
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="#fff" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M2 11l4-5 3 3 5-6" /></svg>
          </span>
          Data Pipeline
        </a>
        <span className="spacer" />
        <LlmChip status={llm.status} failed={llm.failed} onRecheck={() => void llm.check(true)} />
      </header>
      <main className="main">
        {route.name === "home" ? (
          <Home llm={llm.status} options={options} onLlmRecheck={() => void llm.check(true)} />
        ) : (
          <RunPage runId={route.runId!} view={route.view} options={options} llmStatus={llm.status} />
        )}
      </main>
    </div>
  );
}
