"""Append-only results log for M0 experiments, plus a Markdown report.

Everything written here is meant to be shareable (e.g. pasted into a GitHub
issue), so records never contain OAuth tokens, runtime proxy tokens, full
runtime URLs or the account email.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SECRET_KEYS = {"token", "access_token", "refresh_token", "id_token", "client_secret", "authorization", "email"}
_TOKENISH = re.compile(r"(ya29\.[\w\-]+|1//[\w\-]+)")


def results_dir() -> Path:
    path = Path(os.environ.get("GPUDOCK_M0_RESULTS", "m0-results"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k.lower() in SECRET_KEYS:
                out[k] = "<redacted>"
            elif k.lower() == "url" and isinstance(v, str):
                out[k] = host_only(v)
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _TOKENISH.sub("<redacted>", value)
    return value


def host_only(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}/…" if parts.hostname else "<url>"


class Recorder:
    def __init__(self, directory: Path | None = None) -> None:
        self.dir = directory or results_dir()
        self.log = self.dir / "results.jsonl"
        self.created = self.dir / "created_runtimes.json"

    def write(self, experiment: str, event: str, **data: Any) -> dict[str, Any]:
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "experiment": experiment,
            "event": event,
            **redact(data),
        }
        with self.log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def records(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    # Runtimes this tool created. cleanup only ever deletes these, never the
    # user's own notebook runtimes.
    def remember_runtime(self, name: str) -> None:
        names = set(self.created_runtimes())
        names.add(name)
        self.created.write_text(json.dumps(sorted(names), indent=2))

    def forget_runtime(self, name: str) -> None:
        names = set(self.created_runtimes())
        names.discard(name)
        self.created.write_text(json.dumps(sorted(names), indent=2))

    def created_runtimes(self) -> list[str]:
        if not self.created.exists():
            return []
        return json.loads(self.created.read_text())


def build_report(records: list[dict[str, Any]]) -> str:
    """Summarise the log into the M0 question list."""
    by_exp: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        by_exp.setdefault(r["experiment"], []).append(r)

    lines = [
        "# M0 实测报告",
        "",
        f"生成时间：{datetime.now(UTC).isoformat(timespec='seconds')}（由 `gpudock-m0 report` 根据 results.jsonl 生成，已去除令牌和邮箱）",
        "",
    ]

    notes = {r["key"]: r for r in by_exp.get("note", []) if r.get("event") == "note"}

    def note(key: str) -> str:
        r = notes.get(key)
        return f"{r['value']}（{r['ts'][:10]} 记录）" if r else "未记录"

    lines += ["## ① OAuth scope 分类", "", f"- `colaboratory` scope：{note('scope_classification')}", ""]

    probe = [r for r in by_exp.get("probe", []) if r["event"] == "result"]
    lines += ["## 账号与规格（probe）", ""]
    if probe:
        last = probe[-1]
        lines.append(f"- 订阅等级：{last.get('tier')}")
        lines.append(f"- 已有运行时数量：{last.get('runtime_count')}")
        lines.append("")
        lines.append("| 规格 | variant | shape | 可申请 |")
        lines.append("|---|---|---|---|")
        for s in last.get("specs", []):
            lines.append(f"| {s['label']} | {s['variant']} | {s['shape']} | {'✅' if s['eligible'] else '❌'} |")
    else:
        lines.append("- 未运行（`gpudock-m0 probe`）")
    lines.append("")

    cap = [r for r in by_exp.get("capacity", []) if r["event"] in ("created", "refused", "error")]
    lines += ["## ② 免费/当前账号实际能拿到的 GPU（capacity）", ""]
    if cap:
        lines.append("| 时间 | 规格 | 结果 | 就绪耗时(s) | GPU 名称 | 显存 | 原因 |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in cap:
            gpu = r.get("gpu") or {}
            reason = (r.get("error") or {}).get("reason") or (r.get("error") or {}).get("status") or ""
            lines.append(
                f"| {r['ts'][:16]} | {r.get('spec')} | {r['event']} | {r.get('ready_seconds', '')} | "
                f"{gpu.get('name', '')} | {gpu.get('memory_total', '')} | {reason} |"
            )
    else:
        lines.append("- 未运行（`gpudock-m0 capacity`）")
    lines.append("")

    tok = by_exp.get("token-expiry", [])
    lines += ["## ③ 令牌过期后 WebSocket 是否可用（token-expiry）", ""]
    summary = [r for r in tok if r["event"] == "summary"]
    if summary:
        for r in summary:
            for k, v in r.items():
                if k not in ("ts", "experiment", "event"):
                    lines.append(f"- {k}: {v}")
    else:
        lines.append("- 未运行（`gpudock-m0 token-expiry`）")
    lines.append("")

    idle = [r for r in by_exp.get("idle", []) if r["event"] == "summary"]
    lines += ["## ④ 空闲回收时长（idle）与长时间执行（long-exec）", ""]
    for r in idle:
        lines.append(f"- 空闲：{ {k: v for k, v in r.items() if k not in ('ts', 'experiment', 'event')} }")
    for r in [r for r in by_exp.get("long-exec", []) if r["event"] == "summary"]:
        lines.append(f"- 长执行：{ {k: v for k, v in r.items() if k not in ('ts', 'experiment', 'event')} }")
    if not idle and not by_exp.get("long-exec"):
        lines.append("- 未运行（`gpudock-m0 idle` / `gpudock-m0 long-exec`）")
    lines.append("")

    e2e = [r for r in by_exp.get("e2e", []) if r["event"] in ("upload", "download", "summary")]
    lines += ["## ⑤ 端到端与传输吞吐（e2e）", ""]
    if e2e:
        for r in e2e:
            lines.append(f"- {r['event']}: { {k: v for k, v in r.items() if k not in ('ts', 'experiment', 'event')} }")
    else:
        lines.append("- 未运行（`gpudock-m0 e2e`）")
    lines.append("")

    lines += [
        "## ⑥ A100 80GB / H100 在付费账号上的规格",
        "",
        "- 见上面 probe 表格中 `A100 (HIGHMEM)`、`H100` 的“可申请”列（需要用 Pro/Pro+ 账号各跑一次 probe）。",
        "",
        "## 其他记录",
        "",
    ]
    for key, r in notes.items():
        if key != "scope_classification":
            lines.append(f"- {key}: {r['value']}")
    errors = [r for r in records if r["event"] == "error"]
    if errors:
        lines += ["", "## 错误", ""]
        for r in errors[-20:]:
            lines.append(f"- {r['ts']} {r['experiment']}: {r.get('error')}")
    return "\n".join(lines) + "\n"
