"""
BIT专属策略持仓接管管理器
用于检测和接管交易所的外部持仓

文件路径: @DMR_binance_Version_V14_A1/bit/common/position_claim_manager.py
版本: v1.0.0
创建日期: 2025-10-30
"""

import time
import json
from datetime import datetime
from typing import Dict, Optional, Any, List
from pathlib import Path
import logging
import sys

# 添加项目根目录到 Python 路径
project_root = str(Path(__file__).parent.parent)
if project_root not in sys.path:
    sys.path.append(project_root)

from bit.common.time_utils import TimeInfo, TimeSource, TimePriorityManager


class PositionClaimManager:
    """
    持仓接管管理器
    
    功能:
    1. 检测交易所的外部持仓
    2. 获取持仓信息（开仓价、数量、时间等）
    3. 接管持仓并保存到本地
    4. 支持多种持仓信息推断方式
    """
    
    def __init__(self, 
                 exchange,
                 symbol: str,
                 position_manager,
                 logger: logging.Logger,
                 config: Dict[str, Any],
                 on_position_claimed=None):
        """
        初始化持仓接管管理器
        
        Args:
            exchange: CCXT交易所实例
            symbol: 交易币种
            position_manager: 持仓管理器实例（TrailingStopManager）
            logger: 日志记录器
            config: 配置字典（持仓接管配置）
            on_position_claimed: 持仓接管成功后的回调函数
        """
        self.exchange = exchange
        self.symbol = symbol
        self.position_manager = position_manager
        self.logger = logger
        self.config = config
        self.on_position_claimed = on_position_claimed
        
        # 解析配置
        self.claim_mode = config.get('claim_mode', 'DISABLED')
        self.infer_method = config.get('infer_method', 'ORDER_HISTORY')
        self.min_position_amount = config.get('min_position_amount', 0.001)
        self.detection_interval = config.get('detection_interval', 60)
        self.order_history_limit = config.get('order_history_limit', 50)
        self.hybrid_priority = config.get('hybrid_priority', 'EXTERNAL')
        self.auto_open_delay = config.get('auto_open_delay', 60)
        
        # 内部状态
        self.last_detection_time = 0
        self.claimed_positions = set()  # 已接管的持仓ID
        
        self.logger.info(f"✅ 持仓接管管理器初始化完成")
        self.logger.info(f"   接管模式: {self.claim_mode}")
        self.logger.info(f"   推断方式: {self.infer_method}")
    
    def is_enabled(self) -> bool:
        """检查持仓接管功能是否启用"""
        return self.claim_mode in ['ENABLED', 'HYBRID']
    
    def should_detect(self) -> bool:
        """
        检查是否应该进行持仓检测
        
        Returns:
            True如果应该检测，False否则
        """
        if not self.is_enabled():
            return False
        
        current_time = time.time()
        if current_time - self.last_detection_time < self.detection_interval:
            return False
        
        return True
    
    def detect_external_position(self) -> Optional[Dict[str, Any]]:
        """
        检测交易所的外部持仓
        
        Returns:
            持仓信息字典，如果没有检测到则返回None
        """
        try:
            self.last_detection_time = time.time()
            
            # 获取当前持仓
            positions = self.exchange.fetch_positions([self.symbol])
            
            if not positions:
                return None
            
            # 查找有效持仓
            for position in positions:
                contracts = float(position.get('contracts', 0))
                
                # 检查持仓量是否大于最小值
                if abs(contracts) < self.min_position_amount:
                    continue
                
                # 检查是否已被接管
                position_id = self._get_position_id(position)
                if position_id in self.claimed_positions:
                    continue
                
                # 检测到新的外部持仓
                self.logger.info(f"🔍 检测到外部持仓: {contracts} {self.symbol}")
                return position
            
            return None
            
        except Exception as e:
            self.logger.error(f"❌ 检测外部持仓失败: {e}")
            return None
    
    def get_position_info(self, position: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        获取持仓详细信息
        
        Args:
            position: 持仓对象
            
        Returns:
            持仓信息字典
        """
        try:
            # 基本信息
            contracts = float(position.get('contracts', 0))
            entry_price = float(position.get('entryPrice', 0))
            
            # 修正：从 position 对象正确获取持仓方向
            # 优先级：positionSide > side > contracts正负号
            position_side = position.get('positionSide', '').upper()  # 币安双向持仓模式的字段
            raw_side = position.get('side', '').lower()
            
            # 判断持仓方向
            if position_side in ['LONG', 'SHORT']:
                # 优先使用 positionSide（币安双向持仓模式）
                side = position_side
                self.logger.info(f"📊 从 positionSide 获取持仓方向: {side}")
            elif raw_side in ['long', 'short']:
                # 使用 side 字段
                side = raw_side.upper()
                self.logger.info(f"📊 从 side 获取持仓方向: {side}")
            elif contracts != 0:
                # 根据 contracts 正负号判断（兜底方案）
                side = 'LONG' if contracts > 0 else 'SHORT'
                self.logger.info(f"📊 从 contracts 正负号判断方向: {contracts} → {side}")
            else:
                # 默认为多单
                side = 'LONG'
                self.logger.warning(f"⚠️ 无法确定持仓方向，默认为 LONG")
            
            self.logger.info(f"📊 持仓信息: side={side}, contracts={abs(contracts)}, entry_price={entry_price}")
            
            # 根据推断方式获取详细信息
            if self.infer_method == 'ORDER_HISTORY':
                info = self._infer_from_order_history(side, contracts)
            elif self.infer_method == 'DMR_SIGNAL':
                info = self._infer_from_dmr_signal(side)
            elif self.infer_method == 'MARKET_PRICE':
                info = self._infer_from_market_price(side)
            else:
                self.logger.warning(f"⚠️ 未知的推断方式: {self.infer_method}，使用默认方式")
                info = self._infer_from_market_price(side)
            
            # 补充基本信息
            if info:
                # 修正：直接使用持仓方向 LONG/SHORT，而不是转换为订单操作 BUY/SELL
                # side 已在第139行转为大写：'LONG' 或 'SHORT'
                info['side'] = side  # 'LONG' = 多单持仓，'SHORT' = 空单持仓
                info['amount'] = abs(contracts)
                if entry_price > 0:
                    info['entry_price'] = entry_price
                info['infer_method'] = self.infer_method
            
            return info
            
        except Exception as e:
            self.logger.error(f"❌ 获取持仓信息失败: {e}")
            return None
    
    def _infer_from_order_history(self, side: str, contracts: float) -> Optional[Dict[str, Any]]:
        """
        从订单历史推断持仓信息（最准确的方式）
        
        Args:
            side: 持仓方向 ('long' 或 'short')
            contracts: 持仓数量
            
        Returns:
            持仓信息字典
        """
        try:
            self.logger.info("📋 正在从订单历史获取持仓信息...")
            
            # 获取最近的订单历史
            orders = self.exchange.fetch_orders(
                symbol=self.symbol,
                limit=self.order_history_limit
            )
            
            # 反向遍历订单，查找最近的开仓订单
            for order in reversed(orders):
                if order['status'] != 'closed':
                    continue
                
                order_side = order['side']  # 'buy' 或 'sell'
                order_amount = float(order['amount'])
                
                # 修正：匹配方向和数量（side 是 'LONG' 或 'SHORT'）
                # LONG 持仓通过 buy 订单开仓，SHORT 持仓通过 sell 订单开仓
                if (side == 'LONG' and order_side == 'buy') or \
                   (side == 'SHORT' and order_side == 'sell'):
                    # 检查数量是否接近
                    if abs(order_amount - abs(contracts)) < 0.01:
                        # 创建TimeInfo对象（从订单历史推断）
                        time_info = TimePriorityManager.create_from_binance_order(order)
                        if not time_info:
                            # 如果无法从订单中提取，使用订单时间戳
                            timestamp = order.get('timestamp', time.time() * 1000) / 1000
                            time_info = TimeInfo(
                                timestamp=timestamp,
                                source=TimeSource.PENDING_ORDER_TIME,
                                raw_data={'order_id': order['id']},
                                description="从订单历史获取（订单时间）"
                            )
                        
                        info = {
                            'entry_price': float(order['price']),
                            'entry_time': time_info.timestamp,
                            'order_id': order['id'],
                            'source': 'ORDER_HISTORY',
                            'time_info': time_info  # 新增：保存TimeInfo对象
                        }
                        
                        self.logger.info(f"✅ 从订单历史获取持仓信息:")
                        self.logger.info(f"   开仓价: {info['entry_price']}")
                        self.logger.info(f"   开仓时间: {time_info.iso_format} (来源: {time_info.source.value})")
                        self.logger.info(f"   订单ID: {info['order_id']}")
                        self.logger.info(f"   时间优先级: {time_info.priority}")
                        
                        return info
            
            self.logger.warning("⚠️ 未找到匹配的订单历史，使用市场价格推断")
            return self._infer_from_market_price(side)
            
        except Exception as e:
            self.logger.error(f"❌ 从订单历史推断失败: {e}")
            return self._infer_from_market_price(side)
    
    def _infer_from_dmr_signal(self, side: str) -> Optional[Dict[str, Any]]:
        """
        从DMR信号推断持仓信息
        
        Args:
            side: 持仓方向
            
        Returns:
            持仓信息字典
        """
        try:
            self.logger.info("📊 正在从DMR信号推断持仓信息...")
            
            # 获取当前市场价格
            ticker = self.exchange.fetch_ticker(self.symbol)
            current_price = float(ticker['last'])
            
            # 使用当前价格作为开仓价（近似）
            # 创建TimeInfo对象（策略周期时间）
            time_info = TimePriorityManager.create_strategy_cycle_time("从DMR信号推断，使用策略周期时间")
            
            info = {
                'entry_price': current_price,
                'entry_time': time_info.timestamp,
                'order_id': None,
                'source': 'DMR_SIGNAL',
                'time_info': time_info  # 新增：保存TimeInfo对象
            }
            
            self.logger.info(f"✅ 从DMR信号推断持仓信息:")
            self.logger.info(f"   推断开仓价: {info['entry_price']} (当前市价)")
            self.logger.info(f"   时间来源: {time_info.source.value}, 优先级: {time_info.priority}")
            
            return info
            
        except Exception as e:
            self.logger.error(f"❌ 从DMR信号推断失败: {e}")
            return None
    
    def _infer_from_market_price(self, side: str) -> Optional[Dict[str, Any]]:
        """
        从市场价格推断持仓信息（最简单但最不准确的方式）
        
        Args:
            side: 持仓方向
            
        Returns:
            持仓信息字典
        """
        try:
            self.logger.info("💱 正在从市场价格推断持仓信息...")
            
            # 获取当前市场价格
            ticker = self.exchange.fetch_ticker(self.symbol)
            current_price = float(ticker['last'])
            
            # 创建TimeInfo对象（系统时间）
            time_info = TimePriorityManager.create_system_time("从市场价格推断，使用系统时间")
            
            info = {
                'entry_price': current_price,
                'entry_time': time_info.timestamp,
                'order_id': None,
                'source': 'MARKET_PRICE',
                'time_info': time_info  # 新增：保存TimeInfo对象
            }
            
            self.logger.info(f"✅ 从市场价格推断持仓信息:")
            self.logger.info(f"   推断开仓价: {info['entry_price']} (当前市价)")
            self.logger.info(f"   时间来源: {time_info.source.value}, 优先级: {time_info.priority}")
            
            return info
            
        except Exception as e:
            self.logger.error(f"❌ 从市场价格推断失败: {e}")
            return None
    
    def claim_position(self, position: Dict[str, Any], info: Dict[str, Any]) -> bool:
        """
        接管持仓
        
        Args:
            position: 持仓对象
            info: 持仓信息字典
            
        Returns:
            True如果接管成功，False否则
        """
        try:
            self.logger.info("========================================")
            self.logger.info("✅ 正在接管外部持仓...")
            
            # 构建持仓数据
            position_data = {
                'side': info['side'],
                'amount': info['amount'],
                'entry_price': info['entry_price'],
                'entry_time': info['entry_time'],
                'order_id': info.get('order_id'),
                'source': 'EXTERNAL',
                'infer_method': info.get('infer_method'),
                'claim_time': time.time()
            }
            
            # 保存到持仓管理器（移动止损状态文件）
            self.position_manager.save_position(position_data)
            
            # 记录已接管
            position_id = self._get_position_id(position)
            self.claimed_positions.add(position_id)
            
            # 打印接管信息
            self.logger.info("========================================")
            self.logger.info("✅ 外部持仓接管成功!")
            self.logger.info(f"   持仓方向: {info['side']}")
            self.logger.info(f"   开仓价格: {info['entry_price']}")
            self.logger.info(f"   持仓数量: {info['amount']}")
            self.logger.info(f"   推断方式: {info.get('infer_method')}")
            self.logger.info(f"   追踪止损: 将在下个周期启动")
            self.logger.info("========================================")
            
            # 调用回调函数，通知策略引擎创建持仓快照文件
            if self.on_position_claimed:
                try:
                    self.on_position_claimed(position_data)
                except Exception as e:
                    self.logger.error(f"❌ 执行持仓接管回调失败: {e}")
            
            return True
            
        except Exception as e:
            self.logger.error(f"❌ 接管持仓失败: {e}")
            return False
    
    def _get_position_id(self, position: Dict[str, Any]) -> str:
        """
        生成持仓唯一ID
        
        Args:
            position: 持仓对象
            
        Returns:
            持仓ID字符串
        """
        side = position.get('side', 'unknown')
        contracts = position.get('contracts', 0)
        timestamp = position.get('timestamp', 0)
        
        return f"{self.symbol}_{side}_{contracts}_{timestamp}"
    
    def check_and_claim(self) -> bool:
        """
        检查并接管外部持仓（主入口方法）
        
        Returns:
            True如果检测到并成功接管持仓，False否则
        """
        # 检查是否应该进行检测
        if not self.should_detect():
            return False
        
        # 检查本地是否已有持仓
        if self.position_manager.has_position():
            return False
        
        # 检测外部持仓
        position = self.detect_external_position()
        if not position:
            return False
        
        # 获取持仓信息
        info = self.get_position_info(position)
        if not info:
            self.logger.warning("⚠️ 无法获取持仓信息，跳过接管")
            return False
        
        # 接管持仓
        success = self.claim_position(position, info)
        return success
    
    def get_auto_open_delay(self) -> int:
        """
        获取自动开仓延迟时间（用于混合模式）
        
        Returns:
            延迟时间（秒）
        """
        return self.auto_open_delay
    
    def get_hybrid_priority(self) -> str:
        """
        获取混合模式下的优先级
        
        Returns:
            'EXTERNAL' 或 'AUTO'
        """
        return self.hybrid_priority
    
    def reset(self):
        """重置接管管理器状态"""
        self.claimed_positions.clear()
        self.last_detection_time = 0
        self.logger.info("🔄 持仓接管管理器已重置")
    
    def get_statistics(self) -> Dict[str, Any]:
        """
        获取统计信息
        
        Returns:
            统计信息字典
        """
        return {
            'claim_mode': self.claim_mode,
            'infer_method': self.infer_method,
            'claimed_count': len(self.claimed_positions),
            'last_detection_time': self.last_detection_time
        }
