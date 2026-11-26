"""Check GPUDock's template parameter map against a live ComfyUI (maintainers).

Run it whenever ComfyUI or its workflow templates update. Every model/mode in
GEN_MODELS is converted to API format, filled in with test values and submitted
to ComfyUI's /prompt, whose validator checks node types, links, required inputs
and combo values. Model files are replaced by empty placeholders and execution
is interrupted right away, so a CPU-only ComfyUI is enough:

    git clone --depth 1 https://github.com/Comfy-Org/ComfyUI && cd ComfyUI
    uv venv -p 3.12 && uv pip install -r requirements.txt comfy-cli
    .venv/bin/python main.py --cpu --port 8199 &
    .venv/bin/python ../scripts/validate_templates.py packages/core/src/gpudock_core/agent . 8199

usage: validate_templates.py <agent_dir> <comfy_dir> <port>
Note: it writes empty placeholder files into <comfy_dir>/models.
"""
import json
import struct
import sys
import time
import urllib.request
import zlib
from pathlib import Path

agent_dir, comfy_dir, port = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])
sys.path.insert(0, agent_dir)
import gpudock_agent as ga  # noqa: E402
from comfy_cli.workflow_to_api import convert_ui_to_api  # noqa: E402

BASE = f"http://127.0.0.1:{port}"


def post(path, data):
    req = urllib.request.Request(BASE + path, data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def png(path, w=64, h=48):
    raw = b"".join(b"\x00" + bytes([200, 120, 40]) * w for _ in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


with urllib.request.urlopen(BASE + "/object_info", timeout=60) as r:
    object_info = json.loads(r.read())
(comfy_dir / "input").mkdir(exist_ok=True)
names = []
for i in range(9):
    name = f"gpudock_test_{i}.png"
    png(comfy_dir / "input" / name)
    names.append(name)

touched = set()
ok = fail = 0
for gen, spec in ga.GEN_MODELS.items():
    for mode, tpl in spec["templates"].items():
        wf = json.loads(ga.template_file(tpl).read_text())
        lo, hi = {"i2v": (1, 1), "flf2v": (2, 2), "r2v": (1, 9), "edit": (1, 1)}.get(mode, (0, 0))
        imgs = names[:hi]
        ga._prepare_ui(wf, tpl, mode, imgs)
        for m in ga.template_models(wf):
            f = comfy_dir / "models" / m["directory"] / m["name"]
            if not f.exists():
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(b"")
                touched.add(f)
        if touched:
            # ComfyUI caches folder listings; refresh object_info so new files show up in combos
            with urllib.request.urlopen(BASE + "/object_info", timeout=60) as r:
                object_info = json.loads(r.read())
        api = convert_ui_to_api(wf, object_info)
        w, h = spec["size"]
        params = {"prompt": f"GPUDOCK_PROMPT {gen} {mode}", "width": w, "height": h, "seed": 12345, "fast": True}
        if spec["kind"] == "video":
            params["seconds"] = 3
        ga.apply_params(api, tpl, mode, params, imgs)
        dump = json.dumps(api, ensure_ascii=False)
        tp = ga.TEMPLATE_PARAMS[tpl]
        want = {"prompt": params["prompt"], "width": w, "height": h, "seed": 12345, "seconds": 3.0, "seconds_int": 3, "fast": True}
        checks = []
        for key, expected in want.items():
            for node, field in tp.get(key, []) if isinstance(tp.get(key), list) else []:
                checks.append(api[node]["inputs"].get(field) == expected)
        if "frames" in tp:
            checks.append(all(api[n]["inputs"][f] == ga._frames(3, tp["fps"], tp.get("frame_step", 4)) for n, f in tp["frames"]))
        checks.append(bool(checks))
        for n in imgs[: len(ga.TEMPLATE_PARAMS[tpl].get("image_slots", {}).get(mode, [])) or len(ga.TEMPLATE_PARAMS[tpl].get("ui_images", {}).get(mode, []))]:
            checks.append(n in dump)
        status, body = post("/prompt", {"prompt": api, "client_id": "gpudock-validate"})
        if status == 200 and all(checks):
            ok += 1
            print(f"OK    {gen:<14} {mode:<6} {tpl}")
        else:
            fail += 1
            print(f"FAIL  {gen:<14} {mode:<6} {tpl} status={status} checks={checks}")
            print("      ", ga._prompt_error(body)[:1200] if status != 200 else "")
        post("/interrupt", {})
        post("/queue", {"clear": True})
        time.sleep(0.3)
print(f"\n{ok} ok, {fail} failed; placeholders created: {len(touched)}")
