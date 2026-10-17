"""
BIT专属策略 - 时间工具模块
提供时间来源标记、时间数据结构和时间优先级处理

文件路径: @DMR_binance_Version_V14_A1/bit/common/time_utils.py
版本: v1.0.0
创建日期: 2025-11-06
"""

from enum import Enum
from typing import Optional, Dict, Any
from datetime import datetime
import time


class TimeSource(Enum):
    """时间来源枚举"""
    ACTUAL_TRADE_TIME = 'actual_trade_time'      # 实际成交时间（来自币安API）
    PENDING_ORDER_TIME = 'pending_order_time'     # 挂单时间
    SYSTEM_TIME = 'system_time'                   # 系统时间
    STRATEGY_CYCLE_TIME = 'strategy_cycle_time'   # 策略周期时间（用于接管持仓）
    UNKNOWN = 'unknown'                           # 未知来源


class TimeInfo:
    """
    时间信息数据结构
    
    用于封装时间及其来源，支持时间优先级处理
    """
    
    def __init__(self, 
                 timestamp: float,
                 source: TimeSource,
                 raw_data: Optional[Dict[str, Any]] = None,
                 description: str = ""):
        """
        初始化时间信息
        
        Args:
            timestamp: Unix时间戳（秒）
            source: 时间来源
            raw_data: 原始数据（如订单信息）
            description: 描述信息
        """
        self.timestamp = timestamp
        self.source = source
        self.raw_data = raw_data or {}
        self.description = description
        self.created_at = time.time()
    
    @property
    def datetime(self) -> datetime:
        """获取datetime对象"""
        return datetime.fromtimestamp(self.timestamp)
    
    @property
    def iso_format(self) -> str:
        """获取ISO格式字符串"""
        return self.datetime.isoformat()
    
    @property
    def priority(self) -> int:
        """
        获取时间来源的优先级（数值越小优先级越高）
        
        优先级顺序：
        1. ACTUAL_TRADE_TIME (实际成交时间) - 最高优先级
        2. PENDING_ORDER_TIME (挂单时间)
        3. STRATEGY_CYCLE_TIME (策略周期时间)
        4. SYSTEM_TIME (系统时间)
        5. UNKNOWN (未知来源) - 最低优先级
        """
        priority_map = {
            TimeSource.ACTUAL_TRADE_TIME: 1,
            TimeSource.PENDING_ORDER_TIME: 2,
            TimeSource.STRATEGY_CYCLE_TIME: 3,
            TimeSource.SYSTEM_TIME: 4,
            TimeSource.UNKNOWN: 5
        }
        return priority_map.get(self.source, 999)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'timestamp': self.timestamp,
            'source': self.source.value,
            'datetime': self.iso_format,
            'description': self.description,
            'priority': self.priority,
            'created_at': self.created_at
        }
    
    def __str__(self) -> str:
        """字符串表示"""
        return f"TimeInfo(time={self.iso_format}, source={self.source.value}, priority={self.priority})"
    
    def __repr__(self) -> str:
        """详细表示"""
        return self.__str__()
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TimeInfo':
        """从字典创建TimeInfo对象"""
        source_str = data.get('source', 'unknown')
        try:
            source = TimeSource(source_str)
        except ValueError:
            source = TimeSource.UNKNOWN
        
        return cls(
            timestamp=data.get('timestamp', time.time()),
            source=source,
            raw_data=data.get('raw_data'),
            description=data.get('description', '')
        )


class TimePriorityManager:
    """
    时间优先级管理器
    
    用于在多个时间来源中选择最优时间
    """
    
    @staticmethod
    def select_best_time(*time_infos: TimeInfo) -> TimeInfo:
        """
        从多个TimeInfo中选择优先级最高的时间
        
        Args:
            *time_infos: 多个TimeInfo对象
            
        Returns:
            优先级最高的TimeInfo对象
        """
        if not time_infos:
            # 如果没有提供时间，返回当前系统时间
            return TimeInfo(
                timestamp=time.time(),
                source=TimeSource.SYSTEM_TIME,
                description="默认系统时间（无其他时间源可用）"
            )
        
        # 过滤掉None
        valid_times = [t for t in time_infos if t is not None]
        
        if not valid_times:
            return TimeInfo(
                timestamp=time.time(),
                source=TimeSource.SYSTEM_TIME,
                description="默认系统时间（所有时间源均无效）"
            )
        
        # 按优先级排序，选择优先级最高的
        return min(valid_times, key=lambda t: t.priority)
    
    @staticmethod
    def create_from_binance_order(order: Dict[str, Any]) -> Optional[TimeInfo]:
        """
        从币安订单响应创建TimeInfo
        
        优先使用 lastTradeTimestamp，其次使用 info['updateTime']
        
        Args:
            order: 币安订单响应对象
            
        Returns:
            TimeInfo对象，如果无法提取时间则返回None
        """
        if not order:
            return None
        
        # 方式1: 优先使用 lastTradeTimestamp (CCXT标准化字段)
        last_trade_timestamp = order.get('lastTradeTimestamp')
        if last_trade_timestamp:
            # lastTradeTimestamp 通常是毫秒，需要转换为秒
            timestamp = last_trade_timestamp / 1000
            return TimeInfo(
                timestamp=timestamp,
                source=TimeSource.ACTUAL_TRADE_TIME,
                raw_data={'order_id': order.get('id'), 'last_trade_timestamp': last_trade_timestamp},
                description=f"币安API实际成交时间 (lastTradeTimestamp: {last_trade_timestamp}ms)"
            )
        
        # 方式2: 使用 info['updateTime'] (币安原始字段)
        info = order.get('info', {})
        update_time = info.get('updateTime')
        if update_time:
            # updateTime 通常是毫秒
            timestamp = int(update_time) / 1000
            return TimeInfo(
                timestamp=timestamp,
                source=TimeSource.ACTUAL_TRADE_TIME,
                raw_data={'order_id': order.get('id'), 'update_time': update_time},
                description=f"币安API实际成交时间 (updateTime: {update_time}ms)"
            )
        
        # 方式3: 使用订单的 timestamp 字段作为挂单时间
        order_timestamp = order.get('timestamp')
        if order_timestamp:
            timestamp = order_timestamp / 1000
            return TimeInfo(
                timestamp=timestamp,
                source=TimeSource.PENDING_ORDER_TIME,
                raw_data={'order_id': order.get('id'), 'order_timestamp': order_timestamp},
                description=f"订单挂单时间 (timestamp: {order_timestamp}ms)"
            )
        
        return None
    
    @staticmethod
    def create_system_time(description: str = "") -> TimeInfo:
        """
        创建当前系统时间的TimeInfo
        
        Args:
            description: 描述信息
            
        Returns:
            TimeInfo对象
        """
        return TimeInfo(
            timestamp=time.time(),
            source=TimeSource.SYSTEM_TIME,
            description=description or "当前系统时间"
        )
    
    @staticmethod
    def create_strategy_cycle_time(description: str = "") -> TimeInfo:
        """
        创建策略周期时间的TimeInfo（用于持仓接管场景）
        
        Args:
            description: 描述信息
            
        Returns:
            TimeInfo对象
        """
        return TimeInfo(
            timestamp=time.time(),
            source=TimeSource.STRATEGY_CYCLE_TIME,
            description=description or "策略周期时间（持仓接管）"
        )


def parse_time_to_timestamp(time_value: Any) -> float:
    """
    将各种时间格式转换为Unix时间戳（秒）
    
    Args:
        time_value: 时间值（可以是时间戳、datetime对象、ISO字符串等）
        
    Returns:
        Unix时间戳（秒）
    """
    if isinstance(time_value, (int, float)):
        # 如果是数字，判断是秒还是毫秒
        if time_value > 1e10:  # 大于10位数，可能是毫秒
            return time_value / 1000
        return time_value
    
    if isinstance(time_value, datetime):
        return time_value.timestamp()
    
    if isinstance(time_value, str):
        try:
            # 尝试解析ISO格式
            dt = datetime.fromisoformat(time_value.replace('Z', '+00:00'))
            return dt.timestamp()
        except:
            pass
        
        try:
            # 尝试解析为数字
            return float(time_value)
        except:
            pass
    
    # 默认返回当前时间
    return time.time()


def format_timestamp(timestamp: float, format_str: str = '%Y-%m-%d %H:%M:%S') -> str:
    """
    格式化时间戳
    
    Args:
        timestamp: Unix时间戳（秒）
        format_str: 格式字符串
        
    Returns:
        格式化的时间字符串
    """
    return datetime.fromtimestamp(timestamp).strftime(format_str)
