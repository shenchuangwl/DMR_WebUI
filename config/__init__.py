# config 包初始化文件
#
# 在导入任何配置模块之前加载项目根目录的 .env。
# README / SECURITY.md 一直指导用户 `cp .env.example .env`，
# 但此前项目没有任何代码读取该文件（也没有 python-dotenv 依赖），
# 按文档配置的用户拿到的是空凭证。
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from utils.env_loader import ensure_loaded as _ensure_loaded
    _ensure_loaded()
except Exception as _e:  # 加载 .env 失败不应阻断导入，凭证仍可由环境变量提供
    print(f"[config] 加载 .env 失败(已忽略，可改用环境变量): {_e}")
