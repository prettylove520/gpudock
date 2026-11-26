import { useState } from "react";
import { post, ApiError } from "../api";
import type { AuthStatus, LoginState } from "../types";
import { Logo } from "./Logo";

const SPECS: [string, string][] = [
  ["运行位置", "你的电脑 · 127.0.0.1"],
  ["算力来源", "你自己的 Colab 账号"],
  ["登录凭据", "系统钥匙串，不出本机"],
  ["接入方式", "Google 官方 Colab API"],
];

// The same figures as the price board in the app (server/catalog.py).
const BOARD = [
  { name: "NVIDIA T4", vram: 15, rate: 1.19 },
  { name: "NVIDIA L4", vram: 22.5, rate: 1.71 },
  { name: "NVIDIA A100", vram: 40, rate: 5.4 },
  { name: "A100 80GB", vram: 80, rate: 7.52 },
  { name: "RTX PRO 6000", vram: 96, rate: 8.71 },
];

export default function Login({ auth, onChange }: { auth: AuthStatus; onChange: () => void }) {
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const login = auth.login;
  const waiting = login.status === "running";

  async function start() {
    setStarting(true);
    setError(null);
    try {
      await post<LoginState>("/api/auth/login");
      onChange();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setStarting(false);
    }
  }

  return (
    <div className="landing">
      <header className="landing-nav">
        <Logo withText />
        <span className="subtle small">开源 · Apache-2.0</span>
      </header>

      <main className="landing-main">
        <section className="landing-copy rise">
          <h1 className="display">
            把 Colab 的显卡
            <br />
            装进你的桌面。
          </h1>
          <p className="lede">
            用自己的 Google 账号登录，一键启动 T4、L4、A100、H100。在本地界面里部署开源模型，批量转写、配音、朗读，不用再来回复制 notebook。
          </p>

          {auth.client_ready ? (
            <div className="cta-row">
              <button className="btn primary big" onClick={start} disabled={starting || waiting}>
                <GoogleMark />
                {waiting ? "等待浏览器授权…" : "用 Google 登录"}
                {waiting ? <span className="spinner tiny light" /> : <span className="arrow">→</span>}
              </button>
              {waiting && login.url && (
                <a className="link small" href={login.url} target="_blank" rel="noreferrer">
                  浏览器没打开？点这里
                </a>
              )}
            </div>
          ) : (
            <div className="notice warn">
              <b>这个版本没有内置登录配置</b>
              <pre className="pre-wrap">{auth.client_error}</pre>
            </div>
          )}

          {auth.client_ready && (
            <p className="callout">
              <span className="callout-tag">注意</span>
              授权页上请点 <b>全选</b>，一定要勾上 <b>Colab</b>，否则 GPUDock 管理不了你的运行时。
            </p>
          )}
          {(error || login.status === "error") && <div className="notice error">{error ?? login.error}</div>}

          <dl className="spec-list">
            {SPECS.map(([k, v]) => (
              <div key={k}>
                <dt>{k}</dt>
                <dd>{v}</dd>
              </div>
            ))}
          </dl>
        </section>

        <figure className="landing-visual">
          <div className="plate ui-preview" data-ui>
            <div className="ui-head">
              <b>价目表</b>
              <span>1 CU ≈ $0.10</span>
            </div>
            <table className="board">
              <tbody>
                {BOARD.map((r) => (
                  <tr key={r.name}>
                    <td><div className="hw-name">{r.name}</div></td>
                    <td className="num">
                      <div className="vram">
                        <div className="track"><span style={{ width: `${(r.vram / 96) * 100}%` }} /></div>
                        <b>{r.vram}<small> GB</small></b>
                      </div>
                    </td>
                    <td className="num rate"><b>{r.rate.toFixed(2)} CU/时</b></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <figcaption className="small subtle">登录后，这些卡按你的 Colab 订阅开放，单价按小时计。</figcaption>
        </figure>
      </main>
    </div>
  );
}

/** Kept for compatibility with pages that rendered the old animated background. */
export function Backdrop() {
  return null;
}

function GoogleMark() {
  return (
    <svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true">
      <path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3C33.7 32.7 29.2 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.4-.4-3.5z" />
      <path fill="#FF3D00" d="m6.3 14.7 6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.8 1.2 7.9 3.1l5.7-5.7C34 6.1 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z" />
      <path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.2 0-9.6-3.3-11.3-8l-6.5 5C9.5 39.6 16.2 44 24 44z" />
      <path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.2-2.2 4.2-4.1 5.6l6.2 5.2C37 39.2 44 34 44 24c0-1.3-.1-2.4-.4-3.5z" />
    </svg>
  );
}
