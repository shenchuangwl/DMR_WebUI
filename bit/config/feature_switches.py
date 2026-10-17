"""
BIT专属策略功能开关配置
用于控制策略运行链条中各个关键环节的启用/禁用

文件路径: @DMR_binance_Version_V14_A1/bit/config/feature_switches.py
版本: v1.0.0
创建日期: 2025-10-30
"""

from enum import Enum
from typing import Dict, Any, Callable, List
import logging
import json
from pathlib import Path

# 初始化日志
logger = logging.getLogger(__name__)

# 配置文件路径
CONFIG_DIR = Path(__file__).parent.parent / 'data' / 'config'
MODEL_STATE_FILE = CONFIG_DIR / 'model_state.json'


class SwitchState(Enum):
    """开关状态枚举"""
    ENABLED = True      # 开启
    DISABLED = False    # 关闭


class SwitchMode(Enum):
    """开关模式"""
    FORCE_ON = 'on'         # 强制开启（不可关闭）
    FORCE_OFF = 'off'       # 强制关闭（不可开启）
    OPTIONAL = 'optional'   # 可选（允许开关）


class RunningModel(Enum):
    """运行模型枚举"""
    ORIGINAL = 'ORIGINAL'      # 原有模型（完全自动）
    NEW = 'NEW'                # 现有模型（手动开仓+自动管理）
    HYBRID = 'HYBRID'          # 混合模型（自动+手动）


# ============== 功能开关配置 (FEATURE_SWITCHES) ==============
FEATURE_SWITCHES = {
    # ===== 环节1: 运行模型选择 =====
    'running_model': {
        'mode': RunningModel.ORIGINAL.value,  # 当前运行模型
        'description': '运行模型选择: ORIGINAL(完全自动) / NEW(手动开仓+自动管理) / HYBRID(混合模式)'
    },
    
    # ===== 环节2: 状态同步 =====
    'state_sync': {
        'enabled': True,                    # 总开关
        'reconcile_on_startup': True,       # 启动时状态校验
        'claim_orphan_positions': False,    # 认领孤立仓位（原有模型禁用）
        'reset_on_flag': True,              # reset标志重置
        'description': '状态同步功能，用于启动时校验和同步策略状态'
    },
    
    # ===== 环节3: 信号检测 =====
    'signal_detection': {
        'enabled': True,                    # 总开关（不可关闭）
        'dmr_crossover': True,              # DMR零轴穿越（核心信号，不可关闭）
        'position_correction': True,        # 仓位纠偏
        'dual_timeframe_filter': True,      # 双时间框架过滤（仅短期策略）
        'description': '信号检测功能，包括DMR穿越、仓位纠偏和双时间框架过滤'
    },
    
    # ===== 环节4: 订单执行 =====
    'order_execution': {
        'enabled': True,                    # 总开关
        'auto_open': True,                  # 自动开仓（原有模型开启）
        'auto_close': True,                 # 自动平仓
        'retry_on_failure': True,           # 失败重试
        'max_retries': 3,                   # 最大重试次数
        'description': '订单执行功能，控制自动开仓、平仓和重试机制'
    },
    
    # ===== 环节5: 追踪止损与风险管理 =====
    'risk_management': {
        'trailing_stop': {
            'enabled': True,                # 追踪止损总开关
            'k_line_rule': True,            # 6-K线规则
            'atr_dynamic': True,            # ATR动态止损
            'description': '追踪止损功能，使用6-K线规则和ATR动态止损'
        },
        'risk_limits': {
            'enabled': True,                # 风险限制总开关（建议保持开启）
            'daily_loss_limit': True,       # 日损失限制
            'max_drawdown_limit': True,     # 最大回撤限制
            'trade_frequency_limit': True,  # 交易频率限制
            'description': '风险限制功能，包括日损失、最大回撤和交易频率限制'
        }
    }
}


# ============== 持仓接管配置 (POSITION_CLAIM_CONFIG) ==============
POSITION_CLAIM_CONFIG = {
    # 持仓接管模式
    'claim_mode': 'DISABLED',  # 'DISABLED' / 'ENABLED' / 'HYBRID'
    
    # 持仓信息推断方式
    'infer_method': 'ORDER_HISTORY',  # 'ORDER_HISTORY' / 'DMR_SIGNAL' / 'MARKET_PRICE'
    
    # 最小持仓量（用于过滤小额持仓）
    'min_position_amount': 0.001,
    
    # 检测间隔（秒）
    'detection_interval': 60,
    
    # 订单历史查询深度
    'order_history_limit': 50,
    
    # 混合模式下的优先级
    'hybrid_priority': 'EXTERNAL',  # 'EXTERNAL' / 'AUTO'
    
    # 自动开仓延迟（秒），避免与外部开仓冲突
    'auto_open_delay': 60,
    
    # 描述
    'description': '持仓接管配置，用于检测和接管外部持仓'
}


# ============== 开关变更回调机制 ==============
_feature_switch_callbacks: List[Callable] = []


def register_switch_change_callback(callback_func: Callable):
    """
    注册开关变更回调函数
    
    Args:
        callback_func: 回调函数，接收参数 (switch_path, old_value, new_value)
    """
    _feature_switch_callbacks.append(callback_func)
    logger.info(f"✅ 已注册开关变更回调: {callback_func.__name__}")


def update_feature_switch(switch_path: str, new_value: Any):
    """
    运行时更新功能开关
    
    Args:
        switch_path: 开关路径，如 'signal_detection.position_correction'
        new_value: 新值
        
    Example:
        update_feature_switch('order_execution.auto_open', False)
    """
    keys = switch_path.split('.')
    target = FEATURE_SWITCHES
    
    # 定位到目标开关
    for key in keys[:-1]:
        if key not in target:
            raise KeyError(f"开关路径不存在: {switch_path}")
        target = target[key]
    
    final_key = keys[-1]
    if final_key not in target:
        raise KeyError(f"开关路径不存在: {switch_path}")
    
    old_value = target[final_key]
    target[final_key] = new_value
    
    # 触发回调
    for callback in _feature_switch_callbacks:
        try:
            callback(switch_path, old_value, new_value)
        except Exception as e:
            logger.error(f"❌ 开关变更回调执行失败: {e}")
    
    logger.info(f"📡 开关更新: {switch_path} = {old_value} → {new_value}")


def get_feature_switch(switch_path: str) -> Any:
    """
    获取功能开关的当前值
    
    Args:
        switch_path: 开关路径，如 'signal_detection.position_correction'
        
    Returns:
        开关的当前值
    """
    keys = switch_path.split('.')
    target = FEATURE_SWITCHES
    
    for key in keys:
        if key not in target:
            raise KeyError(f"开关路径不存在: {switch_path}")
        target = target[key]
    
    return target


def update_position_claim_config(config_path: str, new_value: Any):
    """
    运行时更新持仓接管配置
    
    Args:
        config_path: 配置路径，如 'claim_mode'
        new_value: 新值
    """
    if config_path not in POSITION_CLAIM_CONFIG:
        raise KeyError(f"配置路径不存在: {config_path}")
    
    old_value = POSITION_CLAIM_CONFIG[config_path]
    POSITION_CLAIM_CONFIG[config_path] = new_value
    
    logger.info(f"📡 持仓接管配置更新: {config_path} = {old_value} → {new_value}")


def get_position_claim_config(config_path: str) -> Any:
    """
    获取持仓接管配置的当前值
    
    Args:
        config_path: 配置路径，如 'claim_mode'
        
    Returns:
        配置的当前值
    """
    if config_path not in POSITION_CLAIM_CONFIG:
        raise KeyError(f"配置路径不存在: {config_path}")
    
    return POSITION_CLAIM_CONFIG[config_path]


SAFE_FALLBACK_ACTIVE = False


def _apply_safe_defaults():
    """状态不可信时的安全态：允许自动平仓(降风险)，禁止自动开仓(加风险)。"""
    global SAFE_FALLBACK_ACTIVE
    SAFE_FALLBACK_ACTIVE = True
    FEATURE_SWITCHES['running_model']['mode'] = RunningModel.ORIGINAL.value
    FEATURE_SWITCHES['state_sync']['claim_orphan_positions'] = False
    FEATURE_SWITCHES['order_execution']['auto_open'] = False
    FEATURE_SWITCHES['order_execution']['auto_close'] = True
    POSITION_CLAIM_CONFIG['claim_mode'] = 'DISABLED'


def save_config():
    """
    保存当前配置到文件
    """
    try:
        # 确保目录存在
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        
        # 准备要保存的配置
        config_data = {
            'running_model': FEATURE_SWITCHES['running_model']['mode'],
            'claim_orphan_positions': FEATURE_SWITCHES['state_sync']['claim_orphan_positions'],
            'auto_open': FEATURE_SWITCHES['order_execution']['auto_open'],
            'auto_close': FEATURE_SWITCHES['order_execution']['auto_close'],
            'claim_mode': POSITION_CLAIM_CONFIG['claim_mode']
        }
        
        # 原子写入并回读校验：磁盘满/无权限时必须返回 False，
        # 否则 CLI 会报告“已切换”而下一个进程仍读到旧的自动模式。
        import os as _os
        import tempfile as _tempfile
        fd, tmp = _tempfile.mkstemp(dir=str(CONFIG_DIR), prefix='.model_state.', suffix='.tmp')
        try:
            with _os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, indent=2, ensure_ascii=False)
                f.flush()
                _os.fsync(f.fileno())
            _os.replace(tmp, str(MODEL_STATE_FILE))
        except BaseException:
            try:
                _os.unlink(tmp)
            except OSError:
                pass
            raise

        with open(MODEL_STATE_FILE, 'r', encoding='utf-8') as f:
            written = json.load(f)
        if written != config_data:
            logger.error("❌ 配置回读校验失败，磁盘内容与预期不符: %s", MODEL_STATE_FILE)
            return False

        logger.info(f"💾 配置已保存到: {MODEL_STATE_FILE}")
        return True
        
    except Exception as e:
        logger.error(f"❌ 保存配置失败: {e}")
        return False


def load_config():
    """
    从文件加载配置
    """
    try:
        if not MODEL_STATE_FILE.exists():
            logger.warning(
                "⚠️  运行模式状态文件不存在(%s)，回落到安全模式：禁止自动开仓。"
                " 如需自动开仓，请显式执行 switch_model.py 初始化。", MODEL_STATE_FILE)
            _apply_safe_defaults()
            return False
        
        # 从文件读取配置
        with open(MODEL_STATE_FILE, 'r', encoding='utf-8') as f:
            config_data = json.load(f)
        if not isinstance(config_data, dict):
            raise ValueError('运行模式状态文件的顶层结构不是对象')
        
        # 应用配置。auto_open 缺失时默认 False：状态文件损坏或字段缺失
        # 绝不能让策略回到“全自动开仓”，那是最危险的默认值。
        FEATURE_SWITCHES['running_model']['mode'] = config_data.get('running_model', RunningModel.ORIGINAL.value)
        FEATURE_SWITCHES['state_sync']['claim_orphan_positions'] = bool(config_data.get('claim_orphan_positions', False))
        FEATURE_SWITCHES['order_execution']['auto_open'] = bool(config_data.get('auto_open', False))
        FEATURE_SWITCHES['order_execution']['auto_close'] = bool(config_data.get('auto_close', False))
        POSITION_CLAIM_CONFIG['claim_mode'] = config_data.get('claim_mode', 'DISABLED')
        
        global SAFE_FALLBACK_ACTIVE
        SAFE_FALLBACK_ACTIVE = False
        logger.info(f"📂 已从文件加载配置: 当前模型 = {config_data.get('running_model')}")
        return True
        
    except Exception as e:
        logger.error("❌ 加载配置失败，回落到安全模式(禁止自动开仓): %s", e)
        _apply_safe_defaults()
        return False


def load_model_preset(model: RunningModel):
    """
    加载预设的运行模型配置
    
    Args:
        model: 运行模型枚举
    """
    logger.info(f"🔄 正在切换到运行模型: {model.value}")
    
    if model == RunningModel.ORIGINAL:
        # 模型1: 原有模型（完全自动）
        FEATURE_SWITCHES['running_model']['mode'] = RunningModel.ORIGINAL.value
        FEATURE_SWITCHES['state_sync']['claim_orphan_positions'] = False
        FEATURE_SWITCHES['order_execution']['auto_open'] = True
        FEATURE_SWITCHES['order_execution']['auto_close'] = True
        POSITION_CLAIM_CONFIG['claim_mode'] = 'DISABLED'
        logger.info("✅ 已切换到模型1: 完全自动模式")
        
    elif model == RunningModel.NEW:
        # 模型2: 现有模型（手动开仓+自动管理）
        FEATURE_SWITCHES['running_model']['mode'] = RunningModel.NEW.value
        FEATURE_SWITCHES['state_sync']['claim_orphan_positions'] = True
        FEATURE_SWITCHES['order_execution']['auto_open'] = False
        FEATURE_SWITCHES['order_execution']['auto_close'] = True
        POSITION_CLAIM_CONFIG['claim_mode'] = 'ENABLED'
        logger.info("✅ 已切换到模型2: 手动开仓+自动管理模式")
        
    elif model == RunningModel.HYBRID:
        # 模型3: 混合模型（自动+手动）
        FEATURE_SWITCHES['running_model']['mode'] = RunningModel.HYBRID.value
        FEATURE_SWITCHES['state_sync']['claim_orphan_positions'] = True
        FEATURE_SWITCHES['order_execution']['auto_open'] = True
        FEATURE_SWITCHES['order_execution']['auto_close'] = True
        POSITION_CLAIM_CONFIG['claim_mode'] = 'HYBRID'
        logger.info("✅ 已切换到模型3: 混合模式")
        
    else:
        raise ValueError(f"未知的运行模型: {model}")
    
    # 保存配置到文件。保存失败必须让调用方知道，否则 CLI 会谎报“已切换”，
    # 而下一个进程仍会读到磁盘上的旧模式。
    if not save_config():
        raise RuntimeError(
            '运行模式已在内存中切换，但写入 %s 失败；重启后仍会使用旧模式。'
            % MODEL_STATE_FILE)


def validate_switches():
    """
    验证开关配置的一致性和依赖关系
    
    Returns:
        (is_valid, error_messages)
    """
    errors = []
    
    # 检查1: 关闭signal_detection但开启order_execution
    if not FEATURE_SWITCHES['signal_detection']['enabled'] and \
       FEATURE_SWITCHES['order_execution']['enabled']:
        errors.append("⚠️ 警告: 关闭信号检测但开启订单执行，将自动禁用订单执行")
        FEATURE_SWITCHES['order_execution']['enabled'] = False
    
    # 检查2: 同时关闭k_line_rule和atr_dynamic
    ts = FEATURE_SWITCHES['risk_management']['trailing_stop']
    if not ts['k_line_rule'] and not ts['atr_dynamic']:
        errors.append("⚠️ 警告: K线规则和ATR止损均关闭，将自动禁用追踪止损")
        ts['enabled'] = False
    
    # 检查3: 模型一致性检查
    model = FEATURE_SWITCHES['running_model']['mode']
    claim_mode = POSITION_CLAIM_CONFIG['claim_mode']
    auto_open = FEATURE_SWITCHES['order_execution']['auto_open']
    claim_orphan = FEATURE_SWITCHES['state_sync']['claim_orphan_positions']
    
    if model == RunningModel.ORIGINAL.value:
        if claim_mode != 'DISABLED':
            errors.append(f"⚠️ 模型1配置不一致: claim_mode应为DISABLED，当前为{claim_mode}")
        if claim_orphan:
            errors.append(f"⚠️ 模型1配置不一致: claim_orphan_positions应为False")
        if not auto_open and not SAFE_FALLBACK_ACTIVE:
            errors.append(f"⚠️ 模型1配置不一致: auto_open应为True")
        elif not auto_open:
            errors.append(
                "ℹ️ 运行模式状态不可信，已进入安全回退：禁止自动开仓。"
                "确认状态文件后请执行 switch_model.py 重新初始化。")
    
    elif model == RunningModel.NEW.value:
        if claim_mode != 'ENABLED':
            errors.append(f"⚠️ 模型2配置不一致: claim_mode应为ENABLED，当前为{claim_mode}")
        if not claim_orphan:
            errors.append(f"⚠️ 模型2配置不一致: claim_orphan_positions应为True")
        if auto_open:
            errors.append(f"⚠️ 模型2配置不一致: auto_open应为False")
    
    elif model == RunningModel.HYBRID.value:
        if claim_mode != 'HYBRID':
            errors.append(f"⚠️ 模型3配置不一致: claim_mode应为HYBRID，当前为{claim_mode}")
        if not claim_orphan:
            errors.append(f"⚠️ 模型3配置不一致: claim_orphan_positions应为True")
        if not auto_open:
            errors.append(f"⚠️ 模型3配置不一致: auto_open应为True")
    
    is_valid = len(errors) == 0
    return is_valid, errors


def print_switches_status():
    """打印所有开关的当前状态"""
    logger.info("🔵 ========== 功能开关状态 ==========")
    logger.info(f"🔵 运行模型: {FEATURE_SWITCHES['running_model']['mode']}")
    logger.info(f"🔵 state_sync.enabled = {FEATURE_SWITCHES['state_sync']['enabled']}")
    logger.info(f"🔵 state_sync.claim_orphan_positions = {FEATURE_SWITCHES['state_sync']['claim_orphan_positions']}")
    logger.info(f"🔵 signal_detection.enabled = {FEATURE_SWITCHES['signal_detection']['enabled']}")
    logger.info(f"🔵 order_execution.enabled = {FEATURE_SWITCHES['order_execution']['enabled']}")
    logger.info(f"🔵 order_execution.auto_open = {FEATURE_SWITCHES['order_execution']['auto_open']}")
    logger.info(f"🔵 order_execution.auto_close = {FEATURE_SWITCHES['order_execution']['auto_close']}")
    logger.info(f"🔵 risk_management.trailing_stop.enabled = {FEATURE_SWITCHES['risk_management']['trailing_stop']['enabled']}")
    logger.info(f"🔵 持仓接管模式: {POSITION_CLAIM_CONFIG['claim_mode']}")
    logger.info(f"🔵 持仓推断方式: {POSITION_CLAIM_CONFIG['infer_method']}")
    logger.info("🔵 ====================================")


# 模块加载时自动加载已保存的配置
load_config()

# 自动验证配置
is_valid, errors = validate_switches()
if not is_valid:
    for error in errors:
        logger.warning(error)
