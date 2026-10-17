"""合约规则缓存（第 1 层）。

``refresh`` 经适配层的 ``load_instruments`` 整份换新；失败（``Unknown``、空清单）保持旧缓存并返回 False——
绝不把「读不到」当成「没有合约」。``lookup_list_code`` 是 F10 第 4 步的查表：拿名单里的原始合约代码精确匹配，
只认 PERPETUAL、USDT 计价、TRADING 状态的合约；不补 ``USDT``、不猜 ``1000`` 前缀、不改大小写，中文合约名照原样。
``get`` 则对任何已知合约都返回规则（持仓所在的合约即使已不在交易状态，也要能查到步长）。

适配层返回的 ``AuthConfigError`` 等程序或配置错误不在这里吞掉。
"""
from __future__ import annotations

from dmr_kernel.core.clock import Clock
from dmr_kernel.core.types import Instrument, Known
from dmr_kernel.exchange.adapter import ExchangeAdapter


class InstrumentRules:
    def __init__(self, clock: Clock):
        self._clock = clock
        self._by_symbol: dict[str, Instrument] = {}
        self.refreshed_ms: int | None = None

    async def refresh(self, adapter: ExchangeAdapter) -> bool:
        result = await adapter.load_instruments()
        if not isinstance(result, Known) or not result.value:
            return False  # 保持旧缓存与旧的刷新时间
        self._by_symbol = {i.symbol: i for i in result.value}
        self.refreshed_ms = self._clock.now_ms()
        return True

    def get(self, symbol: str) -> Instrument | None:
        return self._by_symbol.get(symbol)

    def lookup_list_code(self, code: str) -> Instrument | None:
        found = self._by_symbol.get(code)
        if found is not None and found.tradable:
            return found
        return None

    def all(self) -> list[Instrument]:
        return list(self._by_symbol.values())
