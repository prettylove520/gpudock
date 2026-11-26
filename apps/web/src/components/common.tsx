import { useEffect, useRef, useState } from "react";
import { uploadFiles } from "../api";
import { useApp, type Target } from "../ctx";
import { hueStyle } from "../hues";
import type { RuntimeView } from "../types";
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
    <div className="toggle-row">
      <button type="button" className={`toggle ${checked ? "on" : ""}`} onClick={() => onChange(!checked)} role="switch" aria-checked={checked} aria-label={label}>
        <i />
      </button>
      <span onClick={() => onChange(!checked)}>
        <span className="toggle-label">{label}</span>
        {hint && <span className="field-hint block">{hint}</span>}
      </span>
    </div>
  );
}

/**
 * Where a task runs: one of the running runtimes, or "自动" — reuse a running
 * GPUDock runtime of the suggested kind, else start one when the task is
 * submitted. Optionally release the runtime when the task is done.
 */
export function RuntimePicker({
  value,
  onChange,
  minVram = 0,
  suggest = "T4",
  gpus,
  bigRam = false,
  allowAuto = true,
  releaseByDefault = true,
}: {
  value: Target;
  onChange: (t: Target) => void;
  minVram?: number;
  suggest?: string;
  gpus?: string[];
  bigRam?: boolean;
  allowAuto?: boolean;
  releaseByDefault?: boolean;
}) {
  const { runtimes, data, selected, specs, go } = useApp();
  const pending = data?.pending ?? [];
  const fits = (r: RuntimeView) =>
    (!minVram || r.vram_gb == null || r.vram_gb >= minVram) && (!gpus || gpus.includes(r.accelerator)) && !(bigRam && ["T4", "NONE"].includes(r.accelerator) && r.shape !== "SHAPE_HIGHMEM");
  const card = specs?.find((c) => c.accelerator === suggest);
  const rate = card?.options[0]?.cu_per_hour;

  useEffect(() => {
    if (!data) return; // decide once the runtime list has loaded
    if (value.runtime === "auto" && allowAuto) return;
    if (value.runtime && runtimes.some((r) => r.id === value.runtime)) return;
    const pick = runtimes.find((r) => r.id === selected && fits(r)) ?? runtimes.find(fits);
    if (pick) onChange({ runtime: pick.id, release: false });
    else if (allowAuto) onChange({ runtime: "auto", release: releaseByDefault });
    else if (runtimes[0]) onChange({ runtime: runtimes[0].id, release: false });
    else if (value.runtime) onChange({ runtime: null, release: false });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, runtimes, selected, value.runtime, allowAuto, suggest, minVram]);

  if (!allowAuto && runtimes.length === 0) {
    return (
      <div className="runtime-empty">
        <div>
          <b>{pending.length ? "显卡申请中…" : "还没有运行中的显卡"}</b>
          <p className="subtle small">{pending.length ? "拿到后会自动出现在这里。" : "先在“显卡”页启动一张。"}</p>
        </div>
        {!pending.length && <button className="btn ghost" onClick={() => go("gpus")}>去启动显卡</button>}
        {pending.length > 0 && <div className="spinner" />}
      </div>
    );
  }

  const current = runtimes.find((r) => r.id === value.runtime);
  const canRelease = value.runtime === "auto" || !!current?.owned;
  const pendingSame = pending.find((j) => j.accelerator === suggest);

  return (
    <div className="picker">
      <div className="runtime-pick">
        {allowAuto && (
          <button type="button" className={`rt-chip auto-chip ${value.runtime === "auto" ? "on" : ""}`} style={hueStyle(suggest)} onClick={() => onChange({ runtime: "auto", release: releaseByDefault })}>
            <span className="auto-mark">AUTO</span>
            <b>自动 · {suggest}</b>
            <span className="subtle small">{rate != null ? `约 ${rate} CU/时` : "按需申请"}</span>
          </button>
        )}
        {runtimes.map((r) => (
          <button key={r.id} type="button" className={`rt-chip ${value.runtime === r.id ? "on" : ""}`} style={hueStyle(r.accelerator)} onClick={() => onChange({ runtime: r.id, release: false })}>
            <span className={`pulse-dot ${r.busy ? "busy" : "ok"}`} />
            <b>{r.display_name}</b>
            <span className="subtle small">{vram(r.vram_gb)}{r.busy ? " · 忙碌" : ""}</span>
            {!fits(r) && <span className="tag warn-tag">{bigRam && r.accelerator === "T4" ? "内存不够克隆" : "可能跑不动"}</span>}
          </button>
        ))}
      </div>
      {value.runtime === "auto" && (
        <p className="subtle small">
          {runtimes.some((r) => r.owned && r.accelerator === suggest)
            ? `会直接用正在运行的 ${suggest}。`
            : pendingSame
              ? `${suggest} 正在申请中，拿到后自动开始。`
              : `提交后自动申请一张 ${suggest}（通常 1–3 分钟，没货会排队重试），拿到就开始。`}
        </p>
      )}
      {canRelease && (
        <Toggle
          checked={value.release}
          onChange={(release) => onChange({ ...value, release })}
          label="任务完成后立即释放这张卡"
          hint="最省钱；同一张卡上还有排队的任务时会等它们做完。下次再用要重新部署模型。"
        />
      )}
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
