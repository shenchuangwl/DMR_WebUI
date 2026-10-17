# DMR四象限量化策略分析工具

基于最新的币种行情K线数据，结合DMR四象限多周期策略，自动判断当前市场所处的四象限状态（T1-多头趋势、T2-空头趋势、R1-高位震荡、R2-低位震荡），并输出行情判定、仓位建议和策略细节。

## 市场状态判断依据

| 市场状态 | 4H DMR(12) | 1H DMR(26) | 典型用语 | 交易核心 |
|---------|-----------|-----------|---------|---------|
| T1 – 多头趋势 | 负 → 正 | 负 → 正 | "双多" | 做多加码;移动止盈 |
| T2 – 空头趋势 | 正 → 负 | 正 → 负 | "双空" | 做空加码;移动止盈 |
| R1 – 高位震荡 | 负 → 正 | 正 → 负 | "锁空" | 降杠杆持仓;等待共振 |
| R2 – 低位震荡 | 正 → 负 | 负 → 正 | "锁多" | 降杠杆持仓;等待共振 |

## 安装依赖

```bash
pip install -r requirements.txt
```

## 使用方法

### 单一交易对分析

使用`dmr_analysis.py`脚本分析单个交易对的市场状态：

```bash
python3 dmr_analysis.py -s BTC/USDT
```

#### 可选参数

- `-s, --symbol`: 交易对名称，例如 BTC/USDT (默认: NFP/USDT)
- `-4h, --dmr-4h`: 4H DMR周期 (默认: 12)
- `-1h, --dmr-1h`: 1H DMR周期 (默认: 26)
- `-o, --output`: 输出报告文件名 (默认: dmr_analysis_report.txt)
- `-v, --verbose`: 显示详细信息

示例：

```bash
python3 dmr_analysis.py -s ETH/USDT -4h 8 -1h 20 -o eth_analysis.txt
```

### 多交易对批量分析

使用`analyze_markets.py`脚本批量分析多个交易对的市场状态：

```bash
python3 analyze_markets.py
```

默认分析以下交易对：BTC/USDT, ETH/USDT, BNB/USDT, SOL/USDT, XRP/USDT, DOGE/USDT, ADA/USDT, AVAX/USDT, MATIC/USDT, LINK/USDT

#### 可选参数

- `-s, --symbols`: 要分析的交易对列表
- `-4h, --dmr-4h`: 4H DMR周期 (默认: 12)
- `-1h, --dmr-1h`: 1H DMR周期 (默认: 26)
- `-o, --output`: 输出汇总文件名 (默认: market_analysis_summary.csv)
- `-d, --output-dir`: 输出报告目录 (默认: analysis_reports)
- `-v, --verbose`: 显示详细信息

示例：

```bash
python3 analyze_markets.py -s BTC/USDT ETH/USDT BNB/USDT -4h 8 -1h 20
```

## 输出示例

### 单一交易对分析报告

```
当前行情判定：多头趋势行情（T1）
当前仓位建议：浮盈加仓 双多
策略判定细节：4H DMR(12) 持续为正（0.002689），1H DMR(26) 持续为正（0.001234），Enet=200.00USDT
简明说明：T1-多头趋势：建议做多加码，移动止盈。
```

### 多交易对汇总报告

生成的CSV文件包含以下字段：

- symbol: 交易对
- market_state: 市场状态 (T1, T2, R1, R2)
- market_description: 市场描述
- position_advice: 仓位建议
- dmr_4h_value: 4H DMR值
- dmr_1h_value: 1H DMR值
- dmr_4h_transition: 4H DMR转变方向
- dmr_1h_transition: 1H DMR转变方向
- enet_value: 净额/Enet值

## 注意事项

1. 请通过**环境变量**配置 API 密钥（`BINANCE_API_KEY` / `BINANCE_API_SECRET`），
   或复制 `.env.example` 为 `.env` 后填写；**不要把密钥写进任何 `.py` 文件**。
   详见 [SECURITY.md](SECURITY.md) 与 [README.md](README.md)。
2. 该工具仅提供策略参考，实际交易决策请结合其他因素综合判断
3. 建议在使用前先了解DMR四象限策略的基本原理

## 时间周期配置说明

本系统支持灵活的时间周期配置，您可以在 `config/config.py` 文件中修改以下两个时间周期配置：

```python
# 交易时间周期配置
TIMEFRAME_4H = '15m'  # 长周期时间框架
TIMEFRAME_1H = '5m'   # 短周期时间框架
```

### 支持的时间周期

系统支持以下时间周期格式：
- `5m`: 5分钟
- `15m`: 15分钟
- `30m`: 30分钟
- `1h`: 1小时
- `2h`: 2小时
- `4h`: 4小时
- `6h`: 6小时
- `12h`: 12小时
- `1d`: 1天

### 时间周期修改影响

当您修改 `config.py` 中的时间周期配置后，系统会自动适应新的时间周期：

1. 数据获取：系统会按照新的时间周期从交易所获取K线数据
2. 数据重采样：策略中的数据重采样会使用新的时间周期
3. 定时任务：系统会根据新的时间周期自动调整定时任务的执行频率
4. 信号生成：策略会基于新的时间周期生成交易信号

### 注意事项

1. 虽然变量名称仍为 `TIMEFRAME_4H` 和 `TIMEFRAME_1H`，但它们可以设置为任何支持的时间周期
2. 通常建议 `TIMEFRAME_4H` 设置为较长的时间周期，`TIMEFRAME_1H` 设置为较短的时间周期
3. 修改时间周期后，策略的表现可能会有所不同，因为不同的时间周期会产生不同的信号
4. 建议在修改时间周期后进行回测，评估新时间周期下的策略表现

## DMR四象限策略说明

DMR四象限量化策略基于两个不同时间周期的DMR指标值，将市场状态分为四种情况：

- T1(双多趋势): 长周期DMR12>0 且 短周期DMR26>0，做多加码
- T2(双空趋势): 长周期DMR12<0 且 短周期DMR26<0，做空加码
- R1(高位震荡): 长周期DMR12>0 且 短周期DMR26<0，锁空对冲
- R2(低位震荡): 长周期DMR12<0 且 短周期DMR26>0，锁多对冲 