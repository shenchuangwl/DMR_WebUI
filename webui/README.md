# DMR WebUI

`webui/` 是 DMR_WebUI 的 Web 控制台子工程，包含交易内核 `dmr_kernel`、NiceGUI 控制台
`dmr_console` 和仅用于开发/验收的本地替身交易所 `dmr_standin`。当前公开代码的可验证运行边界是
`DMR_ENV=standin`：三个服务只绑定 `127.0.0.1`，不连接 Binance 私有接口，也不代表实盘或测试网已经验证。

## 快速开始

所有命令都在本目录执行，并使用本工程的解释器，不要使用根目录旧 DMR 的 `venv`：

```bash
# 依赖安装由 deploy/deploy_webui.sh 负责，先查看计划
deploy/deploy_webui.sh --preview --tests none

# 显式确认后创建两个 Python 3.12 环境、按哈希锁安装并跑禁网测试
deploy/deploy_webui.sh --execute --yes --tests both

# 启动替身 → 内核 → 控制台（默认不开仓）
deploy/dev_start.sh
deploy/dev_status.sh
```

控制台默认地址为 `http://127.0.0.1:18210`；内核为 `127.0.0.1:18200`，替身为
`127.0.0.1:18220`。需要停止时运行 `deploy/dev_stop.sh`。端口和主机均可按实际环境调整，
但启动脚本会拒绝非回环监听。

## 测试

```bash
./run_tests.sh kernel    # 内核、算法对拍、替身、集成与故障注入，禁网包装
./run_tests.sh console   # 控制台，禁网包装
./run_tests.sh e2e       # 回环端到端，临时真实进程与回放行情
./run_tests.sh ui        # 三进程 + 本机 Chrome/Playwright；不在禁网包装下
```

不要在 `webui/` 根目录直接运行裸 `pytest` 或 PATH 中的 Python。测试只使用本工程的
`.venv-kernel`、`.venv-console`。历史验收报告和截图不属于公开发布物。

## 环境与配置

- Python：3.12（`pyproject.toml` 要求 `>=3.12,<3.13`）。
- 内核/替身依赖：`requirements/kernel.in`、`requirements/kernel.lock`；开发测试使用
  `kernel-dev.lock`。
- 控制台依赖：`requirements/console.in`、`requirements/console.lock`；测试使用
  `console-dev.lock`。
- 运行期目录默认为 `var/`，备份目录默认为 `backup/`。其中账本、令牌、操作员口令、会话
  签名密钥和日志均为本机生成数据，已被忽略，禁止提交。

`DMR_*` 环境变量的完整清单和回环约束见 `dmr_kernel/settings.py`、`dmr_console/settings.py`
及 `deploy/README.md`。本子工程没有公开的生产凭证模板；替身令牌和操作员口令由运行时以
`0600` 权限生成。若未来接入交易所私有接口，必须沿用仓库外的安全配置，不得把密钥写进源码、
`.env`、截图或提交历史。

## 部署、清理与备份

推荐先读 `docs/部署手册.md`、`docs/清理手册.md` 和 `docs/一键部署清理.md`。

```bash
# 仅预览（不创建环境、不启动、不移动文件）
deploy/deploy_all.sh --preview --scope webui --tests none
deploy/cleanup_all.sh --preview --scope webui

# 显式执行；部署和清理都会在项目外留下 result.json 证据
deploy/deploy_all.sh --execute --yes --scope webui --tests both
deploy/cleanup_all.sh --execute --yes --scope webui
```

`deploy_all.sh --scope all` 按 DMR → WebUI 编排，`cleanup_all.sh --scope all` 按
停止 WebUI → WebUI 可恢复隔离 → DMR 清理编排；两套引擎的内部逻辑不混合。清理只把
日志、缓存和符合保留期限的旧账本备份移到项目外 quarantine，不直接删除，并永久保护正在使用的
账本、令牌、口令、锁和两个虚拟环境。执行前必须阅读预览结果；运行中的服务或不可读的 `/proc`
信息会触发 fail-closed 拒绝。

内核每小时生成账本备份。升级前及定期执行 `deploy/backup_drill.sh` 验证备份，另需按实际
部署环境把备份复制到项目目录之外；仓库不包含任何运行期账本或备份。

## 与根 DMR 的边界

根目录的旧 DMR 入口（例如 `start_trading.sh`、`first_*_main.py`）是另一套运行面，可能在
配置真实凭证后访问 Binance 并下单；本 WebUI 子工程当前只保证替身回环验收。不要把 WebUI
的 18200/18210/18220 端口与根 DMR 的外部 Binance API 混为一谈。正式部署、systemd 安装、
测试网/主网接入和任何真实订单都需要根据实际部署环境另行审查与授权。

## 安全与风险

本子工程只提供研究和本地验收代码。若二次开发接入 Binance：开发者必须在 Binance 官方平台
自行创建和管理个人 API Key/Secret Key，只通过环境变量或仓库外的权限受限配置注入；密钥不得
提交到 GitHub、Issue、日志、截图或备份。使用最小权限并关闭提现权限，先在测试环境或极小资金
验证，确认网络、时间同步、风控和停止流程后再考虑生产环境。任何交易都可能造成全部本金损失。

## 许可证

整个项目按 Apache License 2.0（SPDX：`Apache-2.0`）发布，详见根目录 `LICENSE`、`NOTICE`
和 `THIRD_PARTY_NOTICES.md`。使用、修改和分发时请保留原始版权、许可证及必要的修改说明，
不得将原始成果冒称为唯一原创。作者/归属只依据仓库现有声明，不在此新增未核实的权利信息。
