"""Pure helpers in the runtime agent (no runtime needed)."""

import pytest

from gpudock_core.agent import gpudock_agent as ag


def words(tokens, step=0.5):
    return [{"start": i * step, "end": i * step + step * 0.8, "word": w} for i, w in enumerate(tokens)]


def test_split_sentences_at_sentence_end():
    out = ag.split_sentences(words(["大家", "好，", "欢迎", "来到", "节目。", "今天", "聊", "显卡。"]))
    assert [s["text"] for s in out] == ["大家好，欢迎来到节目。", "今天聊显卡。"]
    assert out[1]["start"] == pytest.approx(2.5)


def test_split_sentences_breaks_long_runs_at_commas_or_pauses():
    toks = [f" w{i}" + ("," if i == 9 else "") for i in range(20)]
    out = ag.split_sentences(words(toks, 0.6), max_seconds=8)
    assert out[0]["text"].endswith("w9,") and len(out) == 2
    no_punct = ag.split_sentences(words([f" x{i}" for i in range(30)], 0.6), max_seconds=5)
    assert all(s["end"] - s["start"] <= 5.5 for s in no_punct)
    assert "".join(s["text"] for s in no_punct).replace(" ", "") == "".join(f"x{i}" for i in range(30))


def test_split_sentences_ignores_numbered_list_dots():
    out = ag.split_sentences(words(["Step", " 1.", " open", " it."]))
    assert len(out) == 1


def test_fix_zh_punct():
    assert ag.fix_zh_punct("大家好,欢迎来到节目.我们聊聊GPU?好的!") == "大家好，欢迎来到节目。我们聊聊GPU？好的！"
    assert ag.fix_zh_punct("version 1.5, ok") == "version 1.5, ok"


def test_parse_translations_tolerates_fences_and_think():
    content = '<think>hmm</think>\n```json\n{"translations": [{"id": 2, "text": " 你好 "}]}\n```'
    assert ag.parse_translations(content) == {2: "你好"}
    with pytest.raises(ValueError):
        ag.parse_translations("no json here")


def test_translation_prompt_mentions_rate_and_ids():
    msgs = ag.translation_messages([{"id": 7, "text": "hi", "seconds": 1.234}], "en", "zh")
    assert "中文" in msgs[0]["content"] and "4.5" in msgs[0]["content"]
    assert '"id": 7' in msgs[1]["content"] and '"seconds": 1.2' in msgs[1]["content"]


def test_srt_and_vtt_formatting():
    segs = [{"start": 0, "end": 1.5, "text": "a", "source": "b"}, {"start": 3661.25, "end": 3662, "text": "c"}]
    srt = ag.to_srt(segs, second="source")
    assert "1\n00:00:00,000 --> 00:00:01,500\na\nb\n" in srt
    assert "01:01:01,250 --> 01:01:02,000" in srt
    assert ag.to_vtt(segs).startswith("WEBVTT\n\n00:00:00.000 --> 00:00:01.500")


def test_recipes_and_voices_are_consistent():
    for rid, r in ag.RECIPES.items():
        assert {"name", "category", "license", "commercial", "vram_gb", "summary"} <= set(r), rid
    for code in {v["kokoro"] for v in ag.LANGS.values() if v["kokoro"]}:
        assert ag.KOKORO_VOICES.get(code), code


def test_simulate_placement_accounts_for_late_starts():
    from gpudock_core.server.tasks import simulate_placement

    segs = [{"start": 0.0, "tts": "a"}, {"start": 1.5, "tts": "b"}, {"start": 5.4, "tts": "c"}]
    durs = {"a": 2.1, "b": 4.0, "c": 7.8}
    ratios = [round(r, 2) for _, r in simulate_placement(segs, durs, 13.2)]
    # the third line starts late because the first two borrowed time, so it needs more speed-up
    naive = 7.8 / (13.2 - 5.4 - 0.05)
    assert ratios[2] > naive
    assert all(r > 0 for r in ratios)


def test_every_generation_mode_is_mapped():
    from gpudock_core.agent.gpudock_agent import (
        GEN_MODELS,
        MODE_LABELS,
        TEMPLATE_PARAMS,
    )
    from gpudock_core.server.tasks import GEN_IMAGES

    for gen, spec in GEN_MODELS.items():
        for mode, tpl in spec["templates"].items():
            assert mode in MODE_LABELS, (gen, mode)
            tp = TEMPLATE_PARAMS[tpl]
            assert tp.get("prompt") and tp.get("seed"), tpl
            if spec["kind"] == "video":
                assert any(k in tp for k in ("seconds", "seconds_int", "frames")), tpl
                if "frames" in tp:
                    assert tp["fps"] == spec["fps"], tpl
            if mode != "edit":
                assert "width" in tp and "height" in tp, tpl
            need = GEN_IMAGES.get(mode, (0, 0))[1]
            slots = len(tp.get("ui_images", {}).get(mode, [])) + len(tp.get("image_slots", {}).get(mode, []))
            assert slots >= need, (tpl, mode, slots, need)
            if spec.get("fast"):
                assert tp.get("fast") or mode == "flf2v", tpl
        w, h = spec["size"]
        assert w % 32 == 0 and h % 16 == 0


def test_apply_params_fills_api_prompt():
    from gpudock_core.agent.gpudock_agent import _frames, apply_params

    api = {
        "3": {"class_type": "KSampler", "inputs": {"seed": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "old"}},
        "55": {"class_type": "Wan22ImageToVideoLatent", "inputs": {"width": 1, "height": 1, "length": 1}},
    }
    apply_params(api, "video_wan2_2_5B_ti2v", "t2v", {"prompt": "猫", "width": 1280, "height": 704, "seconds": 3, "seed": 9}, [])
    assert api["6"]["inputs"]["text"] == "猫" and api["3"]["inputs"]["seed"] == 9
    assert api["55"]["inputs"] == {"width": 1280, "height": 704, "length": _frames(3, 24, 4)} and _frames(3, 24, 4) == 73

    api = {"136": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {"ref_images.ref_image_0": ["137", 0], "ref_images.ref_image_1": ["139", 0], "width": 1}},
           "137": {"class_type": "LoadImage", "inputs": {}}, "139": {"class_type": "LoadImage", "inputs": {}},
           "138": {"inputs": {}}, "132": {"inputs": {}}, "129": {"inputs": {}}, "146": {"inputs": {}}}
    apply_params(api, "video_minimax_h3_r2v", "r2v", {"prompt": "p", "width": 832, "height": 480, "seconds": 5, "seed": 1, "fast": False}, ["a.png"])
    assert "137" not in api and "139" not in api and "ref_images.ref_image_1" not in api["136"]["inputs"]
    assert api["136"]["inputs"]["ref_images.ref_image_0"] == ["gpudock_img_0", 0]
    assert api["gpudock_img_0"]["inputs"]["image"] == "a.png" and api["146"]["inputs"]["value"] is False


def test_download_resumes_after_dropped_connection(tmp_path, monkeypatch):
    import httpx

    from gpudock_core import files

    monkeypatch.setattr(files.time, "sleep", lambda s: None)
    payload = bytes(range(256)) * 4096  # 1 MiB
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("Range"))
        start = int(request.headers["Range"].split("=")[1].rstrip("-")) if "Range" in request.headers else 0
        if len(calls) == 1:
            def broken():
                yield payload[:300_000]
                raise httpx.ReadError("SSL: UNEXPECTED_EOF_WHILE_READING")
            return httpx.Response(200, content=broken())
        return httpx.Response(206, content=payload[start:], headers={"Content-Range": f"bytes {start}-{len(payload) - 1}/{len(payload)}"})

    class Conn:
        client = httpx.Client(transport=httpx.MockTransport(handler))

        def headers(self):
            return {}

        def url_for(self, path):
            return "http://rt/" + path

        def refresh_token(self):
            return False

    out = tmp_path / "x.bin"
    files.download(Conn(), "a/x.bin", out, block=65536)
    assert out.read_bytes() == payload
    assert calls[0] is None and calls[1].startswith("bytes=")


def test_stats_never_queues_behind_a_slow_read():
    import time

    from gpudock_core.server.state import RuntimeHandle

    h = RuntimeHandle("runtimes/x", api=None)
    h.stats_cache, h.stats_at = {"ram": {"used_gb": 1}}, 0.0
    h.monitor_lock.acquire()  # a read is in flight
    t = time.monotonic()
    assert h.stats() == {"ram": {"used_gb": 1}, "stale": True}
    assert time.monotonic() - t < 0.5
    h.monitor_lock.release()
    h.stats_failed_at = time.time()  # just failed: back off instead of hammering a dead kernel
    assert h.stats()["stale"] is True
