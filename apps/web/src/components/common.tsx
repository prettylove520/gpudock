import { useEffect, useRef, useState } from "react";
import { uploadFiles } from "../api";
import { useApp } from "../ctx";
import { hueStyle } from "../hues";
import { vram } from "../util";

export function PageHead({ eyebrow, title, accent, lede }: { eyebrow: string; title: string; accent: string; lede: string }) {
  return (
    <section className="page-head rise">
      <span className="kicker">{eyebrow}</span>
      <h1 className="display page-title">
        {title}
        {accent}
      </h1>
      <p className="lede">{lede}</p>
    </section>
  );
}

export function Card({ title, hint, children, delay = 0 }: { title?: string; hint?: string; children: React.ReactNode; delay?: number }) {
  return (
    <section className="panel rise" style={{ animationDelay: `${delay}ms` }}>
      {title && (
        <div className="panel-head">
          <h2>{title}</h2>
          {hint && <span className="subtle small">{hint}</span>}
        </div>
      )}
      {children}
    </section>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="field2">
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}

export function Seg<T extends string>({ value, options, onChange }: { value: T; options: { value: T; label: string; disabled?: boolean }[]; onChange: (v: T) => void }) {
  return (
    <div className="seg wide-seg">
      {options.map((o) => (
        <button key={o.value} type="button" className={`seg-btn ${o.value === value ? "on" : ""}`} disabled={o.disabled} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Toggle({ checked, onChange, label, hint }: { checked: boolean; onChange: (v: boolean) => void; label: string; hint?: string }) {
  return (
    <label className="toggle-row">
      <span className={`toggle ${checked ? "on" : ""}`} onClick={() => onChange(!checked)} role="switch" aria-checked={checked}>
        <i />
      </span>
      <span>
        <span className="toggle-label">{label}</span>
        {hint && <span className="field-hint block">{hint}</span>}
      </span>
    </label>
  );
}

/** Choose which running runtime a job goes to; offers to start one if none. */
export function RuntimePicker({
  value,
  onChange,
  minVram = 0,
  suggest = "T4",
  bigRam = false,
}: {
  value: string | null;
  onChange: (id: string) => void;
  minVram?: number;
  suggest?: string;
  bigRam?: boolean;
}) {
  const { runtimes, data, selected, startRuntime, go } = useApp();
  const pending = data?.pending ?? [];

  useEffect(() => {
    if (value && runtimes.some((r) => r.id === value)) return;
    const pick = runtimes.find((r) => r.id === selected) ?? runtimes[0];
    if (pick) onChange(pick.id);
  }, [runtimes, selected, value, onChange]);

  if (runtimes.length === 0) {
    return (
      <div className="runtime-empty">
        <div>
          <b>{pending.length ? "显卡申请中…" : "还没有运行中的显卡"}</b>
          <p className="subtle small">
            {pending.length ? "拿到后会自动出现在这里。" : suggest === "T4" ? `这个任务建议至少 ${minVram || 15} GB 显存，T4 就够用。` : `这个任务建议至少 ${minVram || 15} GB 显存，推荐 ${suggest}。`}
          </p>
        </div>
        {!pending.length && (
          <div className="row-gap">
            <button className="btn primary" onClick={() => startRuntime(suggest, "SHAPE_STANDARD", `NVIDIA ${suggest}`)}>
              启动 {suggest} <span className="arrow">→</span>
            </button>
            <button className="btn ghost" onClick={() => go("gpus")}>选择其他显卡</button>
          </div>
        )}
        {pending.length > 0 && <div className="spinner" />}
      </div>
    );
  }

  return (
    <div className="runtime-pick">
      {runtimes.map((r) => {
        const low = minVram > 0 && r.vram_gb != null && r.vram_gb < minVram;
        const smallRam = bigRam && ["T4", "NONE"].includes(r.accelerator) && r.shape !== "SHAPE_HIGHMEM";
        return (
          <button key={r.id} type="button" className={`rt-chip ${value === r.id ? "on" : ""}`} style={hueStyle(r.accelerator)} onClick={() => onChange(r.id)}>
            <span className={`pulse-dot ${r.busy ? "busy" : "ok"}`} />
            <b>{r.display_name}</b>
            <span className="subtle small">{vram(r.vram_gb)}{r.busy ? " · 忙碌" : ""}</span>
            {low && <span className="tag warn-tag">显存可能不够</span>}
            {smallRam && <span className="tag warn-tag">内存不够克隆</span>}
          </button>
        );
      })}
    </div>
  );
}

export interface PickedFile {
  key: string;
  name: string;
  size: number;
  progress: number;
  status: "uploading" | "done" | "error";
  id?: string;
  error?: string;
}

export function FileDrop({
  files,
  onChange,
  accept,
  multiple = true,
  label,
}: {
  files: PickedFile[];
  onChange: (f: PickedFile[] | ((prev: PickedFile[]) => PickedFile[])) => void;
  accept: string;
  multiple?: boolean;
  label: string;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);

  function add(list: FileList | null) {
    if (!list || !list.length) return;
    const picked = Array.from(list).slice(0, multiple ? 50 : 1);
    const entries: PickedFile[] = picked.map((f) => ({ key: `${f.name}-${f.size}-${Math.random()}`, name: f.name, size: f.size, progress: 0, status: "uploading" }));
    onChange((prev) => (multiple ? [...prev, ...entries] : entries));
    picked.forEach((file, i) => {
      const key = entries[i].key;
      uploadFiles([file], (p) => onChange((prev) => prev.map((x) => (x.key === key ? { ...x, progress: p } : x))))
        .then(([info]) => onChange((prev) => prev.map((x) => (x.key === key ? { ...x, status: "done", progress: 1, id: info.id } : x))))
        .catch((e) => onChange((prev) => prev.map((x) => (x.key === key ? { ...x, status: "error", error: String(e.message ?? e) } : x))));
    });
  }

  return (
    <div className="drop-wrap">
      <div
        className={`dropzone ${over ? "over" : ""}`}
        onClick={() => input.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          add(e.dataTransfer.files);
        }}
      >
        <div className="drop-icon" aria-hidden="true"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12 16V4M6 10l6-6 6 6M4 20h16" /></svg></div>
        <div>
          <b>{label}</b>
          <p className="subtle small">拖到这里，或点击选择{multiple ? "（可多选）" : ""}</p>
        </div>
        <input ref={input} type="file" accept={accept} multiple={multiple} hidden onChange={(e) => add(e.target.files)} />
      </div>
      {files.length > 0 && (
        <ul className="file-list">
          {files.map((f) => (
            <li key={f.key}>
              <span className="file-name">{f.name}</span>
              <span className="subtle small">{fmtSize(f.size)}</span>
              {f.status === "uploading" && (
                <span className="mini-bar"><span style={{ width: `${Math.round(f.progress * 100)}%` }} /></span>
              )}
              {f.status === "done" && <span className="ok-text small">已就绪</span>}
              {f.status === "error" && <span className="err-text small" title={f.error}>上传失败</span>}
              <button className="x-btn" onClick={() => onChange((prev) => prev.filter((x) => x.key !== f.key))}>×</button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function fmtSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}
