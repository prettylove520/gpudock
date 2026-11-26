"""What the GUI shows about each accelerator.

Prices are community measurements (2026-03, $0.10 per compute unit) and only
used for rough estimates in the UI; Colab does not publish exact rates.
"""

from __future__ import annotations

from typing import Any

from ..agent.gpudock_agent import GEN_MODELS
from ..colab_api import RuntimeSpec

ORDER = ["T4", "L4", "A100", "G4", "H100", "V5E1", "V6E1", "NONE"]

CATALOG: dict[str, dict[str, Any]] = {
    "T4": {
        "name": "NVIDIA T4",
        "kind": "gpu",
        "vram_gb": 15,
        "arch": "Turing",
        "cu_per_hour": 1.19,
        "tagline": "入门款，最容易拿到",
        "good_for": ["Whisper 转写", "TTS 配音", "7B 量化模型", "SDXL"],
        "notes": "不支持 bf16，部分新模型会慢一些",
        "highmem": {"label": "高内存", "detail": "更多系统内存（需 Pro）"},
    },
    "L4": {
        "name": "NVIDIA L4",
        "kind": "gpu",
        "vram_gb": 22.5,
        "arch": "Ada Lovelace",
        "cu_per_hour": 1.71,
        "tagline": "性价比首选",
        "good_for": ["批量配音全流程", "8B 模型", "Flux fp8"],
        "notes": "支持 bf16 / fp8",
    },
    "A100": {
        "name": "NVIDIA A100",
        "kind": "gpu",
        "vram_gb": 40,
        "arch": "Ampere",
        "cu_per_hour": 5.40,
        "tagline": "大模型推理",
        "good_for": ["14–32B 模型", "视频生成", "批量出图"],
        "notes": "",
        "highmem": {"label": "80GB 显存", "detail": "显存翻倍，适合 70B 量化模型", "vram_gb": 80, "cu_per_hour": 7.52},
    },
    "G4": {
        "name": "RTX PRO 6000",
        "kind": "gpu",
        "vram_gb": 96,
        "arch": "Blackwell",
        "cu_per_hour": 8.71,
        "tagline": "超大显存",
        "good_for": ["70B 模型", "视频生成", "多模型同时常驻"],
        "notes": "",
    },
    "H100": {
        "name": "NVIDIA H100",
        "kind": "gpu",
        "vram_gb": 80,
        "arch": "Hopper",
        "cu_per_hour": None,
        "tagline": "旗舰算力",
        "good_for": ["大模型推理", "训练"],
        "notes": "Colab 没有公开单价，社区也还没有实测数据",
    },
    "V5E1": {
        "name": "TPU v5e-1",
        "kind": "tpu",
        "vram_gb": 16,
        "arch": "TPU",
        "cu_per_hour": None,
        "tagline": "仅 JAX / PyTorch-XLA",
        "good_for": ["JAX 训练与推理"],
        "notes": "大部分开源模型不能直接在 TPU 上跑",
    },
    "V6E1": {
        "name": "TPU v6e-1",
        "kind": "tpu",
        "vram_gb": 32,
        "arch": "TPU",
        "cu_per_hour": None,
        "tagline": "仅 JAX / PyTorch-XLA",
        "good_for": ["JAX 训练与推理"],
        "notes": "大部分开源模型不能直接在 TPU 上跑",
    },
    "NONE": {
        "name": "CPU",
        "kind": "cpu",
        "vram_gb": None,
        "arch": "—",
        "cu_per_hour": None,
        "tagline": "无加速器",
        "good_for": ["准备数据", "下载文件"],
        "notes": "",
        "highmem": {"label": "高内存", "detail": "更多系统内存"},
    },
}


def info_for(accelerator: str, shape: str = "SHAPE_STANDARD") -> dict[str, Any]:
    base = dict(CATALOG.get(accelerator.upper(), {"name": accelerator, "kind": "gpu", "vram_gb": None, "cu_per_hour": None}))
    hm = base.get("highmem") or {}
    if shape == "SHAPE_HIGHMEM":
        for key in ("vram_gb", "cu_per_hour"):
            if key in hm:
                base[key] = hm[key]
    return base


def features_for(accelerator: str) -> tuple[list[str], list[str]]:
    """Which GPUDock features run on this accelerator: (can, cannot)."""
    accel = accelerator.upper()
    if CATALOG.get(accel, {}).get("kind") != "gpu":
        return [], ["GPUDock 的功能都需要 GPU"]
    can = ["转写", "配音", "朗读"]
    cannot = []
    if accel == "T4":
        cannot.append("声音克隆（需高内存版）")
    else:
        can.append("声音克隆")
    for m in GEN_MODELS.values():
        (can if accel in m["gpus"] else cannot).append(m["name"])
    return can, cannot


def build_cards(specs: list[RuntimeSpec]) -> list[dict[str, Any]]:
    """Group specs by accelerator into one card each, with standard/highmem options."""
    cards: dict[str, dict[str, Any]] = {}
    for spec in specs:
        accel = spec.key.accelerator.upper() or "NONE"
        info = CATALOG.get(accel, {"name": accel, "kind": "gpu", "vram_gb": None, "cu_per_hour": None, "tagline": "", "good_for": [], "notes": ""})
        card = cards.setdefault(
            accel,
            {
                "accelerator": accel,
                "variant": spec.key.variant,
                **{k: v for k, v in info.items() if k != "highmem"},
                "options": [],
            },
        )
        card["can_run"], card["cannot_run"] = features_for(accel)
        if spec.key.shape == "SHAPE_HIGHMEM":
            hm = info.get("highmem") or {"label": "高内存", "detail": ""}
            option = {
                "shape": spec.key.shape,
                "label": hm["label"],
                "detail": hm.get("detail", ""),
                "vram_gb": hm.get("vram_gb", info.get("vram_gb")),
                "cu_per_hour": hm.get("cu_per_hour", info.get("cu_per_hour")),
                "eligible": spec.eligible,
            }
        else:
            option = {
                "shape": spec.key.shape,
                "label": "标准",
                "detail": "",
                "vram_gb": info.get("vram_gb"),
                "cu_per_hour": info.get("cu_per_hour"),
                "eligible": spec.eligible,
            }
        card["options"].append(option)
    out = []
    for accel in sorted(cards, key=lambda a: ORDER.index(a) if a in ORDER else len(ORDER)):
        card = cards[accel]
        card["options"].sort(key=lambda o: o["shape"] != "SHAPE_STANDARD")
        card["eligible"] = any(o["eligible"] for o in card["options"])
        out.append(card)
    return out
