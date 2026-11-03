import { useState, type FormEvent } from "react";
import { api, ApiError } from "../api";
import { Alert } from "./ui";

/** Shown when the service has an access token set and this browser is not signed in. */
export function SignIn({ onSignedIn }: { onSignedIn: () => void }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.signIn(token.trim());
      setToken("");
      onSignedIn();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not sign in.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card card-pad signin">
      <h2>Sign in</h2>
      <p className="muted small">This Data Pipeline service is protected. Enter the access token your administrator set (DQ_API_TOKEN).</p>
      <form className="stack" onSubmit={submit}>
        <div className="field">
          <label htmlFor="token">Access token</label>
          <input id="token" type="password" autoComplete="current-password" value={token} onChange={(e) => setToken(e.target.value)} autoFocus />
        </div>
        {error && <Alert kind="error">{error}</Alert>}
        <div className="row end">
          <button className="btn primary" type="submit" disabled={busy || !token.trim()}>{busy ? "Signing in…" : "Sign in"}</button>
        </div>
      </form>
    </div>
  );
}
