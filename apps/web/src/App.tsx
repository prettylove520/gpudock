import { useCallback, useState } from "react";
import { api, ApiError, token } from "./api";
import type { AuthStatus } from "./types";
import { usePoll } from "./util";
import Login from "./components/Login";
import Shell from "./components/Shell";
import { Logo } from "./components/Logo";

export default function App() {
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setAuth(await api<AuthStatus>("/api/auth/status"));
      setFatal(null);
    } catch (e) {
      setFatal(e instanceof ApiError ? e.message : String(e));
    }
  }, []);

  const loggingIn = auth?.login.status === "running";
  usePoll(refresh, loggingIn ? 1500 : 15000, !!token);

  if (!token) {
    return (
      <Centered>
        <h1 className="display small-display">需要从 GPUDock 打开</h1>
        <p className="subtle">在终端运行 <code>gpudock</code>，它会自动打开带访问令牌的页面。</p>
      </Centered>
    );
  }
  if (fatal && !auth) {
    return (
      <Centered>
        <h1 className="display small-display">连不上 GPUDock</h1>
        <p className="subtle">{fatal}</p>
      </Centered>
    );
  }
  if (!auth) return <Centered><div className="spinner" /></Centered>;
  if (!auth.logged_in) return <Login auth={auth} onChange={refresh} />;
  return <Shell onLoggedOut={refresh} />;
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div className="centered">
      <div className="centered-box rise">
        <Logo size={40} />
        {children}
      </div>
    </div>
  );
}
