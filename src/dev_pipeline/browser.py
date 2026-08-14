from __future__ import annotations

import os
import re
import signal
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from .errors import PipelineError, ValidationError
from .storage import RunStore


class BrowserVerifier(Protocol):
    def verify(self, task_id: str, worktree: Path) -> dict[str, Any]: ...


class PlaywrightBrowserVerifier:
    """Runs deterministic browser scenarios without writing into the target project."""

    ACTIONS = {
        "click",
        "fill",
        "press",
        "goto",
        "wait_for",
        "expect_visible",
        "expect_text",
        "expect_url",
    }

    def __init__(self, store: RunStore, config: dict[str, Any]) -> None:
        self.store = store
        self.start_command = self._required_text(config, "start_command")
        self.base_url = self._required_text(config, "base_url").rstrip("/")
        if urllib.parse.urlparse(self.base_url).scheme not in {"http", "https"}:
            raise ValidationError("pipeline.browser.base_url must use http or https")
        scenarios = config.get("scenarios")
        if scenarios is None:
            scenarios = [{"name": "smoke", "path": "/", "actions": []}]
        if not isinstance(scenarios, list) or not scenarios:
            raise ValidationError("pipeline.browser.scenarios must be a non-empty array")
        self.scenarios = scenarios
        self.startup_timeout = int(config.get("startup_timeout_seconds", 120))
        self.action_timeout = int(config.get("action_timeout_seconds", 15)) * 1000
        self.headless = bool(config.get("headless", True))
        environment = config.get("environment", {})
        if not isinstance(environment, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in environment.items()
        ):
            raise ValidationError("pipeline.browser.environment must contain string values")
        self.environment = environment
        self.screenshot_mode = str(config.get("screenshots", "on_failure"))
        if self.screenshot_mode not in {"on_failure", "always", "never"}:
            raise ValidationError(
                "pipeline.browser.screenshots must be on_failure, always, or never"
            )
        self.fail_on_page_error = bool(config.get("fail_on_page_error", True))

    def verify(self, task_id: str, worktree: Path) -> dict[str, Any]:
        started = time.monotonic()
        output_dir = self.store.run_dir(task_id) / "browser"
        output_dir.mkdir(parents=True, exist_ok=True)
        server_log = output_dir / "server.log"
        server: subprocess.Popen[bytes] | None = None
        scenario_results: list[dict[str, Any]] = []
        error = ""
        try:
            sync_playwright = self._load_playwright()
            server = self._start_server(worktree, server_log)
            self._wait_for_server(server)
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch(headless=self.headless)
                except Exception as exc:
                    raise PipelineError(
                        "Chromium could not start. Run 'python -m playwright install chromium': "
                        f"{exc}",
                        code="browser_launch_failed",
                    ) from exc
                try:
                    context = browser.new_context()
                    for index, scenario in enumerate(self.scenarios, start=1):
                        result = self._run_scenario(context, scenario, index, output_dir)
                        scenario_results.append(result)
                        if result["status"] == "failed":
                            break
                finally:
                    browser.close()
        except Exception as exc:
            error = str(exc)
        finally:
            if server is not None:
                self._stop_server(server)

        passed = not error and all(item["status"] == "passed" for item in scenario_results)
        if not passed and not error and scenario_results:
            error = str(scenario_results[-1].get("message", "Browser scenario failed"))
        log_text = self._read_tail(server_log)
        return {
            "name": "browser",
            "command": self.start_command,
            "status": "passed" if passed else "failed",
            "exit_code": 0 if passed else 1,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": self.store.redact(log_text),
            "stderr": self.store.redact(error),
            "base_url": self.base_url,
            "scenarios": scenario_results,
            "server_log": str(server_log),
        }

    def _run_scenario(
        self,
        context: Any,
        scenario: Any,
        index: int,
        output_dir: Path,
    ) -> dict[str, Any]:
        if not isinstance(scenario, dict):
            raise ValidationError("Each pipeline.browser scenario must be an object")
        name = str(scenario.get("name") or f"scenario-{index}")
        path = str(scenario.get("path", "/"))
        actions = scenario.get("actions", [])
        if not isinstance(actions, list):
            raise ValidationError(f"Browser scenario '{name}' actions must be an array")
        page = context.new_page()
        fill_selectors = [
            str(action["selector"])
            for action in actions
            if isinstance(action, dict)
            and action.get("action") == "fill"
            and isinstance(action.get("selector"), str)
        ]
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        action_results: list[dict[str, Any]] = []
        screenshot: str | None = None
        try:
            page.goto(self._url(path), wait_until="domcontentloaded", timeout=self.action_timeout)
            for action in actions:
                action_results.append(self._run_action(page, name, action))
            if self.fail_on_page_error and page_errors:
                raise PipelineError(
                    f"Browser scenario '{name}' raised page errors: {page_errors[-1]}",
                    code="browser_page_error",
                )
            if self.screenshot_mode == "always":
                screenshot = self._screenshot(
                    page, output_dir, index, name, fill_selectors
                )
            return {
                "name": name,
                "path": path,
                "status": "passed",
                "actions": action_results,
                "page_errors": page_errors,
                "screenshot": screenshot,
            }
        except Exception as exc:
            if self.screenshot_mode in {"always", "on_failure"}:
                try:
                    screenshot = self._screenshot(
                        page, output_dir, index, name, fill_selectors
                    )
                except Exception:
                    screenshot = None
            return {
                "name": name,
                "path": path,
                "status": "failed",
                "actions": action_results,
                "page_errors": page_errors,
                "screenshot": screenshot,
                "message": str(exc),
            }
        finally:
            page.close()

    def _run_action(self, page: Any, scenario_name: str, action: Any) -> dict[str, Any]:
        if not isinstance(action, dict):
            raise ValidationError(f"Browser scenario '{scenario_name}' action must be an object")
        kind = str(action.get("action", ""))
        if kind not in self.ACTIONS:
            raise ValidationError(
                f"Browser scenario '{scenario_name}' has unsupported action: {kind}"
            )
        selector = action.get("selector")
        if kind in {"click", "fill", "press", "wait_for", "expect_visible", "expect_text"}:
            if not isinstance(selector, str) or not selector.strip():
                raise ValidationError(
                    f"Browser action '{kind}' in '{scenario_name}' requires selector"
                )
        if kind == "click":
            page.locator(selector).click(timeout=self.action_timeout)
        elif kind == "fill":
            page.locator(selector).fill(str(action.get("value", "")), timeout=self.action_timeout)
        elif kind == "press":
            key = self._action_text(action, "key", kind, scenario_name)
            page.locator(selector).press(key, timeout=self.action_timeout)
        elif kind == "goto":
            page.goto(
                self._url(self._action_text(action, "path", kind, scenario_name)),
                wait_until="domcontentloaded",
                timeout=self.action_timeout,
            )
        elif kind in {"wait_for", "expect_visible"}:
            page.locator(selector).wait_for(state="visible", timeout=self.action_timeout)
        elif kind == "expect_text":
            expected = self._action_text(action, "text", kind, scenario_name)
            actual = page.locator(selector).text_content(timeout=self.action_timeout) or ""
            if expected not in actual:
                raise PipelineError(
                    f"Expected text not found in {selector!r}: {expected!r}",
                    code="browser_assertion_failed",
                )
        elif kind == "expect_url":
            expected = self._action_text(action, "value", kind, scenario_name)
            if expected not in page.url:
                raise PipelineError(
                    f"Expected URL to contain {expected!r}, got {page.url!r}",
                    code="browser_assertion_failed",
                )
        record = {"action": kind, "status": "passed"}
        if selector:
            record["selector"] = selector
        return record

    def _start_server(self, worktree: Path, log_path: Path) -> subprocess.Popen[bytes]:
        if self._server_is_ready():
            raise PipelineError(
                f"Browser base URL is already in use before start_command: {self.base_url}",
                code="browser_server_port_in_use",
                retryable=False,
            )
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        log_handle = log_path.open("wb")
        try:
            process = subprocess.Popen(
                self.start_command,
                cwd=worktree,
                shell=True,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env={**os.environ, **self.environment},
                creationflags=flags,
                start_new_session=os.name != "nt",
            )
        finally:
            log_handle.close()
        return process

    def _wait_for_server(self, server: subprocess.Popen[bytes]) -> None:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise PipelineError(
                    f"Browser web server exited before {self.base_url} became ready",
                    code="browser_server_failed",
                )
            if self._server_is_ready():
                return
            time.sleep(0.25)
        raise PipelineError(
            f"Browser web server did not become ready within {self.startup_timeout}s: "
            f"{self.base_url}",
            code="browser_server_timeout",
        )

    def _server_is_ready(self) -> bool:
        try:
            urllib.request.urlopen(self.base_url, timeout=1).close()
            return True
        except urllib.error.HTTPError:
            return True
        except (urllib.error.URLError, TimeoutError):
            return False

    @staticmethod
    def _stop_server(server: subprocess.Popen[bytes]) -> None:
        if server.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(server.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
            return
        try:
            os.killpg(server.pid, signal.SIGTERM)
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(server.pid, signal.SIGKILL)

    @staticmethod
    def _load_playwright() -> Any:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise PipelineError(
                "Browser verification requires the optional dependency. Install with "
                "'pip install -e .[browser]' and run "
                "'python -m playwright install chromium'.",
                code="browser_provider_not_installed",
            ) from exc
        return sync_playwright

    def _url(self, path: str) -> str:
        parsed = urllib.parse.urlparse(path)
        if parsed.scheme or parsed.netloc:
            raise ValidationError("Browser scenario paths must stay on pipeline.browser.base_url")
        return urllib.parse.urljoin(f"{self.base_url}/", path.lstrip("/"))

    @staticmethod
    def _required_text(config: dict[str, Any], key: str) -> str:
        value = config.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"pipeline.browser.{key} is required when browser is enabled")
        return value.strip()

    @staticmethod
    def _action_text(
        action: dict[str, Any], key: str, kind: str, scenario_name: str
    ) -> str:
        value = action.get(key)
        if not isinstance(value, str) or not value:
            raise ValidationError(
                f"Browser action '{kind}' in '{scenario_name}' requires {key}"
            )
        return value

    @staticmethod
    def _screenshot(
        page: Any,
        output_dir: Path,
        index: int,
        name: str,
        fill_selectors: list[str],
    ) -> str:
        for selector in fill_selectors:
            try:
                page.locator(selector).fill("••••")
            except Exception:
                pass
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-") or "scenario"
        path = output_dir / f"{index:02d}-{safe_name}.png"
        page.screenshot(path=str(path), full_page=True)
        return str(path)

    @staticmethod
    def _read_tail(path: Path) -> str:
        if not path.is_file():
            return ""
        return path.read_bytes()[-10_000:].decode("utf-8", errors="replace")
