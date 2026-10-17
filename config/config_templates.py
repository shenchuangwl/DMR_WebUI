import json
import os
import re
import shutil
from pathlib import Path

# 模板名白名单：只允许字母、数字、下划线、连字符和点，且不得以点开头。
# 修复前 template_name 被直接拼进路径，"../escaped" 可把含凭证的 config.py
# 复制到模板目录之外（脱离 .gitignore 保护），或用目录外的任意 .py 覆盖 config.py。
_TEMPLATE_NAME_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]*$')


def _template_path(template_name):
    """把模板名解析为模板目录内的路径，越界即拒绝。"""
    name = str(template_name)
    if not _TEMPLATE_NAME_RE.match(name):
        raise ValueError(
            "非法模板名 %r：只允许字母、数字、下划线、连字符和点，且不能以点开头" % template_name)
    if name.endswith('.py'):
        name = name[:-3]
    candidate = (Path(TEMPLATES_DIR) / (name + '.py')).resolve()
    templates_root = Path(TEMPLATES_DIR).resolve()
    if templates_root != candidate.parent:
        raise ValueError("模板名 %r 解析后超出模板目录 %s" % (template_name, templates_root))
    return str(candidate)

# 配置模板目录
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

# 确保模板目录存在
if not os.path.exists(TEMPLATES_DIR):
    os.makedirs(TEMPLATES_DIR)

# 主配置文件路径
MAIN_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")

def save_current_as_template(template_name):
    """将当前配置保存为模板"""
    if not os.path.exists(MAIN_CONFIG_PATH):
        print(f"错误: 主配置文件不存在: {MAIN_CONFIG_PATH}")
        return False
    
    template_path = _template_path(template_name)
    shutil.copy2(MAIN_CONFIG_PATH, template_path)
    print(f"当前配置已保存为模板: {template_name}")
    return True

def load_template(template_name):
    """加载指定的配置模板"""
    template_path = _template_path(template_name)
    
    if not os.path.exists(template_path):
        print(f"错误: 模板不存在: {template_name}")
        return False
    
    # 备份当前配置
    backup_path = f"{MAIN_CONFIG_PATH}.bak"
    shutil.copy2(MAIN_CONFIG_PATH, backup_path)
    
    # 应用模板
    shutil.copy2(template_path, MAIN_CONFIG_PATH)
    print(f"已应用配置模板: {template_name}")
    print(f"原配置已备份到: {backup_path}")
    return True

def list_templates():
    """列出所有可用的配置模板"""
    if not os.path.exists(TEMPLATES_DIR):
        print("没有找到配置模板")
        return []
    
    templates = [f.replace(".py", "") for f in os.listdir(TEMPLATES_DIR) 
                if f.endswith(".py") and os.path.isfile(os.path.join(TEMPLATES_DIR, f))]
    
    if not templates:
        print("没有找到配置模板")
    else:
        print("可用的配置模板:")
        for i, template in enumerate(templates, 1):
            print(f"{i}. {template}")
    
    return templates

def create_quadrant_template(template_name="quadrant_config"):
    """创建包含四象限配置的模板"""
    # 读取当前配置
    with open(MAIN_CONFIG_PATH, 'r') as f:
        current_config = f.read()
    
    # 添加四象限配置
    quadrant_config = '''
# 四象限策略配置
QUADRANT_CONFIG = {
    'T1': {  # 多头趋势
        'name': '多头趋势',
        'description': '4H DMR(12) > 0 且 1H DMR(26) > 0',
        'actions': {
            '4h_neg_to_pos': {'action': 'buy', 'position': 'Long_4H_T1', 'comment': 'T1-4H开多'},
            '4h_pos_to_neg': {'action': 'close', 'position': 'Long_4H_T1', 'comment': 'T1-4H平多'},
            '1h_neg_to_pos': {'action': 'buy', 'position': 'Long_1H_T1', 'comment': 'T1-1H开多'},
            '1h_pos_to_neg': {'action': 'close', 'position': 'Long_1H_T1', 'comment': 'T1-1H平多'}
        }
    },
    'T2': {  # 空头趋势
        'name': '空头趋势',
        'description': '4H DMR(12) < 0 且 1H DMR(26) < 0',
        'actions': {
            '4h_pos_to_neg': {'action': 'sell', 'position': 'Short_4H_T2', 'comment': 'T2-4H开空'},
            '4h_neg_to_pos': {'action': 'close', 'position': 'Short_4H_T2', 'comment': 'T2-4H平空'},
            '1h_pos_to_neg': {'action': 'sell', 'position': 'Short_1H_T2', 'comment': 'T2-1H开空'},
            '1h_neg_to_pos': {'action': 'close', 'position': 'Short_1H_T2', 'comment': 'T2-1H平空'}
        }
    },
    'R1': {  # 高位震荡
        'name': '高位震荡',
        'description': '4H DMR(12) > 0 且 1H DMR(26) < 0',
        'actions': {
            '4h_neg_to_pos': {'action': 'buy', 'position': 'Long_4H_R1', 'comment': 'R1-4H开多'},
            '4h_pos_to_neg': {'action': 'close', 'position': 'Long_4H_R1', 'comment': 'R1-4H平多'},
            '1h_pos_to_neg': {'action': 'sell', 'position': 'Short_1H_R1', 'comment': 'R1-1H开空'},
            '1h_neg_to_pos': {'action': 'close', 'position': 'Short_1H_R1', 'comment': 'R1-1H平空'}
        }
    },
    'R2': {  # 低位震荡
        'name': '低位震荡',
        'description': '4H DMR(12) < 0 且 1H DMR(26) > 0',
        'actions': {
            '4h_pos_to_neg': {'action': 'sell', 'position': 'Short_4H_R2', 'comment': 'R2-4H开空'},
            '4h_neg_to_pos': {'action': 'close', 'position': 'Short_4H_R2', 'comment': 'R2-4H平空'},
            '1h_neg_to_pos': {'action': 'buy', 'position': 'Long_1H_R2', 'comment': 'R2-1H开多'},
            '1h_pos_to_neg': {'action': 'close', 'position': 'Long_1H_R2', 'comment': 'R2-1H平多'}
        }
    }
}

# 风险管理配置
MAX_POSITION_EXPOSURE = 100  # 最大持仓金额
MAX_DAILY_TRADES = 10        # 每日最大交易次数
STOP_LOSS_PERCENT = 2.0      # 止损百分比
TAKE_PROFIT_PERCENT = 5.0    # 止盈百分比
'''
    
    # 创建新模板
    template_path = _template_path(template_name)
    with open(template_path, 'w') as f:
        f.write(current_config + quadrant_config)
    
    print(f"已创建四象限配置模板: {template_name}")
    return True

# 命令行接口
if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("用法: python config_templates.py [命令] [参数]")
        print("命令:")
        print("  list - 列出所有模板")
        print("  save [模板名] - 保存当前配置为模板")
        print("  load [模板名] - 加载指定模板")
        print("  create_quadrant [模板名] - 创建四象限配置模板")
        sys.exit(1)
    
    command = sys.argv[1]
    
    if command == "list":
        list_templates()
    elif command == "save" and len(sys.argv) > 2:
        save_current_as_template(sys.argv[2])
    elif command == "load" and len(sys.argv) > 2:
        load_template(sys.argv[2])
    elif command == "create_quadrant":
        template_name = sys.argv[2] if len(sys.argv) > 2 else "quadrant_config"
        create_quadrant_template(template_name)
    else:
        print("无效的命令或缺少参数")