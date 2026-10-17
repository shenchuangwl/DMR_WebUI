# common 包初始化文件
# 使IDE能够正确识别导入路径
#
# ⚠️ 请勿删除本文件。它不是可有可无的空文件，删掉会导致策略无法启动。
#
# 原因：项目集成了多套量化策略子系统，bit/ 下存在同名的 bit/common 包（带 __init__.py，
# 属常规包）。以 `python bit/bit_short_term_main.py` 方式启动时，脚本所在目录 bit/ 会被
# 自动加入 sys.path。按 Python 导入规则，没有 __init__.py 的目录只作为命名空间包"候选"、
# 扫描继续，后续路径上的同名常规包会直接胜出——根 common 因而被 bit/common 顶掉，
# 报 ModuleNotFoundError: No module named 'common.balance_helper'。
#
# 本文件使根 common 成为常规包，与 config/ utils/ data/ strategy/ execution/ 保持一致，
# sys.path 中排在前面的项目根目录得以正常优先。
#
# 命名约定（两个 common 互不干扰，勿混用）：
#   common.*      → 本包，主系统与各策略共享（atomic_state / balance_helper / risk_state 等）
#   bit.common.*  → BIT 专属实现（trailing_stop_manager 等与本包同名但内容不同，须带 bit. 前缀）
