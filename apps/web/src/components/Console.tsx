import { useEffect, useRef, useState } from "react";
import { api, post } from "../api";
import type { Chunk, ExecView, RuntimeView, Stats } from "../types";
import { hueStyle } from "../hues";
import { duration, rate, usePoll } from "../util";

const SNIPPETS: { label: string; code: string }[] = [
  { label: "查看显卡", code: "!nvidia-smi" },
  {
    label: "测试 PyTorch",
    code: "import torch\nprint('PyTorch', torch.__version__)\nprint('CUDA 可用:', torch.cuda.is_available())\nif torch.cuda.is_available():\n    print('显卡:', torch.cuda.get_device_name(0))\n    x = torch.randn(4096, 4096, device='cuda')\n    print('矩阵乘法 OK:', (x @ x).shape)",
  },
  { label: "磁盘与内存", code: "!df -h /content && free -h" },
  { label: "安装 faster-whisper", code: "!pip install -q faster-whisper && echo 安装完成" },
];

export default function Console({
  runtime,
  onError,
  onChanged,
}: {
  runtime: RuntimeView;
  onError: (e: unknown) => void;
  onChanged: () => void;
}) {
  const storeKey = `gpudock.code.${runtime.id}`;
  const [code, setCode] = useState(() => localStorage.getItem(storeKey) ?? SNIPPETS[0].code);
  const [execId, setExecId] = useState<string | null>(runtime.busy_exec);
  const [exec, setExec] = useState<ExecView | null>(null);
  const [chunks, setChunks] = useState<Chunk[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [now, setNow] = useState(Date.now() / 1000);
  const outRef = useRef<HTMLPreElement>(null);
  const nextRef = useRef(0);

  useEffect(() => localStorage.setItem(storeKey, code), [code, storeKey]);

  const running = exec?.status === "running" || (!!execId && !exec);

  usePoll(
    async () => {
      if (!execId) return;
      try {
        const v = await api<ExecView>(`/api/execs/${execId}?since=${nextRef.current}`);
        nextRef.current = v.next;
        if (v.chunks.length) setChunks((c) => [...c, ...v.chunks]);
        setExec(v);
        setNow(Date.now() / 1000);
        if (v.status !== "running") onChanged();
      } catch (e) {
        onError(e);
      }
    },
    400,
    !!execId && (exec === null || exec.status === "running"),
  );

  usePoll(async () => {
    try {
      setStats(await api<Stats>(`/api/runtimes/${runtime.id}/stats`));
    } catch {
      /* stats are best-effort */
    }
  }, 8000);

  useEffect(() => {
    const el = outRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [chunks]);

  async function run() {
    if (!code.trim() || running) return;
    try {
      nextRef.current = 0;
      setChunks([]);
      setExec(null);
      const v = await post<ExecView>(`/api/runtimes/${runtime.id}/exec`, { code });
      setExecId(v.id);
      onChanged();
    } catch (e) {
      onError(e);
    }
  }

  async function interrupt() {
    try {
      await post(`/api/runtimes/${runtime.id}/interrupt`);
    } catch (e) {
      onError(e);
    }
  }

  const elapsed = exec ? (exec.finished ?? now) - exec.started : null;
  const g = stats?.gpu;

  return (
    <div className="console" style={hueStyle(runtime.accelerator)}>
      <div className="console-head">
        <span className={`led ${running ? "busy" : "on"}`} />
        <div className="console-title">
          <span className="mono">CONSOLE</span> {runtime.display_name}
        </div>
        <span className="subtle small">{rate(runtime.cu_per_hour)}</span>
      </div>

      <div className="meters">
        <div className="meter-card wide">
          <div className="m-label">显卡</div>
          <div className="m-value">{g ? g.name : stats?.gpu_error ? "无 GPU" : "读取中…"}</div>
          {g && <div className="m-sub">{g.temp_c}°C</div>}
        </div>
        <Meter label="GPU 利用率" value={g ? g.util : null} max={100} unit="%" />
        <Meter label="显存" value={g ? g.mem_used_mb / 1024 : null} max={g ? g.mem_total_mb / 1024 : null} unit="GB" />
        <Meter label="内存" value={stats?.ram?.used_gb ?? null} max={stats?.ram?.total_gb ?? null} unit="GB" />
        <Meter label="磁盘" value={stats?.disk?.used_gb ?? null} max={stats?.disk?.total_gb ?? null} unit="GB" />
      </div>

      <div className="snippets">
        <span className="mono subtle small">快捷代码</span>
        {SNIPPETS.map((s) => (
          <button key={s.label} className="snippet" onClick={() => setCode(s.code)} disabled={running}>
            {s.label}
          </button>
        ))}
      </div>

      <div className="editor-wrap">
        <span className="prompt">In</span>
        <textarea
          className="editor"
          value={code}
          spellCheck={false}
          onChange={(e) => setCode(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
              e.preventDefault();
              run();
            }
          }}
          rows={Math.min(14, Math.max(3, code.split("\n").length + 1))}
        />
      </div>

      <div className="run-bar">
        <div className="subtle small run-status">
          {running ? (
            <>
              <span className="pulse-dot busy" /> 正在执行 · {duration(elapsed)}
            </>
          ) : exec ? (
            <>
              <span className={`pulse-dot ${exec.status === "ok" ? "ok" : "err"}`} />
              {exec.status === "ok" ? "完成" : exec.status === "aborted" ? "已中止" : "出错"} · 用时 {duration(elapsed)}
            </>
          ) : (
            <>支持 Python 和 <code>!命令</code></>
          )}
        </div>
        <div className="run-actions">
          {running && (
            <button className="btn ghost" onClick={interrupt}>
              中断
            </button>
          )}
          <button className="btn primary" onClick={run} disabled={running || !code.trim()}>
            运行 <kbd>⌘↵</kbd>
          </button>
        </div>
      </div>

      <pre ref={outRef} className={`output ${chunks.length ? "" : "empty-out"}`}>
        {chunks.length === 0
          ? running
            ? "等待输出…"
            : "输出会显示在这里。"
          : chunks.map((c) => (
              <span key={c.i} className={`out-${c.kind}`}>
                {stripAnsi(c.text)}
                {c.kind === "result" ? "\n" : ""}
              </span>
            ))}
        {exec?.error && exec.status === "error" && (
          <span className="out-error">
            {"\n"}
            {exec.error.traceback.length ? exec.error.traceback.map(stripAnsi).join("\n") : `${exec.error.ename}: ${exec.error.evalue}`}
          </span>
        )}
      </pre>
    </div>
  );
}

function Meter({ label, value, max, unit }: { label: string; value: number | null; max: number | null; unit: string }) {
  const pct = value != null && max ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div className="meter-card">
      <div className="m-label">{label}</div>
      <div className="m-value">
        {value == null ? "—" : unit === "%" ? `${Math.round(value)}%` : (
          <>
            {value.toFixed(1)}
            <span className="m-of"> / {max?.toFixed(0)} {unit}</span>
          </>
        )}
      </div>
      <div className="bar">
        <div className={`bar-fill ${pct > 85 ? "hot" : ""}`} style={{ width: `${Math.max(pct, value != null ? 2 : 0)}%` }} />
      </div>
    </div>
  );
}

// eslint-disable-next-line no-control-regex
const ANSI = /\x1b\[[0-9;]*m/g;
function stripAnsi(s: string): string {
  return s.replace(ANSI, "");
}
