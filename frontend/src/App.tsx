import { useEffect, useState } from "react";
import { api, AUTH_EVENT, type SessionState } from "./api";
import { LlmChip } from "./components/LlmChip";
import { SignIn } from "./components/SignIn";
import { useLlmStatus, useRoute } from "./hooks";
import { Home } from "./pages/Home";
import { RunPage } from "./pages/RunPage";
import type { ServiceOptions } from "./types";

export function App() {
  const route = useRoute();
  const llm = useLlmStatus();
  const [options, setOptions] = useState<ServiceOptions | null>(null);
  const [session, setSession] = useState<SessionState | null>(null);

  const loadSession = () => api.session().then(setSession).catch(() => setSession({ auth_required: false, signed_in: true }));
  useEffect(() => {
    void loadSession();
    const onAuth = () => setSession({ auth_required: true, signed_in: false });
    window.addEventListener(AUTH_EVENT, onAuth);
    return () => window.removeEventListener(AUTH_EVENT, onAuth);
  }, []);
  const signedIn = session?.signed_in ?? false;
  useEffect(() => {
    if (signedIn) api.options().then(setOptions).catch(() => undefined);
  }, [signedIn]);

  async function signOut() {
    setSession(await api.signOut().catch(() => ({ auth_required: true, signed_in: false })));
  }

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
        {signedIn && <LlmChip status={llm.status} failed={llm.failed} onRecheck={() => void llm.check(true)} />}
        {session?.auth_required && session.signed_in && <button className="btn sm ghost" onClick={() => void signOut()}>Sign out</button>}
      </header>
      <main className="main">
        {session === null ? (
          <p className="muted">Loading…</p>
        ) : !session.signed_in ? (
          <SignIn onSignedIn={() => void loadSession()} />
        ) : route.name === "home" ? (
          <Home llm={llm.status} options={options} onLlmRecheck={() => void llm.check(true)} />
        ) : (
          <RunPage runId={route.runId!} view={route.view} options={options} llmStatus={llm.status} />
        )}
      </main>
    </div>
  );
}
