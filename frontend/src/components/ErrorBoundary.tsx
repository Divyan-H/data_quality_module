import { Component, type ReactNode } from "react";

/** A render error shows this instead of a blank page; the run itself is untouched on the server. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="main">
        <div className="alert error" role="alert">
          <div>
            <b>This screen hit an unexpected problem.</b>
            <p>Your run and its files are safe on the server. Reloading usually fixes it.</p>
            <p className="small muted" style={{ marginTop: 6 }}><code>{this.state.error.message}</code></p>
            <p style={{ marginTop: 10 }}>
              <button className="btn" onClick={() => window.location.reload()}>Reload</button>{" "}
              <a className="btn ghost" href="#/">All runs</a>
            </p>
          </div>
        </div>
      </div>
    );
  }
}
