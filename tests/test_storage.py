from __future__ import annotations

from pathlib import Path

from dev_pipeline.storage import RunStore


def test_events_and_tool_output_are_redacted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SERVICE_API_KEY", "super-secret-key")
    store = RunStore(tmp_path / "runs")

    store.append_event(
        "TASK-1",
        {
            "event": "tool_failed",
            "api_key": "visible-key",
            "message": "Authorization: Bearer super-secret-key",
        },
    )
    paths = store.save_tool_output(
        "TASK-1",
        "development",
        "claude",
        stdout="token=super-secret-key",
        stderr="Bearer super-secret-key",
    )

    event = store.read_events("TASK-1")[0]
    assert event["api_key"] == "***"
    assert "super-secret-key" not in event["message"]
    for path in paths.values():
        content = (store.run_dir("TASK-1") / path).read_text(encoding="utf-8")
        assert "super-secret-key" not in content


def test_read_events_ignores_incomplete_trailing_line(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    path = store.run_dir("TASK-1") / "events.jsonl"
    path.write_text('{"event":"complete"}\n{"event":', encoding="utf-8")

    assert store.read_events("TASK-1") == [{"event": "complete"}]


def test_tool_output_is_limited(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")

    paths = store.save_tool_output(
        "TASK-1",
        "development",
        "claude",
        stdout="x" * (store.MAX_TOOL_OUTPUT_CHARS + 1),
        stderr="",
    )

    output = (store.run_dir("TASK-1") / paths["stdout"]).read_text(encoding="utf-8")
    assert output.startswith("[truncated")
    assert len(output) < store.MAX_TOOL_OUTPUT_CHARS + 100
