import json

from gpudock_core.colab_api import RuntimeSpec, SpecKey
from gpudock_core.m0.experiments import find_spec, parse_label
from gpudock_core.m0.recorder import Recorder, build_report, redact


def test_parse_label_variants():
    assert parse_label("t4") == ("T4", "SHAPE_STANDARD")
    assert parse_label("A100-HIGHMEM") == ("A100", "SHAPE_HIGHMEM")
    assert parse_label("a100:highmem") == ("A100", "SHAPE_HIGHMEM")
    assert parse_label("cpu") == ("NONE", "SHAPE_STANDARD")


def test_find_spec_matches_shape():
    specs = [
        RuntimeSpec(SpecKey("VARIANT_GPU", "A100", "SHAPE_STANDARD"), True),
        RuntimeSpec(SpecKey("VARIANT_GPU", "A100", "SHAPE_HIGHMEM"), False),
    ]
    assert find_spec(specs, "a100").eligible is True
    assert find_spec(specs, "a100-highmem").eligible is False
    assert find_spec(specs, "H100") is None


def test_redact_removes_secrets():
    data = {
        "token": "abc",
        "nested": {"refresh_token": "1//xyz", "url": "https://abc.prod.colab.dev/tun/m/secret/path?x=1"},
        "email": "someone@example.com",
        "note": "Bearer ya29.a0AfB_secret-value here",
        "ok": 3,
    }
    out = redact(data)
    dumped = json.dumps(out)
    assert "abc" not in out["token"] and "someone@" not in dumped
    assert "ya29.a0AfB_secret" not in dumped and "1//xyz" not in dumped
    assert out["nested"]["url"] == "https://abc.prod.colab.dev/…"
    assert out["ok"] == 3


def test_recorder_and_report(tmp_path):
    rec = Recorder(tmp_path)
    rec.write("note", "note", key="scope_classification", value="sensitive")
    rec.write(
        "probe",
        "result",
        tier="SUBSCRIPTION_TIER_FREE",
        runtime_count=0,
        specs=[{"label": "T4", "variant": "VARIANT_GPU", "accelerator": "T4", "shape": "SHAPE_STANDARD", "eligible": True}],
    )
    rec.write("capacity", "refused", spec="L4", error={"status": "FAILED_PRECONDITION", "reason": "STOCKOUT"})
    rec.remember_runtime("runtimes/a")
    rec.remember_runtime("runtimes/b")
    rec.forget_runtime("runtimes/a")
    assert rec.created_runtimes() == ["runtimes/b"]
    report = build_report(rec.records())
    assert "sensitive" in report
    assert "| T4 | VARIANT_GPU | SHAPE_STANDARD | ✅ |" in report
    assert "STOCKOUT" in report
