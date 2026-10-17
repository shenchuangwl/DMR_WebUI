"""控制台的结构规则（静态扫描源码，不起任何服务）。

1. 不导入内核、替身交易所与任何 DMR 模块（控制台只经 HTTP/WebSocket 调内核）；不导入与只读控制台无关的危险模块。
2. 只有登记过的界面模块才导入 nicegui；状态条、持仓分组、客户端、会话这些纯逻辑模块保持可以脱离 NiceGUI 单测。
3. 界面只在函数里建，模块顶层不碰 ``ui``：在顶层建界面会让 NiceGUI 进入脚本模式，之后 ``ui.page`` 与 ``ui.run`` 互相冲突。
4. 内核令牌与会话只在服务端内存：不用 ``app.storage.user/general/tab/client``，浏览器会话编号只在 ``app.py`` 里读一处。
5. 内核给的文字永远按纯文本渲染：不用 ``ui.html/ui.markdown/ui.add_body_html/run_javascript``、``cellRenderer``、``html_columns``；
   ``add_head_html`` 只放常量样式。
6. 源码里的字符串（文档字符串除外）不出现 ``.env``、``BINANCE_`` 前缀、币安地址、``0.0.0.0``、名单接口；写死的 URL 只能指向回环。
7. 只有 ``secret.py`` 与 ``__main__.py`` 会写文件系统；``ui.run`` 只在 ``__main__.py`` 里调用一次，参数全部来自 ``run_kwargs``。

检查器本身也有正反用例（防止它失效而静默通过）。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

WEBUI = Path(__file__).resolve().parents[2]

DMR_TOP_LEVEL = {
    "common", "config", "bit", "strategy", "execution", "utils", "data", "risk_management", "monitoring", "src",
    "analyze_markets", "backtest_dmr_quadrant", "dmr_analysis", "dmr_quadrant_analyzer", "main",
    "first_short_term_main", "first_long_term_main", "short_term_main", "long_term_main",
}
OTHER_PACKAGES = {"dmr_kernel", "dmr_standin"}
UNRELATED_IMPORTS = {"subprocess", "pickle", "marshal", "shelve", "ctypes", "webbrowser", "smtplib"}

# 允许导入 nicegui 的界面模块：新增界面模块时要在这里明确登记
UI_MODULES = {
    "dmr_console.app", "dmr_console.__main__",
    "dmr_console.pages.login", "dmr_console.pages.oplog", "dmr_console.pages.overview", "dmr_console.pages.positions",
    "dmr_console.pages.shell", "dmr_console.pages.devpage", "dmr_console.pages.settings", "dmr_console.pages.watchlist",
    "dmr_console.pages.trades", "dmr_console.pages.stats", "dmr_console.pages.kline",
    "dmr_console.components.gridslot", "dmr_console.components.statusbar", "dmr_console.components.opdialogs",
}
ID_READER = "dmr_console.app"  # 唯一读 app.storage.browser 的模块
RUN_MODULE = "dmr_console.__main__"
FS_WRITERS = {"dmr_console.secret", "dmr_console.__main__"}

SERVER_STORAGE = {"user", "general", "tab", "client"}
UI_NAMES = {"ui", "nicegui_app"}
HTML_SINK_CALLS = {"html", "markdown", "mermaid", "add_body_html", "run_javascript"}
HTML_SINK_WORDS = {"html_columns", "cellRenderer", ":cellRenderer", "sanitize"}
FORBIDDEN_FRAGMENTS = (".env", "BINANCE_", "binance.com", "fapi", "dapi", "listenKey", "0.0.0.0", "18080", "onlycoin",
                       "api_key", "apiKey")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
URL_HOST = re.compile(r"(?:https?|wss?)://([^/\s:：，。（）]+)")
FS_WRITE_ATTRS = {"write_text", "write_bytes", "mkdir", "unlink", "rmdir", "chmod", "fchmod"}
FS_WRITE_OS_FUNCS = {"open", "fdopen", "replace", "rename", "remove", "unlink", "mkdir", "makedirs", "chmod", "fchmod"}


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


class _ImportTime(ast.NodeVisitor):
    """只看模块导入时就会执行的代码：模块体与类体；函数体、lambda 体与注解（有 __future__ 注解时是字符串）都不算。"""

    def __init__(self) -> None:
        self.hits: list[ast.Name] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in [*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)]:
            self.visit(default)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return None

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in UI_NAMES:
            self.hits.append(node)


def _imported_modules(tree: ast.AST) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.append((node.module, node.lineno))
    return out


def import_problems(label: str, text: str, *, unrelated: bool = True) -> list[str]:
    problems = []
    for name, lineno in _imported_modules(ast.parse(text)):
        top = name.split(".")[0]
        if top in OTHER_PACKAGES or top in DMR_TOP_LEVEL:
            problems.append(f"{label}:{lineno}: 导入了 {name}（控制台不得导入内核、替身交易所或 DMR 模块）")
        if unrelated and top in UNRELATED_IMPORTS:
            problems.append(f"{label}:{lineno}: 导入了 {name}（只读控制台用不到）")
    return problems


def _is_attr_chain_root(node: ast.AST, name: str) -> bool:
    while isinstance(node, ast.Attribute):
        node = node.value
    return isinstance(node, ast.Name) and node.id == name


def scan(sources: dict[str, str]) -> list[str]:
    """``sources``：模块名 → 源码。返回所有违规，格式 ``模块:行号: 说明``。"""
    problems: list[str] = []
    run_calls = 0
    for module, text in sorted(sources.items()):
        tree = ast.parse(text)
        docstrings = _docstring_ids(tree)
        problems += import_problems(module, text)

        # 2. 只有登记的界面模块才导入 nicegui
        for name, lineno in _imported_modules(tree):
            if name.split(".")[0] == "nicegui" and module not in UI_MODULES:
                problems.append(f"{module}:{lineno}: 纯逻辑模块不得导入 nicegui（界面模块要先在 UI_MODULES 里登记）")

        # 3. 模块顶层不碰 ui
        visitor = _ImportTime()
        visitor.visit(tree)
        problems += [f"{module}:{n.lineno}: 模块顶层使用了 {n.id}（界面只能在函数里建）" for n in visitor.hits]

        for node in ast.walk(tree):
            # 4. 令牌与会话只在服务端内存
            if isinstance(node, ast.Attribute):
                inner = node.value
                if isinstance(inner, ast.Attribute) and inner.attr == "storage":
                    if node.attr in SERVER_STORAGE:
                        problems.append(f"{module}:{node.lineno}: 用了 storage.{node.attr}（令牌与会话只放服务端内存）")
                    if node.attr == "browser" and module != ID_READER:
                        problems.append(f"{module}:{node.lineno}: storage.browser 只允许在 {ID_READER} 里读")
            # 5. HTML 汇点
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                attr = node.func.attr
                if attr in HTML_SINK_CALLS and (attr == "run_javascript" or _is_attr_chain_root(node.func, "ui")):
                    problems.append(f"{module}:{node.lineno}: 调用了 {attr}（内核给的文字只能按纯文本渲染）")
                if attr == "add_head_html":
                    arg = node.args[0] if node.args else None
                    if not (isinstance(arg, ast.Name) and arg.id == "STYLE_TAG"):
                        problems.append(f"{module}:{node.lineno}: add_head_html 只能放常量 STYLE_TAG")
            if isinstance(node, ast.Name) and node.id in HTML_SINK_WORDS:
                problems.append(f"{module}:{node.lineno}: 用了 {node.id}")
            if isinstance(node, ast.keyword) and node.arg in HTML_SINK_WORDS:
                problems.append(f"{module}:{getattr(node.value, 'lineno', 0)}: 用了参数 {node.arg}")
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                value = node.value
                if value in HTML_SINK_WORDS:
                    problems.append(f"{module}:{node.lineno}: 用了 {value}")
                # 6. 字符串
                for fragment in FORBIDDEN_FRAGMENTS:
                    if fragment in value:
                        problems.append(f"{module}:{node.lineno}: 字符串里出现 {fragment!r}")
                for host in URL_HOST.findall(value):
                    if host not in LOOPBACK_HOSTS and "%" not in host and "{" not in host:
                        problems.append(f"{module}:{node.lineno}: 写死的 URL 指向非回环地址 {host!r}")
            # 7. 写文件系统与 ui.run
            if isinstance(node, ast.Call):
                func = node.func
                writes = False
                if isinstance(func, ast.Attribute):
                    writes = func.attr in FS_WRITE_ATTRS or (
                        isinstance(func.value, ast.Name) and func.value.id == "os" and func.attr in FS_WRITE_OS_FUNCS)
                    writes = writes or func.attr.endswith("FileHandler")
                elif isinstance(func, ast.Name):
                    writes = func.id.endswith("FileHandler")
                    if func.id == "open":
                        mode = node.args[1] if len(node.args) > 1 else next(
                            (k.value for k in node.keywords if k.arg == "mode"), None)
                        writes = writes or (isinstance(mode, ast.Constant) and isinstance(mode.value, str)
                                            and any(c in mode.value for c in "wax+"))
                if writes and module not in FS_WRITERS:
                    problems.append(f"{module}:{node.lineno}: 写文件系统只允许在 {sorted(FS_WRITERS)} 里")
                if isinstance(func, ast.Attribute) and func.attr == "run" and _is_attr_chain_root(func, "ui"):
                    run_calls += 1
                    if module != RUN_MODULE:
                        problems.append(f"{module}:{node.lineno}: ui.run 只允许在 {RUN_MODULE} 里调用")
                    elif node.args or len(node.keywords) != 1 or node.keywords[0].arg is not None or not (
                            isinstance(node.keywords[0].value, ast.Call)
                            and isinstance(node.keywords[0].value.func, ast.Name)
                            and node.keywords[0].value.func.id == "run_kwargs"):
                        problems.append(f"{module}:{node.lineno}: ui.run 的参数必须只有 **run_kwargs(...)")
    if RUN_MODULE in sources and run_calls != 1:
        problems.append(f"{RUN_MODULE}: ui.run 应当恰好调用一次，实际 {run_calls} 次")
    return problems


def collect(root: Path, package: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        parts = [package, *path.relative_to(root).with_suffix("").parts]
        if parts[-1] == "__init__":
            parts = parts[:-1]
        out[".".join(parts)] = path.read_text(encoding="utf-8")
    return out


# ------------------------------------------------------------------ 真实代码

def test_the_real_console_code_obeys_the_structure_rules():
    sources = collect(WEBUI / "dmr_console", "dmr_console")
    assert "dmr_console.pages.shell" in sources and "dmr_console.__main__" in sources  # 扫到了东西
    assert scan(sources) == [], "\n".join(scan(sources))


def test_every_registered_ui_module_exists_and_really_uses_nicegui():
    sources = collect(WEBUI / "dmr_console", "dmr_console")
    assert UI_MODULES <= set(sources)
    for module in UI_MODULES:
        assert any(name.split(".")[0] == "nicegui" for name, _ in _imported_modules(ast.parse(sources[module]))), module


def test_the_pure_logic_modules_exist_and_stay_free_of_nicegui():
    sources = collect(WEBUI / "dmr_console", "dmr_console")
    pure = {"state", "views", "fmt", "labels", "link", "client", "secret", "settings", "session", "service", "live",
            "events", "clock", "components.grids", "components.theme", "operations", "actions", "devtools"}
    for name in pure:
        module = f"dmr_console.{name}"
        assert module in sources, module
        assert not any(n.split(".")[0] == "nicegui" for n, _ in _imported_modules(ast.parse(sources[module]))), module


def test_the_console_tests_do_not_import_the_kernel_or_dmr_modules():
    problems = []
    for path in sorted((WEBUI / "tests" / "console").glob("*.py")):
        problems += import_problems(path.name, path.read_text(encoding="utf-8"), unrelated=False)
    assert problems == []


# ------------------------------------------------------------------ 检查器自检

def check(module: str, text: str) -> list[str]:
    return scan({module: text})


def test_a_clean_ui_module_passes():
    clean = (
        '"""页面。"""\nfrom __future__ import annotations\n\nfrom nicegui import ui\n\n'
        'def register(console):\n    @ui.page("/x")\n    def page():\n        ui.label("x")\n'
        '        ui.add_head_html(STYLE_TAG)\n'
    )
    assert check("dmr_console.pages.login", clean) == []


def test_importing_the_kernel_the_standin_or_dmr_modules_is_flagged():
    for line in ("import dmr_kernel", "from dmr_kernel.core import types", "from dmr_standin import app",
                 "import common", "from bit.config import x", "import subprocess"):
        assert check("dmr_console.state", line + "\n"), line
    assert check("dmr_console.state", "from dmr_console import fmt\nimport httpx\n") == []


def test_nicegui_outside_the_registered_ui_modules_is_flagged():
    assert check("dmr_console.state", "from nicegui import ui\n")
    assert check("dmr_console.state", "def f():\n    import nicegui\n")
    assert check("dmr_console.pages.login", "from nicegui import ui\n") == []


def test_ui_at_module_level_is_flagged_but_inside_functions_and_annotations_is_not():
    bad = [
        "from nicegui import ui\nui.label('x')\n",
        "from nicegui import ui\nX = ui.column()\n",
        "from nicegui import ui\n@ui.page('/x')\ndef page():\n    pass\n",
        "from nicegui import ui\nclass A:\n    box = ui.row()\n",
        "from nicegui import ui\ndef f(a=ui.label):\n    pass\n",
    ]
    for text in bad:
        assert check("dmr_console.pages.login", text), text
    good = [
        "from __future__ import annotations\nfrom nicegui import ui\ndef f(a: ui.button) -> ui.element:\n    return ui.label('x')\n",
        "from nicegui import ui\nclass A:\n    def build(self):\n        self.b = ui.row()\n    x: ui.element\n",
        "from nicegui import ui\nF = lambda: ui.label('x')\n",
    ]
    for text in good:
        assert check("dmr_console.pages.login", text) == [], text


def test_server_side_or_tab_storage_is_flagged_and_the_browser_id_is_read_in_one_place_only():
    for attr in ("user", "general", "tab", "client"):
        assert check("dmr_console.app", f"from nicegui import app\ndef f():\n    return app.storage.{attr}\n"), attr
    read = "from nicegui import app\ndef f():\n    return app.storage.browser.get('id')\n"
    assert check("dmr_console.app", read) == []
    assert check("dmr_console.pages.login", read)
    assert check("dmr_console.app", "from nicegui import ui\ndef f():\n    return ui.context.client\n") == []


def test_html_sinks_and_unsafe_head_html_are_flagged():
    for body in ("ui.html(data)", "ui.markdown(data)", "ui.add_body_html(data)", "ui.run_javascript(data)",
                 "client.run_javascript(data)", "ui.add_head_html(data)", "ui.add_head_html('<style></style>')",
                 "ui.aggrid(o, html_columns=[1])", "x = {'cellRenderer': 'agGroupCellRenderer'}", "y = cellRenderer"):
        assert check("dmr_console.pages.login", f"from nicegui import ui\ndef f(data, client, o):\n    {body}\n"), body
    ok = "from nicegui import ui\ndef f():\n    ui.add_head_html(STYLE_TAG)\n    ui.label('<b>x</b>')\n"
    assert check("dmr_console.pages.login", ok) == []


def test_forbidden_strings_and_non_loopback_urls_are_flagged_but_docstrings_and_loopback_are_not():
    for literal in ("'.env'", "'BINANCE_API_KEY'", "'https://fapi.binance.com/x'", "'0.0.0.0'", "'api_key'",
                    "'http://127.0.0.1:18080/api/v1/onlycoin/live'", "'http://example.org/x'", "'wss://evil.example/ws'"):
        assert check("dmr_console.settings", f"X = {literal}\n"), literal
    fine = (
        '"""说明里可以提到 .env 和 http://example.org/x 与 0.0.0.0。"""\n'
        "A = 'http://127.0.0.1:18200/api/v1'\nB = 'ws://localhost:1/x'\nC = 'http://%s:%d'\nD = f'http://{1}:{2}'\n"
        "E = '直接在 Binance 处置'\n"
    )
    assert check("dmr_console.settings", fine) == []


def test_file_system_writes_are_only_allowed_in_the_secret_and_main_modules():
    writes = ("Path('a').write_text('x')", "Path('a').mkdir()", "os.open('a', 1)", "os.replace('a', 'b')",
              "open('a', 'w')", "open('a', mode='ab')", "logging.FileHandler('a')", "RotatingFileHandler('a')",
              "Path('a').unlink()")
    for call in writes:
        text = f"import os\nimport logging\nfrom pathlib import Path\ndef f():\n    {call}\n"
        assert check("dmr_console.state", text), call
        assert check("dmr_console.secret", text) == [], call
    reads = "def f():\n    open('a')\n    open('a', 'r')\n    Path('a').read_text()\n    d = {}\n    d.replace\n"
    assert check("dmr_console.state", reads) == []


def test_ui_run_is_only_called_from_main_exactly_once_and_only_with_run_kwargs():
    main_ok = "from nicegui import ui\ndef main():\n    ui.run(**run_kwargs(s, k))\n"
    assert check(RUN_MODULE, main_ok) == []
    assert check("dmr_console.app", main_ok)  # 别处不许调
    assert check(RUN_MODULE, "from nicegui import ui\ndef main():\n    ui.run(host='0.0.0.0')\n")  # 参数不是 run_kwargs
    assert check(RUN_MODULE, "from nicegui import ui\ndef main():\n    ui.run(port=1, **run_kwargs(s, k))\n")
    assert check(RUN_MODULE, "from nicegui import ui\ndef main():\n    ui.run(1)\n")
    assert check(RUN_MODULE, "from nicegui import ui\ndef main():\n    pass\n")  # 一次都没调
    twice = "from nicegui import ui\ndef main():\n    ui.run(**run_kwargs(s, k))\n    ui.run(**run_kwargs(s, k))\n"
    assert check(RUN_MODULE, twice)
