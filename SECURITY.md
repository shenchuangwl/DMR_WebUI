# 安全说明与风险声明

> 本文件为开源发布前的安全基线说明。**使用本项目前请完整阅读。**

## ⚠️ 首要风险提示

本项目是**可在真实交易所账户下达订单的量化交易系统**，运行不当会造成**真实资金损失**。
它仅供研究与学习使用，**不构成任何投资建议**。使用者需自行承担全部资金风险。

---

## 1. 凭证管理（必读）

### 1.1 凭证注入方式

本项目**不在源码中存放任何 API 密钥**。凭证一律通过环境变量注入：

```bash
export BINANCE_API_KEY=<your_key>
export BINANCE_API_SECRET=<your_secret>
```

或复制 `.env.example` 为 `.env` 后填写（`.env` 已被 `.gitignore` 忽略）。

配置文件读取逻辑：

```python
API_KEY = os.environ.get('BINANCE_API_KEY', '')
API_SECRET = os.environ.get('BINANCE_API_SECRET', '')
```

### 1.2 空凭证保护

未配置凭证时，`validate_config(require_credentials=True)` 会在启动早期抛出明确错误并终止，
而非等到首次下单才失败。这是刻意设计，避免"程序看起来在正常运行"的假象。

### 1.3 被忽略的敏感文件

以下文件包含或可能包含凭证，**已在 `.gitignore` 中忽略，切勿强制提交**：

```
config/config.py          config/first_config.py      config/testnet_config.py
bit/config/config.py      bit/config/bit_config.py    .env  .env.*
config/templates/         config/config.py.bak
```

> ⚠️ `config/config_templates.py` 的 `save_current_as_template()` 与 `load_template()` 会把
> `config.py` **整份复制**到 `config/templates/<名称>.py` 和 `config/config.py.bak`。
> 若你把密钥直接写死在 `config.py` 中（而非使用环境变量），这些副本同样含有凭证。
> 两条路径均已纳入忽略规则；这也是**推荐用环境变量注入**的又一理由。

提交前自检：

```bash
git check-ignore -v config/config.py          # 应命中忽略规则
git diff --cached | grep -cE "[A-Za-z0-9]{64}" # 应为 0
```

> 若密钥曾意外提交，**删除文件无法清除历史**。必须立即在交易所撤销并轮换密钥。

---

## 2. 账户设置写入（⚠️ 高风险，默认关闭）

程序具备改写**交易所账户级别设置**的能力，默认**不执行**：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DMR_APPLY_ACCOUNT_SETTINGS` | `0`（关闭） | 置 `1` 才会改写账户设置 |
| `DMR_LEVERAGE` | `10` | 杠杆倍数 |
| `DMR_MARGIN_TYPE` | `CROSSED` | 保证金模式（全仓） |

**开启前请理解以下后果**：

1. **10 倍杠杆**的强平距离约为 5 倍杠杆的**一半**，价格反向波动约 10% 即可能触及强平；
2. **CROSSED 全仓模式**下，单一仓位的亏损会波及**整个账户余额**，而非仅限该仓位保证金；
3. 该设置作用于**账户级别**，会影响同一账户下**其他策略或手动持仓**的风控参数。

开启时程序会在控制台打印明确警告。建议**先在测试网验证**，确认无误后再在实盘开启。

---

## 3. 测试脚本与实盘写操作

公开仓库**不包含**内部测试目录（`A_TEST/`、`bit/B_TEST/` 等），因此仓库内
不存在会自动连接实盘账户的测试脚本。

仓库内提供的测试入口只有两个，均不触碰真实账户：

| 入口 | 说明 |
|---|---|
| `tests/test_security_fixes.py` | 纯离线回归测试，不需要凭证、不发起任何网络请求 |
| `tests/offline/` | 内核级禁网（seccomp）的离线验证套件，见下方限制说明 |

> ⚠️ **通用原则**：任何会构造真实 `ccxt` 交易所实例的脚本，都必须在
> `DMR_ALLOW_LIVE` 不为 `1` 时拒绝发起下单/撤单等写请求。若你自行添加
> 测试脚本，请遵循这一约定，并优先在**没有配置实盘凭证的 shell** 中运行。

推荐的安全验证方式：

```bash
# 离线回归测试（无网络、无凭证）
./venv/bin/python tests/test_security_fixes.py

# 内核级禁网下重跑同一套测试
./venv/bin/python tests/offline/no_network.py ./venv/bin/python tests/test_security_fixes.py
```

---

## 4. 启动入口的风险等级

| 命令 | 风险 | 说明 |
|---|---|---|
| `bash deploy_and_run.sh --yes` | 🟢 低 | 依赖安装 + 禁网离线验证，不读配置、不交易 |
| `python tests/offline/run.py --out <目录>` | 🟢 低 | 离线断言，mock 交易所 |
| `bash start_trading.sh version` / `help` | 🟢 低 | 仅显示信息 |
| `bash start_trading.sh check` / `install` | 🟡 中 | 会创建并安装虚拟环境 |
| `bash start_trading.sh test` | 🟡 中 | **导入真实配置 + 访问公共网络** |
| `bash start_trading.sh fix` | 🟡 中 | 移走 venv 并重建，项目内产生备份 |
| `./start_trading.sh main` / `long` / `short` | 🔴 **高** | **连接实盘账户，可能真实下单** |
| `./bit/start_bit_long_term.sh` 等 | 🔴 **高** | 同上 |
| `./start_trading.sh backtest` | 🟡 中 | 未经审查不应视为完全无副作用 |

---

## 5. 实盘启用前的检查清单

1. 🔑 在交易所为本项目创建**独立 API 密钥**，仅授予必要权限，**关闭提现权限**，并绑定 IP 白名单；
2. 🧪 先在**测试网**完成账户连接、时间同步、下单与撤单的完整联调；
3. 🛡️ 审核风控参数（仓位上限、止损比例、杠杆），确认 `DMR_APPLY_ACCOUNT_SETTINGS` 的取值符合预期；
4. 💰 以**最小资金**灰度运行并人工监控，确认行为符合预期后再逐步放大；
5. 📊 建立独立的监控与告警，不要依赖程序自身日志作为唯一观测手段。

---

## 6. 已知限制

- `risk_management/` 下部分模块为历史遗留实现，使用前请自行审阅；
- 策略状态文件为非原子写入且无跨进程锁，**不建议多实例并发操作同一交易对**；
- 条件单未使用稳定的幂等键，网络超时重试存在重复挂单的可能；
- 交易所接口与限频策略可能随时变更，请以交易所官方文档为准。

---

## 7. 漏洞报告

**请勿通过公开 GitHub Issue 报告安全漏洞。** 公开 Issue 会在修复前把问题暴露给所有人。

请使用 GitHub 的私密漏洞报告渠道：

1. 打开本仓库的 **Security** 标签页；
2. 选择 **Report a vulnerability**（Private vulnerability reporting）；
3. 描述问题、影响范围与复现步骤。

若该渠道未启用，请在仓库 Settings → Code security → 勾选 *Private vulnerability reporting* 后再报告。

无论使用哪种渠道，**请勿粘贴任何真实密钥、账户 ID、订单号或持仓数据**。
