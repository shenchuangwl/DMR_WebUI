import pandas as pd
from datetime import datetime
import os
import json
import time
from bit.config.bit_short_term_config import SHORT_TERM_CONFIG, get_short_term_config, TRAILING_STOP_CONFIG
from bit.config.bit_config import DMR_LONG_PERIOD, DMR_SHORT_PERIOD
from bit.utils.logger import setup_logger
from bit.common.trailing_stop_manager import TrailingStopManager
from bit.config.feature_switches import FEATURE_SWITCHES, POSITION_CLAIM_CONFIG
from bit.common.position_claim_manager import PositionClaimManager
from bit.common.time_utils import TimeInfo, TimeSource, TimePriorityManager

class ShortTermDMRStrategy:
    """短周期DMR12策略引擎（双时间框架DMR量化策略升级版）"""
    
    def __init__(self, data_fetcher, order_executor, position_manager, risk_manager, long_term_data_fetcher=None):
        self.data_fetcher = data_fetcher
        self.order_executor = order_executor
        self.position_manager = position_manager
        self.risk_manager = risk_manager
        self.long_term_data_fetcher = long_term_data_fetcher  # 长期数据获取器（用于DMR6趋势判断）
        self.config = SHORT_TERM_CONFIG
        self.logger = setup_logger(
            name='ShortTermStrategy',
            log_file=self.config['log_config']['log_file']
        )

        # 策略参数（双时间框架升级版）
        self.dmr_period = self.config['dmr_period']  # DMR12（短期信号执行）
        self.long_dmr_period = DMR_LONG_PERIOD       # DMR6（长期趋势判断）
        self.short_dmr_period = DMR_SHORT_PERIOD     # DMR12（短期信号执行）
        self.position_size = self.config['position_size']

        # 信号状态跟踪
        self.last_dmr_value = None
        self.last_long_dmr_value = None  # 长期DMR6状态跟踪

        # 初始化策略持仓为空
        self.strategy_position = None
        safe_symbol = self.config['symbol'].replace('/', '_')
        
        # 修正：使用绝对路径，避免在 /bit 目录下运行时创建 /bit/bit/data
        # 当前文件: .../bit/strategy/bit_short_term/strategy_engine.py
        # 需要回退4级到项目根: DMR_binance_Version_V14_A1
        current_file = os.path.abspath(__file__)
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(current_file))))
        data_dir = os.path.join(project_root, 'bit', 'data', 'bit_positions')
        os.makedirs(data_dir, exist_ok=True)
        
        self.position_file = os.path.join(data_dir, f"bit_short_term_position_{safe_symbol}.json")
        self.reset_flag_file = os.path.join(data_dir, f"bit_short_term_reset.flag")
        self.dmr_state_file = os.path.join(data_dir, f"bit_short_term_dmr_state_{safe_symbol}.json")
        
        # 输出路径信息用于验证
        self.logger.info(f"📁 数据文件目录: {data_dir}")
        self.logger.info(f"📁 持仓快照文件: {self.position_file}")
        self.logger.info(f"📁 DMR状态文件: {self.dmr_state_file}")

        # 初始化DMR状态
        self._load_dmr_state()

        # 初始化移动止损管理器
        self.trailing_stop_enabled = TRAILING_STOP_CONFIG.get('enabled', True)
        if self.trailing_stop_enabled:
            self.trailing_stop_manager = TrailingStopManager(
                symbol=self.config['symbol'],
                timeframe=self.config['timeframe'],
                data_dir=data_dir,  # 使用修正后的绝对路径
                exchange=data_fetcher.exchange  # 传递交易所实例用于设置币安止损单
            )
            self.logger.info(f"短周期策略移动止损功能已启用: 6K线法则 + ATR + 币安市价止损单")
        else:
            self.trailing_stop_manager = None
            self.logger.info(f"短周期策略移动止损功能未启用")

        # 记录初始化时的纠错配置信息
        self._log_correction_config_info()
        
        # 初始化功能开关和持仓接管管理器
        self.running_model = FEATURE_SWITCHES['running_model']['mode']
        self.auto_open_enabled = FEATURE_SWITCHES['order_execution']['auto_open']
        self.auto_close_enabled = FEATURE_SWITCHES['order_execution']['auto_close']
        self.claim_orphan_enabled = FEATURE_SWITCHES['state_sync']['claim_orphan_positions']
        
        self.logger.info("="*60)
        self.logger.info("🎯 短周期策略模型配置")
        self.logger.info(f"   运行模型: {self.running_model}")
        self.logger.info(f"   自动开仓: {self.auto_open_enabled}")
        self.logger.info(f"   自动平仓: {self.auto_close_enabled}")
        self.logger.info(f"   接管外部持仓: {self.claim_orphan_enabled}")
        self.logger.info("="*60)
        
        # 初始化持仓接管管理器
        if self.claim_orphan_enabled:
            # 注意：持仓接管功能依赖追踪止损管理器
            if not self.trailing_stop_manager:
                self.logger.error("❌ 持仓接管功能需要启用追踪止损管理器")
                raise ValueError("持仓接管功能需要启用追踪止损管理器")
            
            # 传递 trailing_stop_manager 而不是 position_manager
            # 因为 PositionClaimManager 需要调用 has_position() 和 save_position() 方法
            self.claim_manager = PositionClaimManager(
                exchange=data_fetcher.exchange,
                symbol=self.config['symbol'],
                position_manager=self.trailing_stop_manager,  # 修改：使用 trailing_stop_manager
                logger=self.logger,
                config=POSITION_CLAIM_CONFIG,
                on_position_claimed=self._on_position_claimed  # 添加持仓接管回调
            )
            self.logger.info("✅ 持仓接管管理器已初始化")
        else:
            self.claim_manager = None
            self.logger.info("ℹ️  持仓接管管理器未启用（原有模型1）")

    def get_long_term_trend(self):
        """
        获取长期趋势方向（基于DMR6）
        Returns:
            str: 'BULLISH' (多头趋势), 'BEARISH' (空头趋势), 'NEUTRAL' (中性), None (无数据)
        """
        try:
            if self.long_term_data_fetcher is None:
                self.logger.warning("长期数据获取器未初始化，无法获取长期趋势")
                return None

            # 获取长期数据
            long_df = self.long_term_data_fetcher.get_and_save_data()
            if long_df is None or long_df.empty:
                self.logger.warning("无法获取长期数据，无法判断趋势")
                return None

            # 获取最新的DMR6值
            current_long_dmr = long_df[f'dmr_{self.long_dmr_period}'].iloc[-1]
            self.last_long_dmr_value = current_long_dmr

            # 判断趋势方向
            if current_long_dmr > 0:
                trend = 'BULLISH'
                self.logger.info(f"长期趋势判断: 多头趋势 (DMR{self.long_dmr_period}: {current_long_dmr:.6f})")
            elif current_long_dmr < 0:
                trend = 'BEARISH'
                self.logger.info(f"长期趋势判断: 空头趋势 (DMR{self.long_dmr_period}: {current_long_dmr:.6f})")
            else:
                trend = 'NEUTRAL'
                self.logger.info(f"长期趋势判断: 中性趋势 (DMR{self.long_dmr_period}: {current_long_dmr:.6f})")

            return trend

        except Exception as e:
            self.logger.error(f"获取长期趋势失败: {e}")
            return None

    def _log_correction_config_info(self):
        """记录当前纠错配置信息"""
        try:
            signal_config = self.config.get('signal_config', {})
            self.logger.info(f"短周期策略纠错配置初始化:")
            self.logger.info(f"  - 纠错阈值: {signal_config.get('correction_threshold', 'N/A')}")
            self.logger.info(f"  - 纠错间隔: {signal_config.get('correction_min_interval', 'N/A')}秒")
            self.logger.info(f"  - 序列ID: {signal_config.get('sequence_id', 'N/A')}")
            self.logger.info(f"  - 时间周期组合: {signal_config.get('timeframe_combination', 'N/A')}")
            self.logger.info(f"  - 是否匹配序列: {signal_config.get('is_matched_sequence', 'N/A')}")
        except Exception as e:
            self.logger.warning(f"记录纠错配置信息失败: {e}")

    def _get_current_correction_config(self):
        """
        获取当前最新的纠错配置
        支持配置热更新
        """
        try:
            # 获取最新配置
            latest_config = get_short_term_config()
            signal_config = latest_config.get('signal_config', {})

            return {
                'correction_threshold': signal_config.get('correction_threshold', 0.0005),
                'correction_min_interval': signal_config.get('correction_min_interval', 3600),
                'sequence_id': signal_config.get('sequence_id'),
                'timeframe_combination': signal_config.get('timeframe_combination'),
                'is_matched_sequence': signal_config.get('is_matched_sequence', False),
                'description': signal_config.get('description', '短周期策略纠错配置')
            }
        except Exception as e:
            self.logger.error(f"获取纠错配置失败，使用默认值: {e}")
            return {
                'correction_threshold': 0.0005,
                'correction_min_interval': 3600,
                'sequence_id': None,
                'timeframe_combination': 'unknown',
                'is_matched_sequence': False,
                'description': '默认纠错配置'
            }

    def reconcile_state(self):
        """
        启动时核对状态。
        1. 检查强制重置标志。
        2. 对比本地与交易所状态，处理手动平仓等情况。
        3. “认领”逻辑已移至 execute_signal，启动时不再主动认领未知持仓。
        """
        # 1. 检查强制重置标志文件
        if os.path.exists(self.reset_flag_file):
            self.logger.warning("检测到重置标志文件，将强制重置策略状态。")
            self._reset_position_state()
            try:
                os.remove(self.reset_flag_file)
                self.logger.info(f"已删除重置标志文件: {self.reset_flag_file}")
            except OSError as e:
                self.logger.error(f"删除重置标志文件失败: {e}")
            return # 完成重置，直接返回

        # 2. 正常核对流程
        self._load_strategy_position()
        local_position = self.get_strategy_position()

        if local_position:
            # 如果本地有持仓记录, 我们就精确地检查对应方向的持仓
            expected_side = local_position['side']
            exchange_position = self.position_manager.get_current_position(side=expected_side)
            
            if not exchange_position:
                self.logger.info(f"检测到本地有 '{expected_side}' 持仓记录但交易所无对应持仓，判定为外部手动平仓。")
                self._reset_position_state()
            else:
                self.logger.info(f"本地与交易所均有 '{expected_side}' 持仓，状态同步。")
                
                # 【关键修复】同步持仓信息到移动止损管理器
                if self.trailing_stop_enabled and self.trailing_stop_manager:
                    # 检查移动止损管理器是否已有持仓记录
                    if not self.trailing_stop_manager.has_position():
                        self.logger.info("检测到移动止损管理器无持仓记录，正在同步...")
                        
                        # 从持仓文件恢复持仓信息
                        side = local_position['side']
                        entry_price = float(local_position.get('entry_price', 0))
                        amount = float(local_position.get('amount', 0))
                        
                        # 处理时间戳（可能是字符串或datetime对象）
                        timestamp = local_position.get('timestamp')
                        if isinstance(timestamp, str):
                            entry_time = timestamp
                        else:
                            entry_time = datetime.now().isoformat()
                        
                        # 同步到移动止损管理器
                        self.trailing_stop_manager.on_position_opened(
                            side=side,
                            entry_price=entry_price,
                            entry_time=entry_time,
                            position_amount=amount  # 传递持仓数量
                        )
                        self.logger.info(f"✓ 已同步持仓到移动止损管理器: {side} @ {entry_price:.6f}, 数量: {amount}")
        else:
            # 如果本地无持仓记录，我们假定本策略当前没有持仓。
            # 真正的“认领”将在 execute_signal 中根据信号方向进行，以确保只认领自己的仓位。
            self.logger.info("本地无持仓记录，策略以无持仓状态启动。")

    def detect_signal(self, df):
        """检测DMR穿越信号和状态纠错信号（双时间框架协调版）"""
        if len(df) < 1:
            self.logger.warning("数据不足，无法检测信号（需要至少1条记录）")
            return None

        current_dmr = df[f'dmr_{self.dmr_period}'].iloc[-1]

        # 使用持久化的前值进行穿越检测
        previous_dmr = self.last_dmr_value

        # 详细记录DMR值变化
        if previous_dmr is not None:
            self.logger.info(f"短周期策略DMR{self.dmr_period}值变化: {previous_dmr:.6f} -> {current_dmr:.6f}")
        else:
            self.logger.info(f"短周期策略DMR{self.dmr_period}当前值: {current_dmr:.6f} (首次执行，无前值)")

        # 获取长期趋势方向
        long_term_trend = self.get_long_term_trend()
        self.logger.info(f"长期趋势判断: {long_term_trend}")

        # 检测短期穿越信号（只有在有前值的情况下才能检测穿越）
        crossover_signal = None
        if previous_dmr is not None:
            if previous_dmr <= 0 and current_dmr > 0:
                crossover_signal = 'LONG'  # 由负转正，开多信号
                self.logger.info(f"检测到穿越信号: LONG (负转正: {previous_dmr:.6f} -> {current_dmr:.6f})")
            elif previous_dmr >= 0 and current_dmr < 0:
                crossover_signal = 'SHORT'  # 由正转负，开空信号
                self.logger.info(f"检测到穿越信号: SHORT (正转负: {previous_dmr:.6f} -> {current_dmr:.6f})")
            else:
                self.logger.info(f"无穿越信号 (前值: {previous_dmr:.6f}, 当前值: {current_dmr:.6f})")
        else:
            self.logger.info("首次执行，无法检测穿越信号")

        # 双时间框架协调逻辑
        final_signal = self._coordinate_dual_timeframe_signal(crossover_signal, long_term_trend)

        # 检测状态纠错信号：当前持仓方向与DMR12符号不一致
        correction_signal = self._detect_position_correction_signal(current_dmr)

        # 优先级：协调后的穿越信号 > 纠错信号
        signal = final_signal or correction_signal

        # 详细记录信号处理结果
        if crossover_signal and final_signal:
            if previous_dmr is not None:
                self.logger.info(f"短周期策略检测到穿越信号: {crossover_signal}, DMR{self.dmr_period}: {previous_dmr:.6f} -> {current_dmr:.6f}")
            else:
                self.logger.info(f"短周期策略检测到穿越信号: {crossover_signal}, 当前DMR{self.dmr_period}: {current_dmr:.6f}")
            self.logger.info(f"双时间框架协调结果: {final_signal} (长期趋势: {long_term_trend})")
        elif crossover_signal and not final_signal:
            self.logger.info(f"短周期策略检测到穿越信号: {crossover_signal}, 但被长期趋势过滤 (长期趋势: {long_term_trend})")
        elif correction_signal:
            self.logger.info(f"短周期策略检测到纠错信号: {correction_signal}, 当前DMR{self.dmr_period}: {current_dmr:.6f}")
        else:
            self.logger.info(f"无交易信号 (穿越信号: {crossover_signal}, 纠错信号: {correction_signal})")

        # 无论是否有信号，都显示当前DMR值
        self.logger.info(f"短周期策略当前DMR{self.dmr_period}值: {current_dmr:.6f}")

        # 保存当前DMR值作为下次执行的前值
        self._save_dmr_state(current_dmr)

        return signal

    def _coordinate_dual_timeframe_signal(self, short_signal, long_trend):
        """
        双时间框架协调逻辑

        Args:
            short_signal: 短期信号 ('LONG', 'SHORT', None)
            long_trend: 长期趋势 ('BULLISH', 'BEARISH', 'NEUTRAL', None)

        Returns:
            str: 协调后的信号 ('LONG', 'SHORT', None)
        """
        if short_signal is None or long_trend is None:
            return short_signal

        # 多头趋势策略（长期DMR6 > 0时）
        if long_trend == 'BULLISH':
            if short_signal == 'LONG':
                # 开多仓条件：短期DMR12从负值转为正值时
                self.logger.info("双时间框架协调: 多头趋势中，执行开多信号")
                return 'LONG'
            elif short_signal == 'SHORT':
                # 平多仓条件：短期DMR12从正值转为负值时（不开空仓）
                strategy_position = self.get_strategy_position()
                if strategy_position and strategy_position['side'] == 'long':
                    self.logger.info("双时间框架协调: 多头趋势中，执行平多信号")
                    return 'CLOSE_LONG'
                else:
                    self.logger.info("双时间框架协调: 多头趋势中，过滤空头信号")
                    return None

        # 空头趋势策略（长期DMR6 < 0时）
        elif long_trend == 'BEARISH':
            if short_signal == 'SHORT':
                # 开空仓条件：短期DMR12从正值转为负值时
                self.logger.info("双时间框架协调: 空头趋势中，执行开空信号")
                return 'SHORT'
            elif short_signal == 'LONG':
                # 平空仓条件：短期DMR12从负值转为正值时（不开多仓）
                strategy_position = self.get_strategy_position()
                if strategy_position and strategy_position['side'] == 'short':
                    self.logger.info("双时间框架协调: 空头趋势中，执行平空信号")
                    return 'CLOSE_SHORT'
                else:
                    self.logger.info("双时间框架协调: 空头趋势中，过滤多头信号")
                    return None

        # 中性趋势时，暂停交易
        elif long_trend == 'NEUTRAL':
            self.logger.info("双时间框架协调: 中性趋势中，暂停交易信号")
            return None

        return short_signal

    def _detect_position_correction_signal(self, current_dmr):
        """检测持仓纠错信号 - 使用动态配置系统"""
        # 检查是否启用纠错功能
        if not self.config['signal_config'].get('position_correction_enabled', False):
            return None

        strategy_position = self.get_strategy_position()
        if not strategy_position:
            return None

        # 获取最新的纠错配置（支持热更新）
        correction_config = self._get_current_correction_config()
        correction_threshold = correction_config['correction_threshold']
        min_interval = correction_config['correction_min_interval']

        # 检查纠错间隔，避免频繁纠错
        if hasattr(self, 'last_correction_time'):
            if time.time() - self.last_correction_time < min_interval:
                return None

        # 如果DMR26明显为正值但持有空仓，发出开多信号
        if current_dmr > correction_threshold and strategy_position['side'] == 'short':
            self.logger.warning(f"持仓纠错：DMR26为正值({current_dmr:.6f})但持有空仓，建议开多")
            self.logger.info(f"纠错配置 - 序列ID: {correction_config['sequence_id']}, "
                           f"阈值: {correction_threshold}, 组合: {correction_config['timeframe_combination']}")
            self.last_correction_time = time.time()
            return 'LONG'

        # 如果DMR26明显为负值但持有多仓，发出开空信号
        if current_dmr < -correction_threshold and strategy_position['side'] == 'long':
            self.logger.warning(f"持仓纠错：DMR26为负值({current_dmr:.6f})但持有多仓，建议开空")
            self.logger.info(f"纠错配置 - 序列ID: {correction_config['sequence_id']}, "
                           f"阈值: {correction_threshold}, 组合: {correction_config['timeframe_combination']}")
            self.last_correction_time = time.time()
            return 'SHORT'

        return None
    
    def execute_signal(self, signal, current_price):
        """
        执行交易信号（双时间框架协调版）
        新增逻辑：在开仓前，如果本地无持仓记录，会先检查交易所是否存在同向的、可能是本策略遗漏的持仓。
        """
        try:
            self.logger.info(f"短周期策略开始执行信号: {signal}, 当前价格: {current_price}")

            trade_quantity = self.position_manager.calculate_position_size(current_price)
            strategy_position = self.get_strategy_position()

            self.logger.info(f"短周期策略交易参数 - 数量: {trade_quantity}, 当前持仓: {strategy_position}")

            if signal == 'LONG':
                if strategy_position and strategy_position['side'] == 'long':
                    self.logger.info("短周期策略：本策略已有多仓，无需操作")
                    return

                # 检查是否存在需要认领的交易所多头持仓
                if strategy_position is None:
                    exchange_long_pos = self.position_manager.get_current_position(side='long')
                    if exchange_long_pos:
                        # 检查持仓是否属于本策略
                        strategy_id = self.config.get('strategy_id', 'DMR12_SHORT')
                        if self._is_position_from_strategy(exchange_long_pos, strategy_id):
                            self.logger.info("检测到未追踪的多头持仓，且当前信号为多头，短周期策略将认领该持仓。")
                            self._claim_exchange_position(exchange_long_pos)
                            return  # 认领后，本次操作完成
                        else:
                            self.logger.info(f"检测到未追踪的多头持仓，但不属于短周期策略({strategy_id})，跳过认领。")

                # 如果有空仓，先平仓
                if strategy_position and strategy_position['side'] == 'short':
                    self.logger.info("短周期策略：平空仓")
                    close_order, _ = self.order_executor.close_short(strategy_position['amount'], strategy_position)
                    if not close_order:
                        self.logger.error("短周期策略：平空订单未成功提交，保留持仓状态，本次不开反向仓，等待下一周期重试")
                        return
                    # 平仓成功后立即清理持仓记录，避免随后开仓失败时残留旧方向记录
                    self._reset_position_state()

                # 与长周期一致：允许平旧仓，但模型2不得自动开反向仓。
                if not self.auto_open_enabled:
                    self.logger.warning("⚠️  当前模型禁用自动开仓（模型2），跳过开多操作")
                    return

                # 开多仓
                self.logger.info("短周期策略：执行开多操作")
                order, time_info = self.order_executor.open_long(current_price, trade_quantity)
                if order:
                    self._update_strategy_position('long', trade_quantity, current_price, time_info=time_info)

            elif signal == 'CLOSE_LONG':
                # 双时间框架新增：仅平多仓，不开空仓
                if strategy_position and strategy_position['side'] == 'long':
                    self.logger.info("短周期策略：双时间框架协调 - 执行平多操作")
                    close_order, _ = self.order_executor.close_long(strategy_position['amount'], strategy_position)
                    if not close_order:
                        self.logger.error("短周期策略：平多订单未成功提交，保留持仓状态，等待下一周期重试")
                        return
                    self._reset_position_state()
                else:
                    self.logger.info("短周期策略：无多仓可平，跳过平多操作")
                return

            elif signal == 'CLOSE_SHORT':
                # 双时间框架新增：仅平空仓，不开多仓
                if strategy_position and strategy_position['side'] == 'short':
                    self.logger.info("短周期策略：双时间框架协调 - 执行平空操作")
                    close_order, _ = self.order_executor.close_short(strategy_position['amount'], strategy_position)
                    if not close_order:
                        self.logger.error("短周期策略：平空订单未成功提交，保留持仓状态，等待下一周期重试")
                        return
                    self._reset_position_state()
                else:
                    self.logger.info("短周期策略：无空仓可平，跳过平空操作")
                return

            elif signal == 'SHORT':
                if strategy_position and strategy_position['side'] == 'short':
                    self.logger.info("短周期策略：本策略已有空仓，无需操作")
                    return

                # 检查是否存在需要认领的交易所空头持仓
                if strategy_position is None:
                    exchange_short_pos = self.position_manager.get_current_position(side='short')
                    if exchange_short_pos:
                        # 检查持仓是否属于本策略
                        strategy_id = self.config.get('strategy_id', 'DMR26_SHORT')
                        if self._is_position_from_strategy(exchange_short_pos, strategy_id):
                            self.logger.info("检测到未追踪的空头持仓，且当前信号为空头，短周期策略将认领该持仓。")
                            self._claim_exchange_position(exchange_short_pos)
                            return  # 认领后，本次操作完成
                        else:
                            self.logger.info(f"检测到未追踪的空头持仓，但不属于短周期策略({strategy_id})，跳过认领。")

                # 如果有多仓，先平仓
                if strategy_position and strategy_position['side'] == 'long':
                    self.logger.info("短周期策略：平多仓")
                    close_order, _ = self.order_executor.close_long(strategy_position['amount'], strategy_position)
                    if not close_order:
                        self.logger.error("短周期策略：平多订单未成功提交，保留持仓状态，本次不开反向仓，等待下一周期重试")
                        return
                    # 平仓成功后立即清理持仓记录，避免随后开仓失败时残留旧方向记录
                    self._reset_position_state()
                
                # 与长周期一致：直接调用 execute_signal 也必须遵守开仓开关。
                if not self.auto_open_enabled:
                    self.logger.warning("⚠️  当前模型禁用自动开仓（模型2），跳过开空操作")
                    return

                # 开空仓
                self.logger.info("短周期策略：执行开空操作")
                order, time_info = self.order_executor.open_short(current_price, trade_quantity)
                if order:
                    self._update_strategy_position('short', trade_quantity, current_price, time_info=time_info)
                
        except Exception as e:
            self.logger.error(f"短周期策略执行信号失败: {e}", exc_info=True)

    def run_strategy(self, df: pd.DataFrame):
        """
        运行策略主循环。
        使用传入的、已包含DMR指标的DataFrame。
        
        执行优先级：
        0. 检查并接管外部持仓（模型2/3）
        1. 检查移动止损
        2. 检测交易信号
        """
        try:
            # 外部已完成数据获取和DMR计算
            if df is None or df.empty:
                self.logger.warning("传入的DataFrame为空，跳过策略执行。")
                return

            current_price = df['close'].iloc[-1]
            
            # 0. 优先检查并接管外部持仓（模型2/3专属）
            if self.claim_orphan_enabled and self.claim_manager:
                # 检查本地是否已有持仓记录
                has_local_position = self.trailing_stop_manager.has_position() if self.trailing_stop_manager else False
                
                if not has_local_position:
                    self.logger.info("="*60)
                    self.logger.info(f"🔍 【模型{self.running_model}】检查外部持仓")
                    self.logger.info("="*60)
                    
                    # 尝试检测并接管外部持仓
                    if self.claim_manager.check_and_claim():
                        self.logger.info("✅ 外部持仓接管成功，等待下个周期启动追踪止损")
                        return  # 接管成功后，本周期结束，下个周期启动止损
                    else:
                        self.logger.debug("ℹ️  未检测到外部持仓，继续正常流程")
                else:
                    self.logger.debug("ℹ️  本地已有持仓记录，跳过外部持仓检测")
            
            # 1. 检查移动止损（优先级高）
            if self.trailing_stop_enabled and self.trailing_stop_manager:
                if self.trailing_stop_manager.has_position():
                    self.logger.info("="*60)
                    self.logger.info("【移动止损检查】开始")
                    
                    # 验证本地持仓方向与交易所实际持仓是否一致
                    position_info = self.trailing_stop_manager.get_position_info()
                    local_side = position_info.get('side', '').lower()
                    
                    try:
                        # 获取交易所实际持仓
                        positions = self.data_fetcher.exchange.fetch_positions([self.config['symbol']])
                        exchange_side = None
                        for pos in positions:
                            contracts = float(pos.get('contracts', 0))
                            if abs(contracts) >= 0.001:  # 有实际持仓
                                position_side = pos.get('positionSide', '').upper()
                                raw_side = pos.get('side', '').lower()
                                # 判断实际持仓方向
                                if position_side in ['LONG', 'SHORT']:
                                    exchange_side = position_side.lower()
                                elif raw_side in ['long', 'short']:
                                    exchange_side = raw_side
                                elif contracts > 0:
                                    exchange_side = 'long'
                                else:
                                    exchange_side = 'short'
                                break
                        
                        # 如果方向不一致，记录警告并修正
                        if exchange_side and local_side != exchange_side:
                            self.logger.error(f"❌ 持仓方向不一致！本地记录: {local_side.upper()}, 交易所实际: {exchange_side.upper()}")
                            self.logger.error(f"正在修正本地持仓方向...")
                            # 修正本地数据
                            self.trailing_stop_manager.position_data['side'] = exchange_side
                            self.trailing_stop_manager._save_position_data()
                            position_info = self.trailing_stop_manager.get_position_info()
                            self.logger.info(f"✓ 持仓方向已修正为: {exchange_side.upper()}")
                    except Exception as e:
                        self.logger.warning(f"⚠️ 验证持仓方向失败: {e}")
                    
                    atr_period = TRAILING_STOP_CONFIG.get('atr_period', 14)
                    stop_result = self.trailing_stop_manager.update_trailing_stop(
                        df=df,
                        current_price=current_price,
                        atr_period=atr_period
                    )
                    
                    # 详细显示止损状态（每次都显示）
                    position_info = self.trailing_stop_manager.get_position_info()
                    self.logger.info(f"持仓方向: {position_info.get('side', 'N/A').upper()}")
                    self.logger.info(f"入场价格: {position_info.get('entry_price', 0):.6f}")
                    self.logger.info(f"当前价格: {current_price:.6f}")
                    self.logger.info(f"当前止损价: {stop_result.get('current_stop_loss', 'N/A')}")
                    self.logger.info(f"6K线周期数: {stop_result.get('kline_cycles', 0)}")
                    
                    # 显示止损更新历史
                    if position_info.get('stop_loss_history'):
                        self.logger.info(f"止损更新次数: {len(position_info['stop_loss_history'])}")
                        if len(position_info['stop_loss_history']) > 0:
                            latest_update = position_info['stop_loss_history'][-1]
                            self.logger.info(f"最新更新: 周期{latest_update['cycle']}, 止损价{latest_update['stop_loss']:.6f}")
                    
                    if stop_result.get('stop_loss_updated'):
                        self.logger.info(f"✓ 止损价格已更新！")
                    
                    # 如果触发止损，立即执行平仓
                    if stop_result['stop_loss_triggered'] and stop_result['should_close']:
                        self.logger.warning("="*60)
                        self.logger.warning("⚠️  【移动止损触发】平仓信号！")
                        self.logger.warning(f"触发原因: {stop_result['message']}")
                        self.logger.warning(f"止损类型: 6K线法则 + ATR")
                        
                        side = position_info['side']
                        entry_price = position_info.get('entry_price', 0)
                        stop_loss = stop_result.get('current_stop_loss', 0)
                        
                        if side == 'long':
                            pnl_pct = ((current_price - entry_price) / entry_price) * 100
                            self.logger.warning(f"做多止损 | 入场:{entry_price:.6f} -> 当前:{current_price:.6f} | 盈亏:{pnl_pct:+.2f}%")
                        else:
                            pnl_pct = ((entry_price - current_price) / entry_price) * 100
                            self.logger.warning(f"做空止损 | 入场:{entry_price:.6f} -> 当前:{current_price:.6f} | 盈亏:{pnl_pct:+.2f}%")
                        
                        self.logger.warning("="*60)
                        
                        # 执行平仓
                        try:
                            # 获取持仓数量
                            position_amount = position_info.get('position_amount', 0)
                            
                            if side == 'long':
                                self.logger.warning(f">>> 执行做多止损平仓 @ {current_price:.6f}")
                                self.logger.warning(f">>> 平仓数量: {position_amount}")
                                # 修正：传递正确的参数 (数量, 策略持仓)
                                close_order, _ = self.order_executor.close_long(position_amount, position_info)
                                if not close_order:
                                    raise RuntimeError("做多止损平仓订单未成功提交，保留持仓状态，等待下一周期重试")
                                self.logger.warning(f">>> 做多止损平仓订单已提交")
                            else:  # short
                                self.logger.warning(f">>> 执行做空止损平仓 @ {current_price:.6f}")
                                self.logger.warning(f">>> 平仓数量: {position_amount}")
                                # 修正：传递正确的参数 (数量, 策略持仓)
                                close_order, _ = self.order_executor.close_short(position_amount, position_info)
                                if not close_order:
                                    raise RuntimeError("做空止损平仓订单未成功提交，保留持仓状态，等待下一周期重试")
                                self.logger.warning(f">>> 做空止损平仓订单已提交")
                            
                            # 重置持仓状态
                            self._reset_position_state()
                            
                            self.logger.warning("✓ 移动止损平仓完成，所有持仓状态已清理")
                            self.logger.warning("="*60)
                            return  # 平仓后直接返回，不再执行其他逻辑
                            
                        except Exception as e:
                            self.logger.error(f"✗ 移动止损平仓失败: {e}", exc_info=True)
                            self.logger.error("="*60)
                    else:
                        # 未触发止损
                        if stop_result.get('current_stop_loss'):
                            distance = 0
                            if stop_result['side'] == 'long':
                                distance = ((current_price - stop_result['current_stop_loss']) / current_price) * 100
                                self.logger.info(f"距离止损: {distance:.2f}% (当前价高于止损价 {distance:.2f}%)")
                            else:
                                distance = ((stop_result['current_stop_loss'] - current_price) / current_price) * 100
                                self.logger.info(f"距离止损: {distance:.2f}% (当前价低于止损价 {distance:.2f}%)")
                        
                        self.logger.info("✓ 止损监控正常，继续持仓")
                    
                    self.logger.info("【移动止损检查】完成")
                    self.logger.info("="*60)
                else:
                    # 移动止损已启用，但当前无持仓
                    self.logger.debug("移动止损已启用，但当前无持仓记录")

            # 2. 检测交易信号
            signal = self.detect_signal(df)
            self.logger.info(f"短周期策略信号检测结果: {signal}")

            if signal:
                # 检查自动开仓开关（模型1和模型3开启，模型2关闭）
                if not self.auto_open_enabled:
                    self.logger.info("="*60)
                    self.logger.info(f"🔕 【模型{self.running_model}】检测到交易信号但自动开仓已关闭")
                    self.logger.info(f"   信号类型: {signal}")  # signal 是字符串，不是字典
                    self.logger.info(f"   信号价格: {current_price:.6f}")
                    self.logger.info(f"   ⚠️  请手动开仓后，策略将自动接管并管理持仓")
                    self.logger.info("="*60)
                    return  # 不执行自动开仓，等待手动开仓后接管
                
                self.logger.info(f"✅ 【模型{self.running_model}】自动开仓已启用，执行信号")
                self.logger.info(f"短周期策略检测到信号: {signal}，开始执行交易流程")

                # 风控检查
                risk_check_result = self.risk_manager.check_risk_limits()
                self.logger.info(f"短周期策略风控检查结果: {risk_check_result}")

                if risk_check_result:
                    self.logger.info(f"短周期策略风控通过，当前价格: {current_price}，准备执行信号: {signal}")
                    self.execute_signal(signal, current_price)
                    self.logger.info(f"短周期策略信号执行完成: {signal}")
                else:
                    self.logger.warning("短周期策略：风控限制，跳过交易信号")
                    # 记录风控详细信息
                    self.logger.warning(f"风控详情 - 日盈亏: {self.risk_manager.daily_pnl}, 总盈亏: {self.risk_manager.total_pnl}")
                    self.logger.warning(f"风控阈值 - 最大日损失: {self.risk_manager.max_daily_loss_pct:.2%}(账户权益), 最大回撤: {self.risk_manager.max_drawdown_pct:.2%}(峰值权益), 当日最大交易数: {self.risk_manager.max_daily_trades}")
            else:
                self.logger.info("短周期策略无交易信号，跳过执行")

        except Exception as e:
            self.logger.error(f"短周期策略运行失败: {e}", exc_info=True)
    
    def _save_strategy_position(self):
        """保存策略持仓到文件"""
        try:
            os.makedirs(os.path.dirname(self.position_file), exist_ok=True)
            with open(self.position_file, 'w') as f:
                json.dump(self.strategy_position, f, default=str)
            self.logger.info(f"✅ 持仓快照已保存: {self.position_file}")
        except Exception as e:
            self.logger.warning(f"保存策略持仓失败: {e}")

    def _load_strategy_position(self):
        """从文件加载策略持仓"""
        try:
            with open(self.position_file, 'r') as f:
                self.strategy_position = json.load(f)
                if self.strategy_position:
                    self.logger.info(f"从文件加载策略持仓成功: {self.strategy_position}")
        except (FileNotFoundError, json.JSONDecodeError):
            self.strategy_position = None
            self.logger.info("策略持仓文件不存在或无效，初始化为空仓。")

    def _reset_position_state(self):
        """重置并清空策略持仓状态"""
        self.strategy_position = None
        if os.path.exists(self.position_file):
            try:
                os.remove(self.position_file)
                self.logger.info(f"已删除本地持仓状态文件: {self.position_file}")
            except OSError as e:
                self.logger.error(f"删除本地持仓文件失败: {e}")

        # 清除移动止损管理器的持仓记录
        if self.trailing_stop_enabled and self.trailing_stop_manager:
            self.trailing_stop_manager.on_position_closed()

        # 同时重置DMR状态
        self._reset_dmr_state()

    def get_strategy_position(self):
        """获取本策略的持仓记录"""
        return getattr(self, 'strategy_position', None)
    
    def _on_position_claimed(self, position_data: dict):
        """
        持仓接管成功后的回调函数
        创建持仓快照文件
        
        Args:
            position_data: 接管的持仓数据
        """
        try:
            # 转换持仓方向格式：LONG/SHORT -> long/short
            side = position_data.get('side', '').lower()
            if side not in ['long', 'short']:
                # 处理大写情况
                side_upper = position_data.get('side', '').upper()
                side = 'long' if side_upper == 'LONG' else 'short'
            
            # 创建策略持仓快照
            self.strategy_position = {
                'side': side,
                'amount': position_data['amount'],
                'entry_price': position_data['entry_price'],
                'entry_time': position_data.get('entry_time'),
                'timestamp': datetime.now().isoformat(),
                'source': 'CLAIMED',  # 标记为接管持仓
                'order_id': position_data.get('order_id'),
                'infer_method': position_data.get('infer_method')
            }
            
            # 保存持仓快照文件
            self._save_strategy_position()
            self.logger.info(f"✅ 持仓快照文件已创建: {self.position_file}")
            
        except Exception as e:
            self.logger.error(f"❌ 创建持仓快照文件失败: {e}")
    
    def _update_strategy_position(self, side, amount, price, time_info=None):
        """
        更新本策略的持仓记录
        
        Args:
            side: 持仓方向
            amount: 持仓数量
            price: 开仓价格
            time_info: TimeInfo对象，包含时间来源标记
        """
        self.strategy_position = {
            'side': side,
            'amount': amount,
            'entry_price': price,
            'timestamp': datetime.now()
        }
        # 可以选择持久化到文件
        self._save_strategy_position()
        
        # 记录开仓到移动止损管理器
        if self.trailing_stop_enabled and self.trailing_stop_manager:
            # 根据策略模型决定使用哪种时间
            if time_info is not None:
                # 模型2：使用实际成交时间
                self.logger.info(f"✅ 使用实际成交时间传递给移动止损管理器")
                self.trailing_stop_manager.on_position_opened(
                    side=side,
                    entry_price=price,
                    position_amount=amount,
                    time_info=time_info
                )
            else:
                # 模型1/模型3：使用原有方式（兼容）
                self.logger.info(f"ℹ️  使用系统时间传递给移动止损管理器")
                self.trailing_stop_manager.on_position_opened(
                    side=side,
                    entry_price=price,
                    entry_time=datetime.now().isoformat(),
                    position_amount=amount
                )

    def _claim_exchange_position(self, exchange_position):
        """
        认领交易所中的持仓，将其标记为本策略的持仓
        
        Args:
            exchange_position: 从交易所获取的持仓信息
        """
        try:
            # 从交易所持仓信息中提取关键信息
            position_side = 'long' if exchange_position.get('side') == 'long' else 'short'
            position_amount = float(exchange_position.get('contracts', 0))
            position_price = float(exchange_position.get('entryPrice', 0))
            
            # 更新策略持仓状态
            self.logger.info(f"短周期策略认领{position_side}持仓: 数量={position_amount}, 入场价={position_price}")
            self._update_strategy_position(position_side, position_amount, position_price)
            
            # 记录认领信息
            claim_info = {
                'timestamp': int(time.time()),
                'position_side': position_side,
                'position_amount': position_amount,
                'position_price': position_price,
                'strategy_id': self.config.get('strategy_id', 'DMR26_SHORT'),
                'claim_reason': 'signal_match'
            }
            
            # 可以将认领信息保存到文件或数据库中，方便后续分析
            safe_symbol = self.config['symbol'].replace('/', '_')
            claim_file = f"data/claims/short_term_claim_{safe_symbol}.json"
            
            # 确保目录存在
            os.makedirs(os.path.dirname(claim_file), exist_ok=True)
            
            # 保存认领记录
            with open(claim_file, 'a') as f:
                f.write(json.dumps(claim_info) + '\n')
                
            self.logger.info(f"持仓认领信息已记录到: {claim_file}")
            
        except Exception as e:
            self.logger.error(f"认领持仓失败: {e}", exc_info=True)

    def _is_position_from_strategy(self, position, strategy_id):
        """
        判断持仓是否来自本策略
        通过检查持仓信息中的标识（如clientOrderId）或其他特征来判断
        """
        try:
            # 尝试从持仓信息中获取clientOrderId
            client_order_id = position.get('info', {}).get('clientOrderId', '')
            
            # 如果clientOrderId包含策略ID，则认为是本策略的持仓
            if strategy_id in client_order_id:
                return True
            
            # 如果没有clientOrderId或不包含策略ID，尝试其他方法判断
            # 例如，可以检查持仓大小是否接近本策略的标准持仓大小
            position_size = float(position.get('contracts', 0))
            expected_size = self.config.get('position_size', 0)
            
            # 如果持仓大小与预期相差不超过10%，也可以认为是本策略的持仓
            if expected_size > 0 and abs(position_size - expected_size) / expected_size < 0.1:
                self.logger.info(f"通过持仓大小匹配认领持仓: 实际={position_size}, 预期={expected_size}")
                return True
            
            return False
        except Exception as e:
            self.logger.error(f"判断持仓来源时出错: {e}")
            return False

    def _load_dmr_state(self):
        """从文件加载DMR状态"""
        try:
            with open(self.dmr_state_file, 'r') as f:
                dmr_state = json.load(f)
                self.last_dmr_value = dmr_state.get('last_dmr_value')
                if self.last_dmr_value is not None:
                    self.logger.info(f"从文件加载DMR状态成功: 上次DMR{self.dmr_period}值 = {self.last_dmr_value:.6f}")
                else:
                    self.logger.info("DMR状态文件存在但无有效数据，初始化为None")
        except (FileNotFoundError, json.JSONDecodeError):
            self.last_dmr_value = None
            self.logger.info("DMR状态文件不存在或无效，初始化为None")
        except Exception as e:
            self.last_dmr_value = None
            self.logger.error(f"加载DMR状态失败: {e}")

    def _save_dmr_state(self, current_dmr):
        """保存DMR状态到文件"""
        try:
            os.makedirs(os.path.dirname(self.dmr_state_file), exist_ok=True)
            dmr_state = {
                'last_dmr_value': current_dmr,
                'timestamp': time.time(),
                'datetime': datetime.now().isoformat()
            }
            with open(self.dmr_state_file, 'w') as f:
                json.dump(dmr_state, f, default=str)
            self.last_dmr_value = current_dmr
            self.logger.debug(f"DMR状态已保存: DMR{self.dmr_period} = {current_dmr:.6f}")
        except Exception as e:
            self.logger.error(f"保存DMR状态失败: {e}")

    def _reset_dmr_state(self):
        """重置DMR状态"""
        self.last_dmr_value = None
        if os.path.exists(self.dmr_state_file):
            try:
                os.remove(self.dmr_state_file)
                self.logger.info(f"已删除DMR状态文件: {self.dmr_state_file}")
            except OSError as e:
                self.logger.error(f"删除DMR状态文件失败: {e}")
