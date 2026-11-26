import { useEffect, useMemo, useState } from "react";
import { api, post, put } from "../api";
import { useApp } from "../ctx";
import type { GenModel, TaskDetail } from "../types";
import { Card, Field, FileDrop, PageHead, RuntimePicker, Seg, Toggle, type PickedFile } from "../components/common";
import { ChipArt, Pic } from "../components/Logo";
import TaskPanel from "../components/TaskPanel";
import { useStoredTask } from "./shared";

type Kind = "all" | "video" | "image";

const ASPECTS: { id: string; label: string; ratio: number }[] = [
  { id: "16:9", label: "16:9 横屏", ratio: 16 / 9 },
  { id: "9:16", label: "9:16 竖屏", ratio: 9 / 16 },
  { id: "1:1", label: "1:1 方形", ratio: 1 },
  { id: "4:3", label: "4:3", ratio: 4 / 3 },
];

const IMAGE_SLOTS: Record<string, { min: number; max: number; labels: string[]; hint: string }> = {
  i2v: { min: 1, max: 1, labels: ["首帧图片"], hint: "视频会从这张图开始动起来" },
  flf2v: { min: 2, max: 2, labels: ["首帧", "尾帧"], hint: "按顺序上传：第 1 张是开头，第 2 张是结尾" },
  r2v: { min: 1, max: 9, labels: ["参考图"], hint: "1–9 张；提示词里用 <Picture 1>、<Picture 2> 按顺序指代" },
  edit: { min: 1, max: 1, labels: ["原图"], hint: "要编辑的图片，输出尺寸跟随原图" },
};

const COMMERCIAL: Record<string, string> = { yes: "可商用", conditional: "有条件商用", unknown: "商用条款见仓库", no: "非商用" };

/** Pick width/height near the model's native pixel count, multiples of 32. */
function sizeFor(model: GenModel, aspect: string, hd: boolean): [number, number] {
  const [w0, h0] = hd && model.hd_size ? model.hd_size : model.size;
  const pixels = w0 * h0;
  const ratio = ASPECTS.find((a) => a.id === aspect)?.ratio ?? w0 / h0;
  const w = Math.round(Math.sqrt(pixels * ratio) / 32) * 32;
  const h = Math.round(Math.sqrt(pixels / ratio) / 32) * 32;
  return [w, h];
}

export default function Create() {
  const { catalog, handle, toast, runtimes } = useApp();
  const [kind, setKind] = useState<Kind>("all");
  const [modelId, setModelId] = useState(() => {
    const picked = sessionStorage.getItem("gpudock.create.model");
    sessionStorage.removeItem("gpudock.create.model");
    return picked || "wan22_5b";
  });
  const [mode, setMode] = useState("t2v");
  const [prompt, setPrompt] = useState("清晨的海边小镇，一只橘猫沿着石阶慢慢走下来，镜头缓缓跟随，电影感，柔和逆光");
  const [aspect, setAspect] = useState("16:9");
  const [seconds, setSeconds] = useState(5);
  const [seed, setSeed] = useState<string>("");
  const [hd, setHd] = useState(false);
  const [fast, setFast] = useState(true);
  const [images, setImages] = useState<PickedFile[]>([]);
  const [runtime, setRuntime] = useState<string | null>(null);
  const [hasHf, setHasHf] = useState<boolean | null>(null);
  const [hfInput, setHfInput] = useState("");
  const [taskId, setTaskId] = useStoredTask("create");

  const models = catalog?.gen_models ?? {};
  const model = models[modelId];
  const modes = model ? Object.keys(model.templates) : [];

  useEffect(() => {
    if (model && !modes.includes(mode)) setMode(modes[0]);
    if (model?.kind === "image" && aspect === "16:9" && model.size[0] === model.size[1]) setAspect("1:1");
    if (model?.max_seconds && seconds > model.max_seconds) setSeconds(model.max_seconds);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modelId]);

  useEffect(() => {
    api<{ has_token: boolean }>("/api/hf-token").then((r) => setHasHf(r.has_token)).catch(handle);
  }, [handle]);

  const slots = IMAGE_SLOTS[mode];
  const ready = images.filter((f) => f.status === "done");
  const uploading = images.some((f) => f.status === "uploading");
  const [w, h] = model ? sizeFor(model, aspect, hd) : [0, 0];
  const isEdit = mode === "edit";
  const current = runtimes.find((r) => r.id === runtime);
  const gpuOk = !model || !current || model.gpus.includes(current.accelerator);
  const needsHf = !!model?.gated && hasHf === false;
  const imagesOk = !slots || (ready.length >= slots.min && ready.length <= slots.max);
  const canStart = !!runtime && !!model && prompt.trim().length > 0 && imagesOk && !uploading && !needsHf;

  const list = useMemo(
    () => Object.entries(models).filter(([, m]) => kind === "all" || m.kind === kind),
    [models, kind],
  );

  async function saveHf() {
    try {
      const r = await put<{ has_token: boolean }>("/api/hf-token", { token: hfInput });
      setHasHf(r.has_token);
      setHfInput("");
      toast("success", "Hugging Face Token 已保存到系统钥匙串。");
    } catch (e) {
      handle(e);
    }
  }

  async function start() {
    if (!runtime || !model) return;
    try {
      const t = await post<TaskDetail>("/api/tasks", {
        kind: "generate",
        runtime,
        params: {
          model: modelId,
          mode,
          prompt,
          width: w,
          height: h,
          seconds: model.kind === "video" ? seconds : undefined,
          seed: seed.trim() ? Number(seed) : undefined,
          fast: model.fast ? fast : undefined,
        },
        uploads: slots ? ready.map((f) => f.id) : [],
      });
      setTaskId(t.id);
    } catch (e) {
      handle(e);
    }
  }

  return (
    <>
      <PageHead
        eyebrow="03 / 创作 · 生图与生视频"
        title="创作"
        accent=""
        lede="用开源模型在你的 Colab 显卡上生图、生视频。模型和工作流来自 ComfyUI 官方模板，第一次使用会自动部署 ComfyUI 并下载模型。"
      />

      <div className="create-grid">
        <section className="panel rise">
          <div className="panel-head">
            <h2>1. 选模型</h2>
            <Seg
              value={kind}
              onChange={setKind}
              options={[
                { value: "all", label: "全部" },
                { value: "video", label: "视频" },
                { value: "image", label: "图像" },
              ]}
            />
          </div>
          <div className="gen-list">
            {list.map(([id, m]) => (
              <button key={id} type="button" className={`gen-item ${id === modelId ? "on" : ""}`} onClick={() => setModelId(id)}>
                <Pic src={`/img/${m.kind === "video" ? "feat-video" : "feat-image"}.webp`} className="gen-pic" fallback={<ChipArt size={28} />} />
                <span className="gen-text">
                  <span className="gen-name">
                    {m.name}
                    {m.experimental && <span className="tag">实验性</span>}
                  </span>
                  <span className="subtle small">{m.vendor} · {m.kind === "video" ? "视频" : "图像"} · 显存 ≈ {m.vram_gb} GB · 下载约 {m.download_gb} GB</span>
                </span>
              </button>
            ))}
          </div>
        </section>

        <div className="col">
          {model && (
            <section className="panel rise" style={{ animationDelay: "40ms" }}>
              <div className="panel-head">
                <h2>{model.name}</h2>
                <span className="mono subtle small">{model.license}</span>
              </div>
              <p className="model-summary">{model.summary}</p>
              <div className="uses">
                <span className={`use ${model.commercial === "yes" ? "ok-use" : ""}`}>{COMMERCIAL[model.commercial]}</span>
                <span className="use">推荐 {model.gpus.join(" / ")}</span>
                {model.fps && <span className="use">{model.fps} fps · 最长 {model.max_seconds} 秒</span>}
                {model.gated && <span className="use">需 HF 授权</span>}
              </div>
              {model.license_note && <p className="model-note">{model.license_note}</p>}
              {model.gated && hasHf === false && (
                <div className="api-box">
                  <p className="small">
                    这个模型的权重需要先在 Hugging Face 打开{" "}
                    <a className="link" href={`https://huggingface.co/${model.gated}`} target="_blank" rel="noreferrer">
                      {model.gated}
                    </a>{" "}
                    申请访问并同意许可，再在这里填一个只读 Token（在 huggingface.co/settings/tokens 创建）。
                  </p>
                  <div className="chat-row">
                    <input className="input" type="password" placeholder="hf_…" value={hfInput} onChange={(e) => setHfInput(e.target.value)} />
                    <button className="btn" onClick={saveHf} disabled={!hfInput.trim()}>
                      保存
                    </button>
                  </div>
                </div>
              )}
            </section>
          )}

          <Card title="2. 选择显卡" hint={model ? `建议 ${model.gpus.join(" / ")}` : undefined} delay={80}>
            <RuntimePicker value={runtime} onChange={setRuntime} minVram={model?.vram_gb ?? 0} suggest={model?.gpus[0] ?? "L4"} />
            {!gpuOk && <p className="err-text small">这张卡不在推荐列表里，可能显存或内存不够，建议换 {model?.gpus.join(" / ")}。</p>}
          </Card>

          <Card title="3. 描述你想要的画面" delay={120}>
            {modes.length > 1 && (
              <Field label="模式">
                <Seg value={mode} onChange={setMode} options={modes.map((m) => ({ value: m, label: catalog?.mode_labels[m] ?? m }))} />
              </Field>
            )}
            <Field label="提示词" hint="中英文都可以，写清主体、动作、场景、镜头和光线">
              <textarea className="input textarea" rows={4} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
            </Field>
            {slots && (
              <Field label={slots.labels.join(" / ")} hint={slots.hint}>
                <FileDrop files={images} onChange={setImages} accept="image/*" multiple={slots.max > 1} label="拖入图片" />
              </Field>
            )}
            <div className="lang-row">
              {!isEdit && (
                <Field label={`画幅 · ${w}×${h}`}>
                  <select className="input" value={aspect} onChange={(e) => setAspect(e.target.value)}>
                    {ASPECTS.map((a) => (
                      <option key={a.id} value={a.id}>
                        {a.label}
                      </option>
                    ))}
                  </select>
                </Field>
              )}
              {!isEdit && model?.hd_size && (
                <Field label="清晰度">
                  <Seg
                    value={hd ? "hd" : "sd"}
                    onChange={(v) => setHd(v === "hd")}
                    options={[
                      { value: "sd", label: "标准" },
                      { value: "hd", label: "高清" },
                    ]}
                  />
                </Field>
              )}
              <Field label="种子（留空随机）">
                <input className="input mono" value={seed} placeholder="随机" onChange={(e) => setSeed(e.target.value.replace(/[^0-9]/g, ""))} />
              </Field>
            </div>
            {model?.kind === "video" && (
              <Field label={`时长 ${seconds} 秒`} hint="越长越慢，显存占用也越高">
                <input type="range" min={1} max={model.max_seconds ?? 5} step={1} value={seconds} onChange={(e) => setSeconds(Number(e.target.value))} />
              </Field>
            )}
            {model?.fast && (
              <Toggle checked={fast} onChange={setFast} label={`加速生成（${model.fast}，快几倍、画质略降）`} />
            )}
            {hd && model?.kind === "video" && <p className="subtle small">高清视频更吃显存和时间，建议 A100 80GB / RTX PRO 6000 / H100。</p>}
            <button className="btn primary full" disabled={!canStart} onClick={start}>
              {uploading ? "等待图片上传…" : model?.kind === "video" ? "生成视频" : "生成图片"} <span className="arrow">→</span>
            </button>
          </Card>
        </div>
      </div>

      {taskId && (
        <Card title="生成结果" delay={0}>
          <TaskPanel taskId={taskId} />
        </Card>
      )}
    </>
  );
}
