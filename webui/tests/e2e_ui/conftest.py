"""回环界面测试（tests/e2e_ui）的公共设施：真实三进程（deploy/dev_start.sh，临时端口、临时 var、回放行情）+ 本机 Chrome（Playwright，headless）。

只能经 ``./run_tests.sh ui`` 运行（控制台环境的解释器，不在禁网包装下，只用回环）。整个目录共用一套已启动的部署（会话级夹具），
测试按文件、文件内顺序执行，会话结束时停掉全部并强杀漏网进程。截图存 ``docs/acceptance/<前缀>-*.png``。
页面元素的 ``.mark()`` 不进 DOM，所以一律按可见文字定位。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
WEBUI_ROOT = HERE.parents[1]
sys.path.insert(0, str(WEBUI_ROOT / "tests" / "e2e_local"))

from deploy_kit import KERNEL_PY, Stack, http_json, wait_until  # noqa: E402

CHROME = "/data/software/chrome/opt/google/chrome/chrome"
SHOTS = WEBUI_ROOT / "docs" / "acceptance"
OPERATOR = "uitester"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "ui: 回环界面测试（真实进程 + 本机 Chrome），只能经 ./run_tests.sh ui 运行")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """根 conftest 在 e2e 模式下会跳过 tests/e2e_local 以外的用例；ui 档复用同一开关，这里把本目录用例的跳过标记撤掉。"""
    for item in items:
        if HERE in Path(str(item.fspath)).parents:
            item.own_markers[:] = [m for m in item.own_markers if not (m.name == "skip" and "e2e 模式" in str(m.kwargs.get("reason", "")))]
            if os.environ.get("DMR_WEBUI_UI") != "1":
                item.add_marker(pytest.mark.skip(reason="界面测试只能经 ./run_tests.sh ui 运行"))


LIST_PATH = "/api/v1/onlycoin/live"
FIXTURE = WEBUI_ROOT / "tests" / "fixtures" / "onlycoin_live_sample.json"
# 假名单里让 BTCUSDT、ETHUSDT、SOLUSDT、BNBUSDT 入选（替身回放行情里有这两个合约），其余沿用录制样例的成员
LIST_OVERRIDES = {"TIAUSDT": ("BTCUSDT", "LONG"), "APRUSDT": ("ETHUSDT", "SHORT"),
                  "MUBARAKUSDT": ("SOLUSDT", "LONG"), "LUMIAUSDT": ("BNBUSDT", "SHORT")}


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def fake_list_body() -> bytes:
    """录制样例，时间改成相对当前：有效期 6 小时后，成员入选时间为一小时前。"""
    body = json.loads(FIXTURE.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    body["valid_until_utc"] = _iso(now + timedelta(hours=6))
    body["server_time"] = body["updated_at_utc"] = body["last_committed_at"] = _iso(now)
    body["business_date"] = now.strftime("%Y-%m-%d")
    for member in body["onlycoin"]:
        if member["symbol"] in LIST_OVERRIDES:
            member["symbol"], member["direction"] = LIST_OVERRIDES[member["symbol"]]
        member["first_selected_at"] = _iso(now - timedelta(hours=1))
        member["expires_at_utc"] = _iso(now + timedelta(hours=6))
    for key in ("long", "short"):
        if isinstance(body.get(key), list):
            body[key] = []
    body["symbols"] = [m["symbol"] for m in body["onlycoin"]]
    return json.dumps(body).encode()


class FakeListServer:
    """只绑 127.0.0.1 临时端口的假名单服务：GET /api/v1/onlycoin/live。"""

    def __init__(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path != LIST_PATH:
                    self.send_response(404)
                    self.end_headers()
                    return
                payload = fake_list_body()
                outer.hits += 1
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # noqa: D401
                pass

        self.hits = 0
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}{LIST_PATH}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class Deployment:
    def __init__(self, stack: Stack, password: str) -> None:
        self.stack = stack
        self.password = password
        self.console = stack.url("console")
        self.kernel = stack.url("kernel")
        self.standin = stack.url("standin")

    @property
    def local_token(self) -> str:
        return (self.stack.var / "kernel" / "local.token").read_text().strip()

    def kernel_get(self, path: str):
        return http_json("GET", f"{self.kernel}/api/v1{path}", token=self.local_token)

    def _items(self, path: str) -> list[dict]:
        status, body = self.kernel_get(path)
        assert status == 200, body
        return body.get("items", body.get("positions", [])) if isinstance(body, dict) else body

    def positions(self) -> list[dict]:
        """登记持仓（托管中等）。"""
        return self._items("/positions")

    def external(self) -> list[dict]:
        """待确认与外部（已忽略）的持仓。"""
        return self._items("/positions/external")


@pytest.fixture(scope="session")
def deployment(tmp_path_factory):
    fake_list = FakeListServer()
    stack = Stack(tmp_path_factory.mktemp("ui"), DMR_LIST_URL=fake_list.url)
    try:
        started = stack.run("dev_start.sh", timeout=240)
        assert started.returncode == 0, started.stdout + started.stderr
        made = subprocess.run([str(KERNEL_PY), "-m", "dmr_kernel.cli", "create-operator", "--username", OPERATOR],
                              env=stack.env, cwd=WEBUI_ROOT, capture_output=True, text=True, timeout=60)
        assert made.returncode == 0, made.stdout + made.stderr
        password = (stack.var / "operators" / f"{OPERATOR}.password").read_text().strip()
        dep = Deployment(stack, password)
        dep.fake_list = fake_list
        yield dep
    finally:
        stack.shutdown()
        fake_list.close()
        assert not stack.leaked, f"测试结束后仍有残留进程：{stack.leaked}"


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        chrome = pw.chromium.launch(executable_path=CHROME, headless=True,
                                    args=["--no-sandbox", "--proxy-server=direct://", "--proxy-bypass-list=*"])
        yield chrome
        chrome.close()


@pytest.fixture(scope="session")
def page(browser, deployment):
    context = browser.new_context(viewport={"width": 1400, "height": 1000}, locale="zh-CN")
    context.set_default_timeout(15000)
    page = context.new_page()
    page.on("pageerror", lambda exc: print("pageerror:", exc))
    yield page
    context.close()


class Shots:
    def __init__(self, page) -> None:
        self.page = page

    def __call__(self, name: str) -> None:
        SHOTS.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


@pytest.fixture(scope="session")
def shot(page):
    return Shots(page)
