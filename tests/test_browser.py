from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dev_pipeline.browser import PlaywrightBrowserVerifier
from dev_pipeline.errors import PipelineError, ValidationError
from dev_pipeline.storage import RunStore


class FakeLocator:
    def __init__(self, page: FakePage, selector: str) -> None:
        self.page = page
        self.selector = selector

    def click(self, **kwargs: Any) -> None:
        self.page.calls.append(("click", self.selector))

    def fill(self, value: str, **kwargs: Any) -> None:
        self.page.calls.append(("fill", self.selector, value))

    def press(self, key: str, **kwargs: Any) -> None:
        self.page.calls.append(("press", self.selector, key))

    def wait_for(self, **kwargs: Any) -> None:
        self.page.calls.append(("wait_for", self.selector))

    def text_content(self, **kwargs: Any) -> str:
        return self.page.texts.get(self.selector, "")


class FakePage:
    def __init__(self, page_error: str | None = None) -> None:
        self.url = ""
        self.calls: list[tuple[str, ...]] = []
        self.texts = {"h1": "控制台已加载"}
        self.page_error = page_error
        self.handlers: dict[str, Any] = {}

    def on(self, event: str, callback: Any) -> None:
        self.handlers[event] = callback
        self.calls.append(("on", event))

    def goto(self, url: str, **kwargs: Any) -> None:
        self.url = url
        self.calls.append(("goto", url))
        if self.page_error and "pageerror" in self.handlers:
            self.handlers["pageerror"](RuntimeError(self.page_error))

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    def screenshot(self, *, path: str, **kwargs: Any) -> None:
        Path(path).write_bytes(b"png")

    def close(self) -> None:
        self.calls.append(("close",))


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page

    def new_page(self) -> FakePage:
        return self.page


def make_verifier(tmp_path: Path, **overrides: Any) -> PlaywrightBrowserVerifier:
    config = {
        "start_command": "npm run serve",
        "base_url": "http://127.0.0.1:8080",
        **overrides,
    }
    return PlaywrightBrowserVerifier(RunStore(tmp_path / "runs"), config)


def test_browser_scenario_executes_actions_without_recording_fill_value(tmp_path: Path) -> None:
    verifier = make_verifier(tmp_path, screenshots="always")
    page = FakePage()
    scenario = {
        "name": "登录后打开控制台",
        "path": "/login",
        "actions": [
            {"action": "fill", "selector": "#password", "value": "top-secret"},
            {"action": "click", "selector": "button[type=submit]"},
            {"action": "press", "selector": "#search", "key": "Enter"},
            {"action": "wait_for", "selector": "h1"},
            {"action": "expect_visible", "selector": "h1"},
            {"action": "expect_text", "selector": "h1", "text": "控制台"},
            {"action": "goto", "path": "/dashboard"},
            {"action": "expect_url", "value": "/dashboard"},
        ],
    }

    result = verifier._run_scenario(FakeContext(page), scenario, 1, tmp_path)

    assert result["status"] == "passed"
    assert result["screenshot"] is not None
    assert Path(result["screenshot"]).is_file()
    assert "top-secret" not in json.dumps(result, ensure_ascii=False)
    assert ("fill", "#password", "top-secret") in page.calls
    assert ("fill", "#password", "••••") in page.calls


def test_browser_scenario_failure_saves_screenshot(tmp_path: Path) -> None:
    verifier = make_verifier(tmp_path)
    page = FakePage()
    scenario = {
        "name": "文本断言失败",
        "actions": [{"action": "expect_text", "selector": "h1", "text": "不存在"}],
    }

    result = verifier._run_scenario(FakeContext(page), scenario, 1, tmp_path)

    assert result["status"] == "failed"
    assert "Expected text not found" in result["message"]
    assert Path(result["screenshot"]).is_file()


def test_browser_defaults_to_external_smoke_scenario(tmp_path: Path) -> None:
    verifier = make_verifier(tmp_path)

    assert verifier.scenarios == [{"name": "smoke", "path": "/", "actions": []}]


def test_browser_rejects_navigation_to_another_origin(tmp_path: Path) -> None:
    verifier = make_verifier(tmp_path)

    with pytest.raises(ValidationError, match="base_url"):
        verifier._url("https://example.test/login")


def test_browser_refuses_to_reuse_an_occupied_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = make_verifier(tmp_path)
    monkeypatch.setattr(verifier, "_server_is_ready", lambda: True)

    with pytest.raises(PipelineError) as error:
        verifier._start_server(tmp_path, tmp_path / "server.log")

    assert error.value.code == "browser_server_port_in_use"


def test_browser_environment_requires_string_values(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="environment"):
        make_verifier(tmp_path, environment={"PORT": 8080})


def test_browser_fails_on_page_error_by_default(tmp_path: Path) -> None:
    verifier = make_verifier(tmp_path)

    result = verifier._run_scenario(
        FakeContext(FakePage("render crashed")),
        {"name": "page error", "actions": []},
        1,
        tmp_path,
    )

    assert result["status"] == "failed"
    assert result["page_errors"] == ["render crashed"]


def test_browser_can_ignore_known_page_errors(tmp_path: Path) -> None:
    verifier = make_verifier(tmp_path, fail_on_page_error=False)

    result = verifier._run_scenario(
        FakeContext(FakePage("known third-party error")),
        {"name": "page error", "actions": []},
        1,
        tmp_path,
    )

    assert result["status"] == "passed"
    assert result["page_errors"] == ["known third-party error"]


def test_browser_rejects_blank_selector(tmp_path: Path) -> None:
    verifier = make_verifier(tmp_path)

    result = verifier._run_scenario(
        FakeContext(FakePage()),
        {"name": "blank selector", "actions": [{"action": "click", "selector": "  "}]},
        1,
        tmp_path,
    )

    assert result["status"] == "failed"
    assert "requires selector" in result["message"]
