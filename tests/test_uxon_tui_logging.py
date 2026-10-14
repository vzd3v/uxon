"""Metrics opt-in, complete records and bounded rotation on real files."""

from __future__ import annotations

import json
import stat

from uxon.infra import events


def test_metrics_private_lifecycle_rotation_retains_recent_complete_records(tmp_path, monkeypatch):
    monkeypatch.setenv("UXON_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("UXON_METRICS", raising=False)
    events.metrics_record("disabled", elapsed_ms=12, error=None)
    assert list(tmp_path.iterdir()) == []
    monkeypatch.setenv("UXON_METRICS", "1")
    monkeypatch.setattr(events, "_METRICS_ROTATE_BYTES", 1)
    for i in range(5):
        events.metrics_record(
            f"remote:host{i}",
            elapsed_ms=i,
            error="failed" if i == 3 else None,
            attempted_at=1700000000 + i,
        )
    assert {p.name for p in tmp_path.iterdir()} == {
        "metrics.jsonl",
        "metrics.jsonl.1",
        "metrics.jsonl.2",
    }
    for suffix, i in [("", 4), (".1", 3), (".2", 2)]:
        path = tmp_path / ("metrics.jsonl" + suffix)
        [record] = [json.loads(line) for line in path.read_text().splitlines()]
        assert record.pop("ts").endswith("Z")
        assert record == {
            "source_id": f"remote:host{i}",
            "elapsed_ms": i,
            "error": "failed" if i == 3 else None,
            "from_cache": False,
            "attempted_at": 1700000000 + i,
        }
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
