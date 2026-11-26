import { del } from "../api";
import { useApp } from "../ctx";
import type { GpuCardData, RuntimeView } from "../types";
import { duration, tierLabel, vram } from "../util";
import { PageHead } from "../components/common";
import GpuGrid from "../components/GpuGrid";
import Console from "../components/Console";

export default function Gpus() {
  const { account, specs, specsError, data, runtimes, selected, setSelected, reloadRuntimes, toast, handle, startRuntime, confirm } = useApp();
  const pending = data?.pending ?? [];
  const current = runtimes.find((r) => r.id === selected) ?? null;
  const pendingAccels = new Set(pending.map((p) => p.accelerator));
  const usage = data?.usage;
  const eligibleCount = specs?.filter((c) => c.eligible).length ?? 0;

  async function release(rt: RuntimeView) {
    const ok = await confirm({
      title: `释放 ${rt.display_name}？`,
      body: rt.busy_task
        ? "它正在执行任务，释放后任务会失败。运行时里的文件、已部署的模型和变量都会丢失，计费随即停止。"
        : "运行时里的文件、已部署的模型和变量都会丢失，计费随即停止。已经下载到本机的结果不受影响。",
      ok: "释放",
      danger: true,
    });
    if (!ok) return;
    try {
      await del(`/api/runtimes/${rt.id}`);
      toast("success", `${rt.display_name} 已释放，计费已停止。`);
      reloadRuntimes();
    } catch (e) {
      handle(e);
    }
  }

  const burn = runtimes.reduce((sum, r) => sum + (r.cu_per_hour ?? 0), 0);
  const unpriced = runtimes.some((r) => r.cu_per_hour == null);
  const lede = runtimes.length
    ? `${runtimes.length} 张卡在计费${burn ? `，每小时约 ${burn.toFixed(2)} CU` : ""}。${data?.auto_release_minutes ? `空闲 ${data.auto_release_minutes} 分钟会自动释放 GPUDock 开的卡。` : "空闲自动释放已关闭，用完记得手动释放。"}`
    : pending.length
      ? `正在向 Colab 申请 ${pending.map((p) => p.label).join("、")}，拿到后会出现在这里。`
      : "现在没有卡在计费。挑一张启动，或者在各功能页选“自动”，提交任务时再申请。";

  return (
    <>
      <PageHead title="显卡" lede={lede} />

      <dl className="stat-strip">
        <div title="正在运行的卡每小时合计的估算单价">
          <dt>正在计费</dt>
          <dd className="lead">
            {burn.toFixed(2)} <small>CU/时</small>
          </dd>
          <span className="stat-note">
            {runtimes.length ? `≈ $${(burn * 0.1).toFixed(2)}/时 · ${runtimes.length} 张卡${unpriced ? " · 部分卡未公开单价" : ""}` : pending.length ? `${pending.length} 张申请中` : "没有运行中的卡"}
          </span>
        </div>
        <div title="按显卡运行时长和社区实测单价估算，只算 GPUDock 开的卡">
          <dt>今天</dt>
          <dd>
            {usage ? usage.today_cu.toFixed(1) : "—"} <small>CU</small>
          </dd>
        </div>
        <div title="按显卡运行时长和社区实测单价估算，只算 GPUDock 开的卡">
          <dt>本月</dt>
          <dd>
            {usage ? usage.month_cu.toFixed(1) : "—"} <small>CU</small>
          </dd>
        </div>
        <div>
          <dt>Colab 订阅</dt>
          <dd>{account ? tierLabel(account.tier) : "—"}</dd>
          <span className="stat-note">{eligibleCount} 种加速器可用</span>
        </div>
      </dl>

      <section className="block">
        <div className="block-head">
          <h2>运行中</h2>
          {runtimes.length > 0 && <span className="subtle small">点一行打开它的控制台</span>}
        </div>
        {runtimes.length === 0 && pending.length === 0 ? (
          <div className="empty-state">
            <div>
              <b>没有运行中的卡</b>
              <p className="subtle small">在下面的价目表里挑一张，通常 1–3 分钟就能用上。</p>
            </div>
          </div>
        ) : (
          <div className="rack">
            {pending.map((j) => (
              <div key={j.id} className="rack-row pending">
                <div className="rack-info">
                  <div className="rack-name">
                    {j.label}
                    <span className="status-pill starting">{j.message.includes("排队") ? "排队中" : "申请中"}</span>
                  </div>
                  <div className="subtle small">
                    {j.message} · 已等待 {duration(Date.now() / 1000 - j.started)}
                  </div>
                  <div className="progress">
                    <span />
                  </div>
                </div>
                <div className="rack-actions">
                  <button className="btn ghost" onClick={() => del(`/api/jobs/${j.id}`).then(reloadRuntimes).catch(handle)}>
                    {j.message.includes("排队") ? "取消排队" : "取消"}
                  </button>
                </div>
              </div>
            ))}
            {runtimes.map((rt) => (
              <div
                key={rt.id}
                className={`rack-row ${rt.id === selected ? "selected" : ""}`}
                role="button"
                tabIndex={0}
                aria-pressed={rt.id === selected}
                onClick={() => setSelected(rt.id)}
                onKeyDown={(e) => {
                  if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) {
                    e.preventDefault();
                    setSelected(rt.id);
                  }
                }}
              >
                <div className="rack-info">
                  <div className="rack-name">
                    {rt.display_name}
                    <span className={`status-pill ${rt.busy ? "busy" : "idle"}`}>{rt.busy_task ? "执行任务中" : rt.busy ? "执行中" : "空闲"}</span>
                    {rt.shape === "SHAPE_HIGHMEM" && <span className="tag">高内存</span>}
                    {!rt.owned && (
                      <span className="tag" title="在 Colab 网页里创建的，GPUDock 不会释放它">
                        Colab 网页
                      </span>
                    )}
                    {rt.auto && <span className="tag" title="提交任务时自动申请的">自动申请</span>}
                  </div>
                  <dl className="rack-meta">
                    <div>
                      <dt>显存</dt>
                      <dd>{vram(rt.vram_gb)}</dd>
                    </div>
                    {rt.uptime_s != null && (
                      <div>
                        <dt>已运行</dt>
                        <dd>{duration(rt.uptime_s)}</dd>
                      </div>
                    )}
                    <div>
                      <dt>单价</dt>
                      <dd>{rt.cu_per_hour != null ? `${rt.cu_per_hour} CU/时` : "未公开"}</dd>
                    </div>
                  </dl>
                </div>
                <Meter rt={rt} />
                <div className="rack-actions">
                  <span className="tag">{rt.id === selected ? "控制台已打开" : "打开控制台"}</span>
                  {rt.owned && (
                    <button
                      className="btn ghost danger"
                      onClick={(e) => {
                        e.stopPropagation();
                        release(rt);
                      }}
                    >
                      释放
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {current && (
        <section className="block">
          <Console key={current.id} runtime={current} onError={handle} onChanged={reloadRuntimes} />
        </section>
      )}

      <section className="block">
        <div className="block-head">
          <h2>价目表</h2>
          <span className="board-note">单价是社区实测估算，1 CU ≈ $0.10，以 Colab 实际计费为准。</span>
        </div>
        {specs ? (
          <GpuGrid cards={specs} onStart={(card: GpuCardData, shape: string) => startRuntime(card.accelerator, shape, card.name)} pendingAccels={pendingAccels} />
        ) : specsError ? (
          <div className="notice error">{specsError}</div>
        ) : (
          <div className="spinner" />
        )}
      </section>
    </>
  );
}

/** What a runtime has cost so far, and how long until it is released for idling. */
function Meter({ rt }: { rt: RuntimeView }) {
  const span = rt.auto_release_in_s != null ? rt.idle_s + rt.auto_release_in_s : null;
  const share = span ? Math.min(1, rt.idle_s / span) : 0;
  const caption = rt.busy
    ? "正在工作，不会自动释放"
    : rt.auto_release_in_s != null
      ? `空闲 ${duration(rt.idle_s)}，${duration(rt.auto_release_in_s)} 后自动释放`
      : rt.owned
        ? "自动释放已关闭"
        : "GPUDock 不会释放这张卡";
  return (
    <div className="meter">
      <div className="meter-top">
        <span>已花费</span>
        <b>
          {rt.est_cu != null ? rt.est_cu.toFixed(2) : "—"} <small>CU</small>
        </b>
      </div>
      <div className="track idle" role="img" aria-label={caption}>
        <span style={{ width: `${Math.round(share * 100)}%` }} />
      </div>
      <span>{caption}</span>
    </div>
  );
}
