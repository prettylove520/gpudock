import json
from types import SimpleNamespace

import pytest

from gpudock_core.server.tasks import Task, TaskManager


def _manager(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    return TaskManager(SimpleNamespace(config_dir=cfg), root=tmp_path / "ws")


def _add(tm, tid, status, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "a.png").write_bytes(b"x")
    tm.tasks[tid] = Task(id=tid, kind="generate", title="t", runtime="", params={}, uploads=[], out_dir=str(out_dir), status=status)


def test_delete_removes_record_and_outputs(tmp_path):
    tm = _manager(tmp_path)
    out = tm.root / "outputs" / "t1-generate"
    _add(tm, "t1", "done", out)
    tm.delete("t1")
    assert "t1" not in tm.tasks and not out.exists()
    assert json.loads(tm.history_path.read_text(encoding="utf-8")) == []


def test_delete_refuses_active_and_keeps_folders_outside_outputs(tmp_path):
    tm = _manager(tmp_path)
    _add(tm, "t1", "running", tm.root / "outputs" / "t1-generate")
    with pytest.raises(ValueError):
        tm.delete("t1")
    elsewhere = tmp_path / "mine"
    _add(tm, "t2", "error", elsewhere)
    tm.delete("t2")
    assert "t2" not in tm.tasks and (elsewhere / "a.png").exists()
    with pytest.raises(LookupError):
        tm.delete("nope")
