"""Drive the M0 experiments end to end against a fake Colab API whose runtimes
are the local jupyter_server. Checks the orchestration (create -> connect ->
work -> release, bookkeeping for cleanup), not Google's backend."""

from datetime import UTC, datetime, timedelta

import pytest

from gpudock_core.colab_api import ConnectionInfo, Runtime, RuntimeSpec, SpecKey
from gpudock_core.errors import ColabApiError
from gpudock_core.m0 import experiments as ex
from gpudock_core.m0.recorder import Recorder

pytestmark = pytest.mark.integration

T4 = SpecKey("VARIANT_GPU", "T4", "SHAPE_STANDARD")
L4 = SpecKey("VARIANT_GPU", "L4", "SHAPE_STANDARD")
H100 = SpecKey("VARIANT_GPU", "H100", "SHAPE_STANDARD")


class FakeApi:
    def __init__(self, url, refuse=()):
        self.url = url
        self.refuse = set(refuse)
        self.live: dict[str, Runtime] = {}
        self.deleted: list[str] = []
        self.user_runtime = Runtime("runtimes/users-own-notebook", T4, None, self._conn())
        self.live[self.user_runtime.name] = self.user_runtime

    def _conn(self):
        return ConnectionInfo(self.url, "tok", datetime.now(UTC) + timedelta(hours=1))

    def list_specs(self):
        return [RuntimeSpec(T4, True), RuntimeSpec(L4, True), RuntimeSpec(H100, False)]

    def list_runtimes(self):
        return list(self.live.values())

    def get_runtime(self, name):
        return Runtime(name, self.live[name].spec, None, self._conn())

    def create_runtime(self, spec, *, runtime_id=None, on_progress=None, **_):
        if spec.accelerator in self.refuse:
            raise ColabApiError(None, 9, "FAILED_PRECONDITION", "no capacity", reason="ACCELERATOR_UNAVAILABLE")
        rt = Runtime(f"runtimes/{runtime_id}", spec, None, self._conn())
        self.live[rt.name] = rt
        return rt

    def delete_runtime(self, name, allow_missing=True):
        self.deleted.append(name)
        self.live.pop(name, None)


def test_e2e_creates_works_and_releases(jupyter, tmp_path):
    url, root = jupyter
    api = FakeApi(url)
    rec = Recorder(tmp_path)
    lines = []
    summary = ex.e2e(api, rec, lines.append, gpu="T4", size_mb=3, keep=False, runtime_name=None, chunk_mb=1, remote_root=str(root))
    assert summary["hello_ok"] and summary["upload_integrity"] and summary["download_integrity"]
    assert summary["interrupt_ok"]
    assert "gpu_error" in summary["machine"] or "gpu" not in summary["machine"]  # no GPU locally
    created = [n for n in api.deleted if n.startswith("runtimes/gpudock-m0-")]
    assert len(created) == 1 and rec.created_runtimes() == []
    assert "runtimes/users-own-notebook" in api.live
    events = [r["event"] for r in rec.records()]
    assert events[:1] == ["created"] and "summary" in events and "deleted" in events


def test_capacity_records_success_refusal_and_skip(jupyter, tmp_path):
    url, _ = jupyter
    api = FakeApi(url, refuse={"L4"})
    rec = Recorder(tmp_path)
    results = ex.capacity(api, rec, ["T4", "L4", "H100", "TPU-X"], lambda s: None, pause_s=0)
    assert [r["event"] for r in results] == ["created", "refused", "skipped", "skipped"]
    assert results[1]["error"]["reason"] == "ACCELERATOR_UNAVAILABLE"
    assert rec.created_runtimes() == []
    assert list(api.live) == ["runtimes/users-own-notebook"]


def test_cleanup_only_touches_own_runtimes(jupyter, tmp_path):
    url, _ = jupyter
    api = FakeApi(url)
    rec = Recorder(tmp_path)
    mine = api.create_runtime(T4, runtime_id="gpudock-m0-left")
    rec.remember_runtime(mine.name)
    rec.remember_runtime("runtimes/gpudock-m0-already-gone")
    deleted = ex.cleanup(api, rec, lambda s: None, yes=True, ask=lambda q: False)
    assert deleted == ["runtimes/gpudock-m0-left"]
    assert "runtimes/users-own-notebook" in api.live
    assert rec.created_runtimes() == []
