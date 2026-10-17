# BIT 策略快速启动指南

## ✅ 问题已修复

`ModuleNotFoundError: No module named 'bit'` 问题已完全解决。

---

## 🚀 三步启动策略

### 方法 1: 使用启动脚本（推荐）

```bash
# 1. 进入项目目录
cd <项目父目录>/DMR_binance_Version_V14_A1

# 2. 运行短周期策略
./bit/start_bit_short_term.sh

# 或运行长周期策略
./bit/start_bit_long_term.sh
```

### 方法 2: 手动启动

```bash
# 1. 进入项目目录
cd <项目父目录>/DMR_binance_Version_V14_A1

# 2. 激活虚拟环境
source venv/bin/activate

# 3. 运行策略
python bit/bit_short_term_main.py
# 或
python bit/bit_long_term_main.py
```

---

## 🔍 验证修复

### 快速验证
```bash
cd <项目父目录>/DMR_binance_Version_V14_A1
python bit/quick_verify.py
```

### 完整测试
```bash
cd <项目父目录>/DMR_binance_Version_V14_A1
python bit/test_imports.py
```

---

## 📋 修复内容摘要

### 创建的文件
1. ✅ `bit/strategy/bit_short_term/__init__.py`
2. ✅ `bit/strategy/bit_long_term/__init__.py`
3. ✅ `bit/utils/__init__.py`
4. ✅ `bit/common/__init__.py`
5. ✅ `bit/start_bit_short_term.sh` - 短周期启动脚本
6. ✅ `bit/start_bit_long_term.sh` - 长周期启动脚本
7. ✅ `bit/test_imports.py` - 导入测试脚本
8. ✅ `bit/quick_verify.py` - 快速验证脚本

### 修改的文件
1. ✅ `bit/bit_short_term_main.py` - 添加路径设置
2. ✅ `bit/bit_long_term_main.py` - 添加路径设置

---

## ⚠️ 重要提示

### 运行前检查
- ✅ 确保虚拟环境已创建：`python3 -m venv venv`
- ✅ 确保依赖已安装：`pip install -r requirements.txt`
- ✅ 确保配置文件正确
- ✅ 确保 API 密钥已设置

### 推荐做法
1. **优先使用启动脚本**：自动处理环境和依赖
2. **从项目根目录运行**：确保路径正确
3. **激活虚拟环境**：避免依赖冲突

---

## 📞 遇到问题？

### 常见错误及解决

| 错误 | 原因 | 解决方案 |
|------|------|----------|
| `ModuleNotFoundError` | 路径或包结构问题 | 使用启动脚本运行 |
| `Permission denied` | 脚本无执行权限 | `chmod +x bit/start_*.sh` |
| `No module named 'ccxt'` | 依赖未安装 | `pip install -r requirements.txt` |
| 无输出或挂起 | 程序正常运行中 | 检查日志文件 |

### 查看日志
```bash
# 短周期策略日志
tail -f bit/B_LOG/*.log

# 长周期策略日志
tail -f bit/B_LOG/*.log
```

---

## 📚 详细文档

- [完整修复说明](./BIT策略运行问题修复说明.md)
- 配置说明：查看 `bit/config/` 目录
- 策略说明：查看 `bit/B_MD/` 目录

---

**祝交易顺利！** 🎯
