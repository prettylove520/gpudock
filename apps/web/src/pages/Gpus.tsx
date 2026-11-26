import { del } from "../api";
import { useApp, type Route } from "../ctx";
import { hueStyle } from "../hues";
import type { GpuCardData, RuntimeView } from "../types";
import { cu, duration, tierLabel, vram } from "../util";
import { ChipArt, HwImage, Pic } from "../components/Logo";
import { PageHead } from "../components/common";
import GpuGrid from "../components/GpuGrid";
import Console from "../components/Console";

const FEATURES: { route: Route; img: string; title: string; desc: string }[] = [
  { route: "create", img: "feat-video", title: "创作", desc: "Wan2.2、LTX-2.5、MiniMax H3 生视频，FLUX 3、Qwen-Image 生图" },
  { route: "dub", img: "feat-dub", title: "批量配音", desc: "视频翻译成另一种语言并重新配音，保留背景音乐" },
  { route: "transcribe", img: "feat-transcribe", title: "批量转写", desc: "音视频转成带时间轴的字幕和文稿" },
  { route: "tts", img: "feat-tts", title: "批量朗读", desc: "一行文字一个音频，预置音色或克隆" },
];

export default function Gpus() {
  const { account, specs, specsError, data, runtimes, selected, setSelected, reloadRuntimes, toast, handle, startRuntime, go, confirm } = useApp();
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

  return (
    <>
      <PageHead eyebrow="01 / 显卡" title="你的 GPU 工作台" accent="" lede="启动一张 Colab 显卡，然后在这里执行代码、部署模型、跑批量任务。用完记得释放，或者交给“空闲自动释放”。" />

      <dl className="stat-strip rise">
        <div>
          <dt>运行中</dt>
          <dd>{runtimes.length + pending.length}</dd>
        </div>
        <div title="按显卡运行时长和社区实测单价估算，只算 GPUDock 开的卡">
          <dt>今天 / 本月花费</dt>
          <dd>
            {usage ? usage.today_cu.toFixed(1) : "—"}
            <small> / {usage ? cu(usage.month_cu) : "—"}</small>
          </dd>
        </div>
        <div>
          <dt>可用加速器</dt>
          <dd>
            {eligibleCount} <small>种</small>
          </dd>
        </div>
        <div>
          <dt>Colab 订阅</dt>
          <dd>{account ? tierLabel(account.tier) : "—"}</dd>
        </div>
      </dl>

      <section className="feature-row rise" style={{ animationDelay: "40ms" }}>
        {FEATURES.map((f) => (
          <button key={f.route} className="feature" onClick={() => go(f.route)}>
            <Pic src={`/img/${f.img}.webp`} className="feature-pic" fallback={<ChipArt size={40} />} />
            <span className="feature-text">
              <b>{f.title}</b>
              <span className="subtle small">{f.desc}</span>
            </span>
            <span className="arrow">→</span>
          </button>
        ))}
      </section>

      <section className="block rise" style={{ animationDelay: "80ms" }}>
        <div className="block-head">
          <h2>运行中</h2>
          <span className="subtle small">点一行打开它的控制台</span>
        </div>
        {runtimes.length === 0 && pending.length === 0 ? (
          <div className="empty-state">
            <ChipArt size={36} />
            <div>
              <b>还没有运行中的显卡</b>
              <p className="subtle small">在下面挑一张，几秒到几分钟就能用上。</p>
            </div>
          </div>
        ) : (
          <div className="rack">
            {pending.map((j) => (
              <div key={j.id} className="rack-row pending" style={hueStyle(j.accelerator)}>
                <HwImage accelerator={j.accelerator} className="rack-pic" />
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
                style={hueStyle(rt.accelerator)}
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
                <HwImage accelerator={rt.accelerator} className="rack-pic" />
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
                    {rt.est_cu != null && (
                      <div>
                        <dt>估算</dt>
                        <dd>{rt.est_cu} CU</dd>
                      </div>
                    )}
                    {rt.auto_release_in_s != null && (
                      <div>
                        <dt>自动释放</dt>
                        <dd>{duration(rt.auto_release_in_s)} 后</dd>
                      </div>
                    )}
                  </dl>
                </div>
                <div className="rack-actions">
                  <span className={`tag ${rt.id === selected ? "on" : ""}`}>{rt.id === selected ? "控制台已打开" : "打开控制台"}</span>
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
        <section className="block rise" style={{ animationDelay: "120ms" }}>
          <Console key={current.id} runtime={current} onError={handle} onChanged={reloadRuntimes} />
        </section>
      )}

      <section className="block rise" style={{ animationDelay: "160ms" }}>
        <div className="block-head">
          <h2>启动新的显卡</h2>
          <span className="subtle small">单价为社区实测估算（1 CU ≈ $0.10），以 Colab 实际计费为准。其实不用先开卡：在各功能页选“自动”，提交时会自动申请。</span>
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
