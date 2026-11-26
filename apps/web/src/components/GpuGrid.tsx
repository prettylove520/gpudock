import { useState } from "react";
import type { GpuCardData } from "../types";
import { hueStyle } from "../hues";
import { HwImage } from "./Logo";

const GROUPS: { kind: GpuCardData["kind"]; title: string; hint: string }[] = [
  { kind: "gpu", title: "GPU", hint: "绝大多数 AI 模型都跑在这里" },
  { kind: "tpu", title: "TPU", hint: "Google 自研芯片，需要 JAX / XLA" },
  { kind: "cpu", title: "CPU", hint: "不需要显卡的准备工作" },
];

export default function GpuGrid({
  cards,
  onStart,
  pendingAccels,
}: {
  cards: GpuCardData[];
  onStart: (card: GpuCardData, shape: string) => void;
  pendingAccels: Set<string>;
}) {
  return (
    <div className="hw-groups">
      {GROUPS.map((g) => {
        const items = cards.filter((c) => c.kind === g.kind);
        if (!items.length) return null;
        return (
          <div key={g.kind} className="hw-group">
            <div className="group-head">
              <span className="group-title">{g.title}</span>
              <span className="rule" />
              <span className="subtle small">{g.hint}</span>
            </div>
            <div className={`hw-grid ${g.kind === "gpu" ? "" : "compact"}`}>
              {items.map((c) => (
                <HwTile key={c.accelerator} card={c} onStart={onStart} pending={pendingAccels.has(c.accelerator)} featured={c.accelerator === "L4"} />
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function HwTile({
  card,
  onStart,
  pending,
  featured,
}: {
  card: GpuCardData;
  onStart: (card: GpuCardData, shape: string) => void;
  pending: boolean;
  featured: boolean;
}) {
  const firstEligible = card.options.find((o) => o.eligible) ?? card.options[0];
  const [shape, setShape] = useState(firstEligible.shape);
  const option = card.options.find((o) => o.shape === shape) ?? firstEligible;
  const memory = card.kind === "cpu" ? "—" : option.vram_gb == null ? "—" : `${option.vram_gb} GB`;

  return (
    <article className={`hw-tile ${card.eligible ? "" : "locked"} ${featured ? "featured" : ""}`} style={hueStyle(card.accelerator)}>
      <div className="hw-media">
        <HwImage accelerator={card.accelerator} />
        {featured && <span className="stamp">推荐</span>}
        {!card.eligible && <span className="stamp muted">订阅不可用</span>}
      </div>
      <div className="hw-body">
        <header className="hw-title">
          <h3>{card.name}</h3>
          {card.arch && card.arch !== "—" && <span className="mono subtle small">{card.arch}</span>}
        </header>
        <p className="subtle small">{card.tagline}</p>
        <dl className="hw-specs">
          <div>
            <dt>{card.kind === "tpu" ? "HBM" : "显存"}</dt>
            <dd className="big-num">{memory}</dd>
          </div>
          <div>
            <dt>单价</dt>
            <dd>{option.cu_per_hour == null ? "暂无数据" : `${option.cu_per_hour} CU/时 · ≈$${(option.cu_per_hour * 0.1).toFixed(2)}`}</dd>
          </div>
          {card.good_for.length > 0 && (
            <div>
              <dt>适合</dt>
              <dd>{card.good_for.join(" · ")}</dd>
            </div>
          )}
        </dl>
        {card.options.length > 1 && (
          <div className="seg">
            {card.options.map((o) => (
              <button
                key={o.shape}
                className={`seg-btn ${o.shape === shape ? "on" : ""}`}
                disabled={!o.eligible}
                title={o.eligible ? o.detail : "当前订阅不可用"}
                onClick={() => setShape(o.shape)}
              >
                {o.label}
              </button>
            ))}
          </div>
        )}
        {card.notes && <p className="note small">{card.notes}</p>}
        <button className={`btn full ${featured ? "primary" : ""}`} disabled={!option.eligible || pending} onClick={() => onStart(card, option.shape)}>
          {!option.eligible ? "当前订阅不可用" : pending ? "申请中…" : `启动 ${card.name.replace("NVIDIA ", "")}`}
          {option.eligible && !pending && <span className="arrow">→</span>}
        </button>
      </div>
    </article>
  );
}
