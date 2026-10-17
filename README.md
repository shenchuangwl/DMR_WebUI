# DMR_WebUI

<p align="center">
  <strong>DMR Binance USDT-M 策略研究与本地 WebUI 安全运行工作台</strong><br>
  一个入口、两套引擎：根目录 DMR 策略面 + <code>webui/</code> 本地回环替身面
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/License-Apache--2.0-blue.svg"></a>
  <a href="https://www.python.org/downloads/release/python-3120/"><img alt="Python 3.12" src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white"></a>
  <a href="#当前验证状态"><img alt="WebUI standin validated" src="https://img.shields.io/badge/WebUI-standin%20validated-0f766e"></a>
  <a href="#7-binance-api-key-与配置安全"><img alt="Credentials policy" src="https://img.shields.io/badge/credentials-never%20commit-critical"></a>
</p>

> **项目定位**：DMR_WebUI 将 DMR Binance USDT-M 合约策略代码与 WebUI 子工程放在同一份
> 可复核的 Apache-2.0 源码发布中。根目录 DMR 面向策略研究/回测和经明确配置后的 Binance
> 访问；`webui/` 当前可验证的边界是只绑定回环地址的 `standin` 替身环境。

> ⚠️ **风险提示**：根 DMR 代码具备连接交易所和提交真实订单的路径。合约交易可能造成全部
> 本金损失。本项目仅供研究和学习，不构成投资建议，也不代表实盘、测试网或收益已经验证。
> 任何真实资金操作前，请自行审查代码并先用测试环境或极小资金做完整联调。

### Quick facts

| 项目 | 当前可核对事实 |
| --- | --- |
| 运行面 | 根 DMR 策略面 + `webui/` 内核/控制台/standin 面，两个环境独立管理 |
| 默认 WebUI 环境 | `DMR_ENV=standin`、停止开仓、仅监听 `127.0.0.1` |
| WebUI 端口 | standin `18220` · kernel `18200` · console `18210` |
| 外部输入 | `127.0.0.1:18080` 名单服务为外部依赖，不由本仓库提供 |
| 公开发布 | 不包含 API Key、Secret、密码、令牌、账本、交易记录、虚拟环境或运行数据库 |

## 导航

- [主要功能与边界](#1-主要功能与边界)
- [架构与服务端口](#架构与服务端口)
- [技术栈与运行环境](#2-技术栈与运行环境)
- [目录结构与公开范围](#3-目录结构与公开范围)
- [安装根 DMR](#4-安装根-dmr不启动交易)
- [安装与运行 WebUI](#5-安装与运行-webui-替身环境)
- [一键部署、清理、备份与恢复](#6-一键部署清理备份与恢复)
- [Binance API Key 与配置安全](#7-binance-api-key-与配置安全)
- [验证、故障排查与维护](#8-验证故障排查与维护)
- [许可证与归属](#9-许可证与归属)

## 1. 主要功能与边界

根 DMR 代码提供 DMR 双时间框架策略、移动止损/止盈、阶梯止盈、持久化风控、状态原子写入、
单实例保护和 BIT 策略链。WebUI 子工程提供交易内核 `dmr_kernel`、NiceGUI 控制台
`dmr_console`、本地替身交易所 `dmr_standin`、部署/清理编排及回环端到端测试。

| 能力 | 说明 |
| --- | --- |
| DMR 策略面 | 双时间框架、止盈止损、持久化风控、状态原子写入和单实例保护 |
| WebUI 内核 | 本地状态、风险门禁、调度和 API；默认只服务 standin 回环环境 |
| Web 控制台 | NiceGUI 登录、状态、行情、持仓/交易视图和运维页面 |
| 部署/清理 | 预览、显式确认、锁文件校验、保护清单、可恢复隔离和证据结果 |
| 测试 | 禁网守卫、内核/控制台测试、替身/回环 e2e，以及可选 Chrome/Playwright UI 测试 |

当前公开验收只覆盖默认的 `DMR_ENV=standin`；源码还枚举了 testnet、mainnet_shadow 和
mainnet_live 模式，但这些模式未在本次发布中验证，也不应被当作实盘交易系统。根目录旧入口（如 `start_trading.sh`、`first_*_main.py`）不由 WebUI
进程管理，不能与 WebUI 的运行时环境混用。

根 DMR 入口本身不提供本地 HTTP 监听端口，主要通过 `ccxt`/`requests` 访问外部交易所；
`18220/18200/18210` 仅属于 WebUI 替身/内核/控制台，`18080` 是外部本机名单服务。

## 架构与服务端口

### 一个入口、两套引擎

```text
deploy_all.sh
├── DMR：deploy_and_run.sh（建环境 + 离线验证，不启动交易）
└── WebUI：standin → kernel → console

cleanup_all.sh
└── 停 WebUI → WebUI 清理 → DMR 清理
```

| 服务 | 默认地址 | 作用 | 当前状态 |
| --- | --- | --- | --- |
| `dmr_standin` | `127.0.0.1:18220` | 本地替身交易所/回放边界 | ✅ 已验证 |
| `dmr_kernel` | `127.0.0.1:18200` | 内核 API、状态和风险门禁 | ✅ standin 已验证 |
| `dmr_console` | `127.0.0.1:18210` | NiceGUI 控制台 | ✅ 回环已验证 |
| 外部名单服务 | `127.0.0.1:18080` | 可选 OnlyCoin/名单输入 | ⏸ 需按环境验证 |

默认不开仓。不要把 WebUI 端口改成 `0.0.0.0` 直接暴露公网；远程查看应使用 SSH 端口转发，
或按实际部署环境增加认证、反向代理和防火墙规则。

## 2. 技术栈与运行环境

| 部分 | 实际实现 |
| --- | --- |
| 根 DMR | Python 3.12、ccxt、python-binance、pandas、numpy、aiohttp、文件锁 |
| WebUI 内核/替身 | Python 3.12、FastAPI、uvicorn、SQLAlchemy、Alembic、APScheduler、ccxt |
| Web 控制台 | NiceGUI；事件流使用项目锁定的 WebSocket/HTTP 依赖 |
| 测试 | pytest；内核/控制台禁网守卫；回环 e2e；可选本机 Chrome/Playwright UI 测试 |
| 平台 | 已按 Linux + CPython 3.12 验证；其它系统/架构需按实际环境重新生成锁文件 |

根依赖声明在 `requirements.txt` 与 `requirements.lock`。WebUI 依赖在
`webui/requirements/*.in` 和带哈希的 `*.lock` 文件。锁文件只覆盖其中声明的平台制品，
不要用关闭哈希校验的方式绕过安装失败。

## 3. 目录结构与公开范围

```text
.
├── config/                  # 公开示例和无凭证配置模块；真实配置不入库
├── common/ strategy/       # DMR 策略、止盈止损、风控和状态组件
├── execution/ risk_management/ data/ utils/ monitoring/ src/
├── bit/                     # BIT 策略链及公开模板
├── tests/                   # 离线/安全回归测试（内部测试目录除外）
├── start_trading.sh        # 根 DMR 入口（可能访问 Binance，需自行配置）
├── deploy_and_run.sh       # 根 DMR 环境部署，不自动启动交易
└── webui/                   # WebUI 内核、控制台、替身、部署脚本和公开操作手册
    ├── dmr_kernel/ dmr_console/ dmr_standin/
    ├── deploy/ requirements/ tests/
    └── docs/                 # 公开部署/清理/运行说明
```

发布时明确排除：根 `A_LOG/`、`A_MD/`、`A_PY/`、`A_TEST/`，`bit/B_LOG/`、`bit/B_MD/`、
`bit/B_PY/`、`bit/B_TEST/`；WebUI 的 `.git/`、`.venv-*`、`var/`、`backup/`、账本/日志/缓存、
`docs/acceptance/`、`docs/plans/` 和内部验收/进度记录；所有 `.env`（仅保留无凭证模板）、
真实 `config/*.py`、运行状态 JSON、数据库、截图、临时文件和构建产物。规则见根目录和
`webui/.gitignore`，必要的源码文件例外会在发布清单中单独审计。

## 4. 安装根 DMR（不启动交易）

```bash
git clone https://github.com/shenchuangwl/DMR_WebUI.git
cd DMR_WebUI
python3.12 -m venv venv
./venv/bin/python -m pip install -r requirements.txt
./venv/bin/python -m pip check

# Linux + CPython 3.12 且需要严格锁校验时
./venv/bin/python -m pip install --require-hashes -r requirements.lock

# 只复制无凭证模板；填好的文件必须留在本机并保持 0600
cp .env.example .env
cp config/config.example.py config/config.py
cp config/first_config.example.py config/first_config.py
cp config/testnet_config.example.py config/testnet_config.py
cp bit/config/bit_config.example.py bit/config/bit_config.py
cp bit/config/config.example.py bit/config/config.py
cp bit/data/config/model_state.example.json bit/data/config/model_state.json
chmod 600 .env config/config.py config/first_config.py config/testnet_config.py \
  bit/config/bit_config.py bit/config/config.py bit/data/config/model_state.json
```

示例配置中的数值只是中性占位值。`config/config.py`、BIT 私有配置和运行状态文件已由
`.gitignore` 保护，不要用 `git add -f` 绕过。

根部署器只做环境安装和离线验证；离线验证需要系统可加载 `libseccomp.so.2`（Ubuntu
通常由 `libseccomp2` 提供），缺失时会 fail-closed：

```bash
bash deploy_and_run.sh --help
bash deploy_and_run.sh --yes
```

真实策略入口（`start_trading.sh short|long` 等）可能连接 Binance 并提交订单，本文不声称已
完成真实账户、测试网、WebSocket、下单、撤单或止损触发验证。
根 DMR 入口通常以前台进程运行，停止应使用 Ctrl-C/SIGINT 或部署环境的精确进程管理，避免宽泛
`pkill`；本地进程退出不会自动撤销已经挂在交易所的条件单，须在交易所侧核对。

## 5. 安装与运行 WebUI 替身环境

```bash
cd webui
deploy/deploy_webui.sh --preview --tests none
deploy/deploy_webui.sh --execute --yes --tests both
deploy/dev_start.sh
deploy/dev_status.sh
```

启动顺序固定为替身交易所 → 内核（由 `supervise.sh` 守护）→ 控制台，且默认保持停止开仓。
默认只绑定回环：替身 `127.0.0.1:18220`、内核 `127.0.0.1:18200`、控制台
`127.0.0.1:18210`。浏览器访问 `http://127.0.0.1:18210`；远程访问需用 SSH 端口转发，
不得改为 `0.0.0.0`。停止和状态命令：

```bash
deploy/dev_stop.sh
deploy/dev_status.sh
```

WebUI 默认名单 URL 是 `http://127.0.0.1:18080/api/v1/onlycoin/live`，该服务不由本仓库
提供；部署环境没有该服务时应改用测试夹具/回放配置。18080 不是 WebUI 自带端口。

测试入口必须使用项目自己的解释器：

```bash
./run_tests.sh kernel
./run_tests.sh console
./run_tests.sh e2e
./run_tests.sh ui       # 需要本机 Chrome/Playwright，不在禁网包装下
```

公开包不携带从外部名单接口录制的 `onlycoin_live_sample.json` 或运行期行情 CSV；运行
`e2e/ui` 前请在本机准备不含敏感信息的测试夹具/回放数据，或按实际环境调整测试。这样不会
把实时/历史选币数据误当作发布资产。

## 6. 一键部署、清理、备份与恢复

WebUI 的操作说明在 `webui/docs/部署手册.md`、`webui/docs/清理手册.md`、
`webui/docs/一键部署清理.md`；程序文件在 `webui/deploy/`。采用“一个入口、两套引擎”：
聚合脚本只编排顺序，不把两边内部逻辑揉成一个大脚本。

```bash
# 预览：不创建环境、不启动服务、不停止服务、不移动文件
webui/deploy/deploy_all.sh --preview --scope all --tests none
webui/deploy/cleanup_all.sh --preview --scope all

# 只处理 WebUI
webui/deploy/deploy_all.sh --execute --yes --scope webui --tests both
webui/deploy/cleanup_all.sh --execute --yes --scope webui

# 全量顺序：DMR → WebUI；清理顺序：停止 WebUI → WebUI → DMR
webui/deploy/deploy_all.sh --execute --yes --scope all
webui/deploy/cleanup_all.sh --execute --yes --scope all
```

部署/清理默认 fail-closed，执行必须显式确认；执行前先审阅项目外证据目录中的
`result.json`。清理使用同文件系统 rename 隔离到 quarantine，不直接删除；正在运行的 WebUI
进程、不可读的 `/proc`、账本、令牌、口令、锁、两个虚拟环境和四套依赖锁文件均受保护。
内核每小时创建账本备份，升级前和定期执行 `webui/deploy/backup_drill.sh`；备份保留和
异地复制需按实际部署环境安排，仓库不携带任何数据库或备份。

## 7. Binance API Key 与配置安全

开发者必须在 Binance 官方平台自行创建和管理自己的 API Key 与 Secret Key。凭证只能通过
环境变量或仓库外、权限受限的本地配置提供，例如：

```bash
export BINANCE_API_KEY='<your-key>'
export BINANCE_API_SECRET='<your-secret>'
```

真实值不得写入 `.py`、`.env.example`、README、Issue、日志、截图、数据库、备份或 Git 历史。
API 权限遵循最小权限原则：只启用实际需要的读取/合约权限，关闭提现并优先配置 IP 白名单；
先使用测试网或极小资金验证，错误配置可能造成资金损失。若怀疑泄露，立即在 Binance 撤销并
轮换凭证；仅删除最新文件不足以清除 Git 历史。

公开模板包括根 `.env.example`、`config/*.example.py`、`bit/config/*.example.py` 和
`bit/data/config/model_state.example.json`；它们不含真实凭证。WebUI 的运行令牌、会话密钥、
操作员口令只在 `webui/var/` 运行时生成，禁止上传。

## 8. 验证、故障排查与维护

### 当前验证状态

| 项目 | 状态 | 说明 |
| --- | --- | --- |
| Shell/Python 部署脚本语法 | ✅ 已核对 | `bash -n` 与 Python 编译检查通过 |
| WebUI 静态部署 e2e | ✅ 已核对 | `45 passed`，不启动真实交易进程 |
| 部署/清理预览 | ✅ 已核对 | 预览不创建环境、不移动文件、不改变运行状态 |
| standin 三服务 | ✅ 回环边界 | `18220 → 18200 → 18210`，默认停止开仓 |
| Binance testnet/mainnet | ⏸ 未验证 | 不应从本 README 推断为可实盘 |
| Chrome/Playwright UI | ⏸ 可选 | 需要本机浏览器和安全夹具，未纳入禁网安装 |

<details>
<summary>当前未验证的交易边界（展开查看）</summary>

- 根 DMR 的真实订单、撤单、条件单触发、交易所状态同步和网络异常恢复，需要在隔离账户中另行验证；
- WebUI 的 standin 回环验证不证明 Binance testnet/mainnet、真实账户或收益结果；
- 外部 `18080` 名单服务、录制数据和生产备份不随仓库发布，相关测试必须由部署者提供安全夹具；
- systemd、反向代理、远程访问、异地备份和凭据轮换属于部署环境责任，仓库只提供模板与操作说明。

</details>

- 根环境：`./venv/bin/python -m pip check`；安全回归：`./venv/bin/python tests/test_security_fixes.py`；
  禁网重跑见 `tests/offline/no_network.py`。这些测试会在本地创建临时状态/配置，运行前应停掉
  交易进程并在副本中执行；需要输出目录的离线套件必须放在项目目录之外。这些命令不替代真实账户联调。
- WebUI（在 `webui/` 目录执行）：先 `deploy/dev_status.sh`，再检查 `var/log/`；内核失联时使用文档中的
  `.venv-kernel/bin/python -m dmr_kernel.cli status/stop/close-all` 应急路径，不手工改账本。
- 启动失败通常先检查 Python 版本、锁文件、回环端口、`DMR_ENV=standin`、`var/` 权限和
  外部名单服务；不要为了“通过”而关闭哈希、禁网或保护规则。
- 依赖升级需重新生成并审阅 lock 文件，运行受保护测试，执行备份演练并保存项目外证据。
- 备份必须放在项目目录之外并定期验证恢复；生产环境的 systemd/监控/告警配置只是模板，
  是否安装、端口和权限必须按实际主机审查。
- `backtrader==1.9.78.123` 是可选 GPLv3+ 回测依赖；本仓库只分发源码、不打包第三方二进制，
  组合再分发（例如镜像或可执行包）时请依据 `THIRD_PARTY_NOTICES.md` 单独评估许可证义务。

## 9. 许可证与归属

本项目采用 Apache License 2.0，SPDX 标识为 `Apache-2.0`，完整条款见 [LICENSE](LICENSE)，
附加声明见 [NOTICE](NOTICE)，依赖清单见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
使用、修改和分发时必须保留原始版权、许可证和必要的修改说明；不得将项目原始成果据为己有，
或声称自己是唯一原始开发者。版权与归属只依据仓库已有声明，不在此虚构作者、组织或年份。

安全问题请使用 GitHub 的私密漏洞报告渠道，不要在公开 Issue 中粘贴凭证、账户 ID、订单号或
持仓数据。完整安全说明见 [SECURITY.md](SECURITY.md)。
