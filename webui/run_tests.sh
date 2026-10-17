#!/usr/bin/env bash
# 运行 webui 的测试。不要在 DMR_WebUI 根目录跑 pytest，也不要用 PATH 上的 python（那是 DMR_WebUI/venv）。
#   ./run_tests.sh kernel  [pytest 参数]   内核、算法对拍、替身交易所、集成、故障注入：内核环境 + 禁网包装
#   ./run_tests.sh console [pytest 参数]   控制台：控制台环境 + 禁网包装
#   ./run_tests.sh e2e     [pytest 参数]   回环端到端：真实进程、只用回环、回放行情（不在禁网包装下）
#   ./run_tests.sh ui      [pytest 参数]   回环界面测试：真实三进程 + 本机 Chrome（Playwright），控制台环境，只用回环（不在禁网包装下）
# 带了 tests/... 路径参数时只跑这些路径。
set -euo pipefail
cd "$(dirname "$0")"
unset VIRTUAL_ENV
export PYTHONDONTWRITEBYTECODE=1
suite="${1:-kernel}"
shift || true
has_path=0
for a in "$@"; do case "$a" in tests/*) has_path=1;; esac; done
# 禁网包装用本工程自己的环境启动（与被测进程同一套解释器），不依赖 PATH，也不用系统或 DMR_WebUI 的 Python
guard_kernel=(.venv-kernel/bin/python -I tests/guard/no_network_ip.py)
guard_console=(.venv-console/bin/python -I tests/guard/no_network_ip.py)
case "$suite" in
  kernel)
    if [ "$has_path" = 1 ]; then targets=(); else targets=(tests/guard tests/unit tests/parity tests/standin tests/integration tests/faults); fi
    exec "${guard_kernel[@]}" .venv-kernel/bin/python -m pytest "${targets[@]}" "$@" ;;
  console)
    if [ "$has_path" = 1 ]; then targets=(); else targets=(tests/console); fi
    exec "${guard_console[@]}" .venv-console/bin/python -m pytest "${targets[@]}" "$@" ;;
  e2e)
    if [ "$has_path" = 1 ]; then targets=(); else targets=(tests/e2e_local); fi
    DMR_WEBUI_E2E=1 exec .venv-kernel/bin/python -m pytest -m e2e "${targets[@]}" "$@" ;;
  ui)
    if [ "$has_path" = 1 ]; then targets=(); else targets=(tests/e2e_ui); fi
    DMR_WEBUI_E2E=1 DMR_WEBUI_UI=1 exec .venv-console/bin/python -m pytest "${targets[@]}" "$@" ;;
  *) echo "用法: $0 kernel|console|e2e|ui [pytest 参数]" >&2; exit 2 ;;
esac
