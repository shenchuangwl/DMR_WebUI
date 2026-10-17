"""对拍基础设施（只读）：读取 DMR 的源码文本与金标 CSV，按 ast 取出函数、方法或语句段，在隔离的字典命名空间里执行。

硬性规则（docs/设计约定.md 第 1 节第 3、4 条）：
- 只读打开 ``DMR_WebUI/`` 下的 ``.py`` 与 ``.csv``；越界路径、隐藏文件（``.env`` 等）、像密钥的文件名、
  运行期状态目录 ``data/``、配置模块目录、``webui/`` 自己一律拒绝，而且拒绝发生在碰文件系统之前。
- 不经 import 系统导入任何 DMR 模块：``compile`` 之后 ``exec`` 到新建的字典里；不写字节码，不在 DMR 目录产生任何文件。
- 编译时不继承调用方的 ``__future__`` 标志：原实现按它自己的语义执行（注解在定义时求值）。
"""
from __future__ import annotations

import ast
import hashlib
import sys
import textwrap
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import pandas as pd

WEBUI_ROOT = Path(__file__).resolve().parents[2]
DMR_ROOT = WEBUI_ROOT.parent

ALLOWED_SUFFIXES = frozenset({".py", ".csv"})
# 相对 DMR_WebUI 的禁入目录：运行期状态（data/runtime_state 在 data/ 下）、配置模块（可能含密钥）、虚拟环境、日志、webui 自己
DENIED_DIRS = ("data", "config", "bit/config", "venv", "logs", "webui")
_SENSITIVE_WORDS = ("secret", "credential", "password", "passwd", "token", "apikey", "api_key", "private_key")


# ---------------------------------------------------------------- 路径与读取


def _check_rel_parts(parts: tuple[str, ...], rel: str) -> None:
    if not parts:
        raise ValueError(f"空路径：{rel!r}")
    if any(p in ("..", ".") for p in parts):
        raise ValueError(f"路径不得含 . 或 ..：{rel!r}")
    if any(p.startswith(".") for p in parts):
        raise PermissionError(f"不打开隐藏文件或目录（.env 等）：{rel!r}")
    if "__pycache__" in parts:
        raise PermissionError(f"不打开字节码目录：{rel!r}")
    lowered = [p.lower() for p in parts]
    if any(word in p for p in lowered for word in _SENSITIVE_WORDS):
        raise PermissionError(f"文件名像密钥，不打开：{rel!r}")
    if PurePosixPath(parts[-1]).suffix not in ALLOWED_SUFFIXES:
        raise PermissionError(f"只允许读取 .py 与 .csv：{rel!r}")
    joined = "/".join(parts)
    for denied in DENIED_DIRS:
        if joined == denied or joined.startswith(denied + "/"):
            raise PermissionError(f"不打开 {denied}/ 下的文件：{rel!r}")


def dmr_path(rel: str) -> Path:
    """把相对 ``DMR_WebUI/`` 的路径换成绝对路径；不符合规则的先拒绝，再碰文件系统。"""
    if not isinstance(rel, str) or not rel or "\x00" in rel or "\\" in rel:
        raise ValueError(f"无效路径：{rel!r}")
    pure = PurePosixPath(rel)
    if pure.is_absolute():
        raise ValueError(f"只接受相对 DMR_WebUI 的路径：{rel!r}")
    if ".." in rel.split("/"):
        raise ValueError(f"路径不得越出 DMR_WebUI：{rel!r}")
    _check_rel_parts(pure.parts, rel)
    resolved = DMR_ROOT.joinpath(*pure.parts).resolve(strict=True)
    # 符号链接解析之后再查一遍：仍须在 DMR_WebUI 之内、webui 之外，且不落进禁入目录
    if not resolved.is_relative_to(DMR_ROOT) or resolved.is_relative_to(WEBUI_ROOT):
        raise PermissionError(f"路径解析到 DMR 源码之外：{rel!r} -> {resolved}")
    _check_rel_parts(resolved.relative_to(DMR_ROOT).parts, rel)
    if not resolved.is_file():
        raise ValueError(f"不是文件：{rel!r}")
    return resolved


def read_text(rel: str) -> str:
    """只读打开并按 UTF-8 解码（不做换行转换，行段哈希与磁盘字节一致）。"""
    with open(dmr_path(rel), "rb") as fh:
        return fh.read().decode("utf-8")


def physical_lines(text: str) -> list[str]:
    """按物理行切分（保留行尾）。只在 \\n、\\r\\n、\\r 处断行，与 Python 的行号一致（splitlines 还会在 \\x0c 等处断开）。"""
    out: list[str] = []
    buf = ""
    for piece in text.splitlines(keepends=True):
        buf += piece
        if buf.endswith(("\n", "\r")):
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


def segment(rel: str, start: int, end: int) -> str:
    """第 start 到 end 行（从 1 起，含两端）的原文。"""
    lines = physical_lines(read_text(rel))
    if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start or end > len(lines):
        raise ValueError(f"行段越界：{rel}:{start}-{end}（共 {len(lines)} 行）")
    return "".join(lines[start - 1:end])


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 按名字定位


def _find_node(rel: str, name: str, cls: str | None) -> ast.AST:
    tree = ast.parse(read_text(rel), filename=f"<dmr:{rel}>")
    body = tree.body
    if cls is not None:
        classes = [n for n in body if isinstance(n, ast.ClassDef) and n.name == cls]
        if len(classes) != 1:
            raise LookupError(f"{rel} 里找不到唯一的类 {cls}（找到 {len(classes)} 个）")
        body = classes[0].body
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    found = [n for n in body if isinstance(n, kinds) and n.name == name]
    if len(found) != 1:
        where = f"{cls}." if cls else ""
        raise LookupError(f"{rel} 里找不到唯一的定义 {where}{name}（找到 {len(found)} 个）")
    return found[0]


def locate_def(rel: str, name: str, cls: str | None = None) -> tuple[int, int]:
    """函数、方法或类定义的行范围 (起, 止)，从 1 起、含两端；有装饰器时从第一个装饰器起。"""
    node = _find_node(rel, name, cls)
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    return start, node.end_lineno


def extract_def(rel: str, name: str, cls: str | None = None) -> str:
    """定义的源码原文（整行，方法保留原缩进；交给 compile_def 时会去掉公共缩进）。"""
    return segment(rel, *locate_def(rel, name, cls))


# ---------------------------------------------------------------- 隔离执行


def _exec_isolated(code_text: str, globals_: dict[str, Any], label: str) -> dict[str, Any]:
    code = compile(code_text, f"<dmr:{label}>", "exec", flags=0, dont_inherit=True)
    ns = dict(globals_)
    ns.setdefault("__name__", f"dmr:{label}")
    exec(code, ns)  # noqa: S102 — 原实现的源码只在这个新字典里执行
    return ns


def compile_def(src: str, globals_: dict[str, Any], *, label: str) -> Any:
    """编译单个函数或类定义，返回定义出的对象。globals_ 只被复制，不被修改。"""
    code_text = textwrap.dedent(src)
    tree = ast.parse(code_text)
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    if len(tree.body) != 1 or not isinstance(tree.body[0], kinds):
        raise ValueError(f"compile_def 只接受单个函数或类定义：{label}")
    return _exec_isolated(code_text, globals_, label)[tree.body[0].name]


def compile_segment(rel: str, start: int, end: int, *, params: str = "", returns: str = "None",
                    globals_: dict[str, Any], label: str) -> Any:
    """把第 start–end 行的语句段包成函数 ``def _dmr_segment(<params>): <语句段>; return <returns>``。"""
    body = textwrap.dedent(segment(rel, start, end))
    if not body.endswith("\n"):
        body += "\n"
    code_text = f"def _dmr_segment({params}):\n" + textwrap.indent(body, "    ") + f"    return {returns}\n"
    return _exec_isolated(code_text, globals_, label)["_dmr_segment"]


# ---------------------------------------------------------------- 自检


def dmr_module_violations(names: Iterable[str]) -> list[str]:
    """names 里由 DMR 源码树（webui 之外）加载的模块名。"""
    bad: list[str] = []
    for name in sorted(set(names)):
        mod = sys.modules.get(name)
        if mod is None:
            continue
        files: list[str] = []
        file_ = getattr(mod, "__file__", None)
        if isinstance(file_, str):
            files.append(file_)
        path_ = getattr(mod, "__path__", None)
        if path_ is not None:
            try:
                files.extend(str(p) for p in path_)
            except TypeError:
                pass
        for f in files:
            try:
                resolved = Path(f).resolve()
            except (OSError, ValueError):
                continue
            if resolved.is_relative_to(DMR_ROOT) and not resolved.is_relative_to(WEBUI_ROOT):
                bad.append(name)
                break
    return bad


# ---------------------------------------------------------------- 金标 CSV


def load_csv(rel: str) -> pd.DataFrame:
    """只读打开 CSV（全部行，含最后一根未收盘的 K 线）。浮点按 round_trip 解析：与 Python 的 float() 逐位一致。"""
    path = dmr_path(rel)
    if path.suffix != ".csv":
        raise PermissionError(f"不是 CSV：{rel!r}")
    with open(path, "rb") as fh:
        return pd.read_csv(fh, float_precision="round_trip")
