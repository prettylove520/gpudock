import { useState } from "react";
import type { GpuCardData } from "../types";

/** The price board: every accelerator in one table, memory drawn to scale. */
export default function GpuGrid({
  cards,
  onStart,
  pendingAccels,
}: {
  cards: GpuCardData[];
  onStart: (card: GpuCardData, shape: string) => void;
  pendingAccels: Set<string>;
}) {
  const maxVram = Math.max(1, ...cards.flatMap((c) => c.options.map((o) => o.vram_gb ?? 0)));
  return (
    <div className="hw-groups">
      <table className="board">
        <thead>
          <tr>
            <th scope="col">加速器</th>
            <th scope="col" className="num">显存</th>
            <th scope="col" className="num">单价</th>
            <th scope="col">能跑什么</th>
            <th scope="col"><span className="sr-only">启动</span></th>
          </tr>
        </thead>
        <tbody>
          {cards.map((c) => (
            <Row key={c.accelerator} card={c} maxVram={maxVram} onStart={onStart} pending={pendingAccels.has(c.accelerator)} featured={c.accelerator === "L4"} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function cannotText(card: GpuCardData): string {
  // a T4 can run none of the generation models: say so in two words, not eight names
  if (card.kind === "gpu" && card.can_run.length <= 3 && card.cannot_run.length > 3) return "生图、生视频";
  const head = card.cannot_run.slice(0, 3).join("、");
  return card.cannot_run.length > 3 ? `${head} 等 ${card.cannot_run.length} 个` : head;
}

function Row({
  card,
  maxVram,
  onStart,
  pending,
  featured,
}: {
  card: GpuCardData;
  maxVram: number;
  onStart: (card: GpuCardData, shape: string) => void;
  pending: boolean;
  featured: boolean;
}) {
  const firstEligible = card.options.find((o) => o.eligible) ?? card.options[0];
  const [shape, setShape] = useState(firstEligible.shape);
  const option = card.options.find((o) => o.shape === shape) ?? firstEligible;
  const gb = card.kind === "cpu" ? null : option.vram_gb;
  const short = card.name.replace("NVIDIA ", "");

  return (
    <tr className={card.eligible ? "" : "locked"}>
      <td>
        <div className="hw-name">
          {card.name}
          {featured && card.eligible && <span className="tag on">推荐</span>}
          {card.kind === "tpu" && <span className="tag">TPU</span>}
        </div>
        <div className="hw-sub">{card.eligible ? card.tagline : "当前订阅不可用"}</div>
      </td>
      <td className="num cell-vram">
        <div className="vram">
          <div className="track" role="img" aria-label={gb == null ? "无显存" : `${gb} GB，最大 ${maxVram} GB`}>
            <span style={{ width: `${gb == null ? 0 : Math.max(2, (gb / maxVram) * 100)}%` }} />
          </div>
          <b>{gb == null ? "—" : gb}{gb != null && <small> GB</small>}</b>
        </div>
      </td>
      <td className="num rate cell-rate">
        {option.cu_per_hour == null ? (
          <span>未公开</span>
        ) : (
          <>
            <b>{option.cu_per_hour.toFixed(2)} CU/时</b>
            <span>≈ ${(option.cu_per_hour * 0.1).toFixed(2)}/时</span>
          </>
        )}
      </td>
      <td className="runs">
        {card.can_run.length > 0 ? <span className="can-run">{card.can_run.join("、")}</span> : <span className="cannot-run">{card.notes || "—"}</span>}
        {card.can_run.length > 0 && card.cannot_run.length > 0 && <span className="cannot-run" title={card.cannot_run.join("、")}>跑不了 {cannotText(card)}</span>}
      </td>
      <td className="cell-start">
        <div className="start-cell">
          {card.options.length > 1 && (
            <div className="seg" role="group" aria-label={`${short} 规格`}>
              {card.options.map((o) => (
                <button
                  key={o.shape}
                  className={`seg-btn ${o.shape === shape ? "on" : ""}`}
                  disabled={!o.eligible}
                  aria-pressed={o.shape === shape}
                  title={o.eligible ? o.detail : "当前订阅不可用"}
                  onClick={() => setShape(o.shape)}
                >
                  {o.label}
                </button>
              ))}
            </div>
          )}
          <button
            className={`btn ${featured && option.eligible ? "primary" : ""}`}
            disabled={!option.eligible || pending}
            aria-label={`启动 ${short}`}
            onClick={() => onStart(card, option.shape)}
          >
            {!option.eligible ? "不可用" : pending ? "申请中…" : "启动"}
          </button>
        </div>
      </td>
    </tr>
  );
}
