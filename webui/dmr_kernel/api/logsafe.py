"""日志里抹掉令牌与口令（设计约定：永远不记令牌、口令、密钥）。

事件流的握手把会话令牌放在查询串里（``WS /events?token=...``），uvicorn 的访问日志会把整条请求路径写出来；
装配起 uvicorn 时对它的日志器调用 ``install("uvicorn", "uvicorn.error", "uvicorn.access")``，令牌就不会落进日志。
``scrub`` 只抹值，不动别的字符；名字必须整词匹配（``mytoken=``、``token_count=`` 不动）。
"""
from __future__ import annotations

import logging
import re

__all__ = ["TokenScrubFilter", "install", "scrub"]

_PARAM = re.compile(r"(?<![A-Za-z0-9_])((?:access_)?token|password|secret)=[^&\s\"']+", re.IGNORECASE)
_BEARER = re.compile(r"(\bbearer\s+)[^\s\"']+", re.IGNORECASE)
MASK = "***"


def scrub(text: str) -> str:
    return _BEARER.sub(rf"\1{MASK}", _PARAM.sub(rf"\1={MASK}", text))


class TokenScrubFilter(logging.Filter):
    """把记录格式化成最终文字、抹掉令牌后再放行。永远不丢记录：格式化出错的原样放行。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # 日志参数写错也不能因此丢日志
            return True
        scrubbed = scrub(message)
        if scrubbed != message:
            record.msg, record.args = scrubbed, None
        return True


def install(*logger_names: str) -> None:
    """给这些日志器装上过滤器（重复安装无害）。"""
    for name in logger_names:
        logger = logging.getLogger(name)
        if not any(isinstance(existing, TokenScrubFilter) for existing in logger.filters):
            logger.addFilter(TokenScrubFilter())
