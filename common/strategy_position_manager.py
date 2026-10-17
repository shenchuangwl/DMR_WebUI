import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Any

_PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from common.atomic_state import StateCorruptError, atomic_write_json, read_json, state_path

class StrategyPositionManager:
    """策略持仓管理器 - 跟踪每个策略的独立持仓"""
    
    def __init__(self, strategy_name: str, symbol: str):
        self.strategy_name = strategy_name
        self.symbol = symbol
        # 修复前该文件位于 /tmp，路径可预测且全局可读：同机其它用户可读取持仓
        # 或篡改内容。现在放在项目内 0700 的私有运行时目录下，权限 0600。
        self.position_file = str(state_path(
            f"{strategy_name}_position_{symbol.replace('/', '_')}.json"))
        self.position = self._load_position()
    
    def _load_position(self) -> Optional[Dict[str, Any]]:
        """从文件加载策略持仓"""
        # 修复前这里吞掉 JSONDecodeError 并返回 None，等于把"文件损坏"
        # 当成"确认空仓"：止损单 ID 与持仓数量一起丢失，重启后可能重复开仓。
        try:
            return read_json(self.position_file, None)
        except StateCorruptError as e:
            raise RuntimeError(
                "%s 策略持仓状态文件损坏，拒绝以'空仓'继续运行；"
                "请先人工核对交易所真实持仓后再启动: %s" % (self.strategy_name, e)
            ) from e
    
    def _save_position(self):
        """保存策略持仓到文件"""
        # 原子写：open(...,'w') 会先把文件截断为 0，崩溃即留下半截 JSON。
        try:
            atomic_write_json(self.position_file, self.position)
        except Exception as e:
            print(f"保存{self.strategy_name}策略持仓失败: {e}")
            raise
    
    def update_position(self, side: str, amount: float, price: float, action: str = 'open'):
        """更新策略持仓"""
        if action == 'open':
            self.position = {
                'side': side,
                'amount': amount,
                'entry_price': price,
                'timestamp': datetime.now().isoformat(),
                'strategy': self.strategy_name
            }
        elif action == 'close':
            self.position = None
        
        self._save_position()
    
    def get_position(self) -> Optional[Dict[str, Any]]:
        """获取策略持仓"""
        return self.position
    
    def has_position(self, side: str = None) -> bool:
        """检查是否有持仓"""
        if not self.position:
            return False
        if side:
            return self.position.get('side') == side
        return True
    
    def clear_position(self):
        """清除策略持仓记录"""
        self.position = None
        if os.path.exists(self.position_file):
            os.remove(self.position_file)