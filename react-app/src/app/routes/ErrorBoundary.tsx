import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  fallback?: (error: Error, reset: () => void) => ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("[ProPaths SPA] uncaught render error", error, info);
  }

  reset = () => this.setState({ error: null });

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    if (this.props.fallback) return this.props.fallback(error, this.reset);
    return (
      <div style={{ padding: 32, fontFamily: "system-ui, sans-serif", color: "#dc2626" }}>
        <h1 style={{ fontSize: 18 }}>Something broke.</h1>
        <pre style={{ whiteSpace: "pre-wrap", fontSize: 12 }}>{error.message}</pre>
        <button type="button" onClick={this.reset} style={{ marginTop: 12 }}>
          Retry
        </button>
      </div>
    );
  }
}
