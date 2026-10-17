"""本机时钟与交易所服务器时间的偏移（第 2 层）。

K 线是否收盘、该几点取数，都按**服务器时间**判断，不信本机时间。一次校准要三个数：发出请求时的本机时刻、服务器的读数、
收到应答时的本机时刻；假设服务器在往返的中点读数，偏移 = 服务器读数 − 往返中点，半毫秒向上取整。
之后「服务器现在」= 本机现在 + 偏移（本机时钟走，偏移不变），所以要定期重新校准（调度器每分钟一次）。

这里只保存偏移，不判断偏移多大算不能接受（那是 ``clock_skew_max_ms`` 与风控的事），也不发请求
（取样与「往返太慢的样本不要」在 ``CandleService.sync_server_time``）。只拒绝一眼看得出是脏数据的样本。
"""
from __future__ import annotations

from dmr_kernel.core.clock import Clock

# 往返超过这么久的样本太粗（偏移的误差可达往返的一半），不采用
MAX_ROUND_TRIP_MS = 2_000
# 偏移超过一天不可能是时钟误差（设错时区最多差 14 小时），多半是单位错了或读数是垃圾；
# 24 小时以内的大偏差照收：让调用方看到真实的偏差并告警，而不是静默校准失败
MAX_PLAUSIBLE_SKEW_MS = 86_400_000


def _require_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} 必须是整数毫秒：{value!r}")
    return value


class ServerClock:
    def __init__(self, clock: Clock):
        self._clock = clock
        self._skew_ms: int | None = None
        self._updated_ms: int | None = None

    @property
    def skew_ms(self) -> int | None:
        """服务器时间 − 本机时间（毫秒）；还没校准过是 None。"""
        return self._skew_ms

    @property
    def updated_ms(self) -> int | None:
        """最近一次校准时收到应答的本机时刻；还没校准过是 None。"""
        return self._updated_ms

    def update(self, server_ms: int, sent_ms: int, recv_ms: int) -> None:
        """用一次往返的三个读数更新偏移。样本不可信时抛 ``ValueError``/``TypeError``，已有的偏移保持不变。"""
        _require_int("server_ms", server_ms)
        _require_int("sent_ms", sent_ms)
        _require_int("recv_ms", recv_ms)
        if recv_ms < sent_ms:
            raise ValueError(f"本机时间在往返途中倒退了（发出 {sent_ms}，收到 {recv_ms}），这个样本不可用")
        if server_ms <= 0:
            raise ValueError(f"服务器时间必须是正整数毫秒：{server_ms}")
        skew = (2 * server_ms - sent_ms - recv_ms + 1) // 2  # server - (sent + recv) / 2，半毫秒向上取整
        if abs(skew) > MAX_PLAUSIBLE_SKEW_MS:
            raise ValueError(f"服务器时间与本机时间的偏移 {skew} 毫秒超出可信范围（±{MAX_PLAUSIBLE_SKEW_MS}），"
                             f"读数多半有误：服务器 {server_ms}，本机 {sent_ms}～{recv_ms}")
        self._skew_ms = skew
        self._updated_ms = recv_ms

    def now_server_ms(self) -> int:
        """服务器此刻的时间（毫秒）；还没校准过就退回本机时间。"""
        return self._clock.now_ms() + (self._skew_ms or 0)
