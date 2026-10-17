# deploy/：开发用启停脚本、守护、备份演练与 systemd 模板

本目录的脚本只用于**替身环境**（`DMR_ENV=standin`）的开发与验收：三个进程都只绑回环，不读任何密钥，不碰
`DMR_WebUI/.env` 和 `data/runtime_state/`，不调用 Binance 私有接口。解释器只用本工程的两个环境
（`.venv-kernel/bin/python`、`.venv-console/bin/python`，绝对路径），脚本自己把 `PATH` 固定为 `/usr/bin:/bin`，
不会用到 shell 里激活的 `DMR_WebUI/venv`。

## 部署/清理总入口（新增）

本目录现在同时提供两套彼此独立的引擎和两个薄编排入口：

| 脚本 | 作用 |
|---|---|
| `deploy_webui.sh` | WebUI 两套 3.12 环境、哈希锁、禁网测试、纯净快照、可选回环启动 |
| `cleanup_webui.sh` | WebUI 缓存/日志/过期备份的可恢复隔离；保护账本、令牌、口令、锁和环境 |
| `deploy_all.sh` | `DMR → WebUI` 顺序编排；preview 不调用没有 preview 参数的根 DMR 部署器 |
| `cleanup_all.sh` | `停止 WebUI → WebUI 清理 → DMR 清理` 顺序编排 |

四个入口默认只预览；执行必须显式使用 `--execute --yes` 或 `--execute --confirm`。
完整参数、安全边界和证据格式见 [部署手册](../docs/部署手册.md)、[清理手册](../docs/清理手册.md)
和[一键部署清理](../docs/一键部署清理.md)。本轮不安装 systemd，也不修改 DMR 根部署脚本。

| 进程 | 命令 | 监听 | 进程号文件 | 日志 |
|---|---|---|---|---|
| 替身交易所 | `.venv-kernel/bin/python -m dmr_standin` | 127.0.0.1:18220 | `var/run/standin.pid` | `var/log/standin.log` |
| 交易内核 | `.venv-kernel/bin/python -m dmr_kernel`（经 `supervise.sh` 守护） | 127.0.0.1:18200 | `var/run/kernel.pid`、守护 `var/run/supervise.pid` | `var/log/kernel.log`、`var/log/supervise.log` |
| Web 控制台 | `.venv-console/bin/python -m dmr_console` | 127.0.0.1:18210 | `var/run/console.pid` | `var/log/console.log` |

## 脚本

| 脚本 | 作用 |
|---|---|
| `dev_start.sh` | 后台依次启动 替身交易所 → 内核（经 `supervise.sh`）→ 控制台，每个都等健康检查通过；已在运行的不重复启动；最后打印三个地址与状态 |
| `dev_stop.sh` | 按 控制台 → 内核 → 替身 的顺序停；内核先写停用标记 `var/run/kernel.nosupervise` 再发 SIGTERM，最多等 30 秒，超时 SIGKILL；清理进程号文件 |
| `dev_status.sh` | 每个进程的进程号、是否存活、健康检查（替身 `GET /api/public/time`、内核 `GET /api/v1/health`、控制台首页）；全部健康退出 0，否则 1 |
| `supervise.sh` | 循环运行内核；内核退出（含 `kill -9`）后若没有停用标记，2 秒后拉起；连续快速失败时间隔翻倍（封顶 60 秒） |
| `backup_drill.sh` | 取 `backup/` 下最新的账本在线备份，用 `restore_backup` 恢复到临时目录并做一致性检查，打印结果 |
| `heartbeat_watch.py` | 内核心跳看护（给 `dmr-heartbeat.service` 用）：心跳文件超过 60 秒没更新就退出 1 并推送告警 |
| `lib.sh` | 公共函数，只被 source，不直接运行 |

脚本可以从任何目录运行（也可以通过符号链接），不依赖当前目录。

### 环境变量（都可省略）

| 变量 | 缺省 | 说明 |
|---|---|---|
| `DMR_STANDIN_PORT` / `DMR_KERNEL_PORT` / `DMR_CONSOLE_PORT` | 18220 / 18200 / 18210 | 端口，1–65535 且互不相同；主机固定 127.0.0.1（`DMR_KERNEL_HOST`、`DMR_CONSOLE_HOST` 设成别的会被拒绝） |
| `DMR_VAR_DIR` | `<工程根>/var` | 运行期目录；相对路径按工程根解析。进程号文件在 `run/`，日志在 `log/` |
| `DMR_STANDIN_FEED` | `auto` | 替身交易所的行情源：`auto`（先试 Binance 公共接口，不可用整次运行用回放）、`binance`、`replay`（只回放 `DMR_WebUI/bit/data` 里的 CSV，不连网） |
| `DMR_KERNEL_SHELL` | 空 | `1` 让内核以空壳运行（业务装配 T0.22 完成之前的自检用；空壳没有控制接口，`dev_status.sh` 按内核锁判断健康），原样传给内核 |
| `DMR_DEV_START_TIMEOUT_S` | 60 | 每个进程起来并通过健康检查的最长等待 |
| `DMR_DEV_STOP_TIMEOUT_S` / `DMR_DEV_KERNEL_STOP_TIMEOUT_S` | 15 / 30 | 收到 SIGTERM 后的最长等待，超时 SIGKILL |
| `DMR_DEV_SUPERVISE_EXIT_TIMEOUT_S` | 10 | 内核停掉后等守护自行退出的最长时间 |
| `DMR_SUPERVISE_DELAY_S` / `_FAST_S` / `_MAX_BACKOFF_S` / `_STOP_GRACE_S` | 2 / 10 / 60 / 30 | 守护的重启间隔、"快速失败"门限（运行不足这么久就退出算一次）、退避上限、停止时给内核的宽限期 |

其余 `DMR_*` 变量（如 `DMR_BACKUP_DIR`、`DMR_LOG_LEVEL`、`DMR_CRASHPOINT`）原样传给被启动的进程。`DMR_ENV` 只能是 `standin`；
`DMR_ALLOW_LIVE` 会被清除。脚本还会清除 `VIRTUAL_ENV`、`PYTHONPATH`、`PYTHONHOME`，以及测试模式标记（`PYTEST_CURRENT_TEST` 等：
NiceGUI 在环境里看到它会改走屏幕测试模式，控制台随即崩溃——e2e 测试正是从 pytest 进程里运行这些脚本的）。

### 退出码

`dev_start.sh`：0 全部健康；1 某个进程没起来（已打印原因和日志末尾；进程起来后又退出过一次就立即判失败，不等满 `DMR_DEV_START_TIMEOUT_S`；内核起不来时会顺手停掉守护，不留崩溃循环，修好配置后直接再运行即可）；2 配置无效（什么都没启动）；
3 停用标记存在。`dev_stop.sh`：0 全部停掉（没有东西在运行也是 0）；1 有进程无法终止。`dev_status.sh`：0 全部存活且健康；1 否则。

## 守护与停用标记

`supervise.sh` 的规则：

- 内核退出后隔 2 秒再拉起，`kill -9` 也一样；每次拉起都更新 `var/run/kernel.pid`，守护自己的进程号记在 `var/run/supervise.pid`。
- 内核运行不足 10 秒就退出算一次快速失败；连续快速失败时间隔 2、4、8…秒翻倍，封顶 60 秒；运行满 10 秒后清零。
- **停用标记** `var/run/kernel.nosupervise`：存在时守护不再拉起内核并自行退出（退出码 0）。检查发生在每次拉起之前和内核退出之后，
  所以退避等待期间写的标记也有效。守护不创建也不删除它：`dev_stop.sh` 写、全部停掉后删；命令行应急入口（M2）写、谁写谁删。
  标记存在而没有守护在运行时，`dev_start.sh` 拒绝启动（退出码 3），确认可以启动后删掉它。
- 收到 SIGTERM/SIGINT/SIGHUP：转发给内核，最多等 30 秒，超时 SIGKILL，然后不再拉起。
- 同一时刻只允许一个守护（`var/run/supervise.lock`）。

## 日志

各进程自己按大小轮转，写 `var/log/` 下的绝对路径，不依赖需要 root 的工具：内核 `kernel.log`、替身交易所 `standin.log`、
控制台 `console.log`，都是 10MB×5 份、时间一律 UTC（`2026-10-15T08:53:20Z INFO …`，本机时区是 +07 也不变）。

`var/log/*.out` 是各进程的原始标准输出和错误（启动失败的回溯、没走日志模块的输出，以及与日志重复的标准错误）；它不是轮转日志，
`dev_start.sh`/`supervise.sh` 在每次启动前发现超过 10MiB 就换成 `.out.1`，可以随时删除。

## 备份与恢复演练

内核每小时在线备份账本到 `backup/`（在 `var/` 之外，文件名 `ledger-<UTC 时间戳>[-标签].sqlite`）。演练：

```
deploy/backup_drill.sh                      # 最新的备份
deploy/backup_drill.sh --file backup/ledger-20251009T085320.000Z.sqlite
deploy/backup_drill.sh --keep               # 保留恢复出来的库，排查用
```

它用 `dmr_kernel.ledger.backup.restore_backup` 把备份复制到临时目录（`$TMPDIR`，缺省 `/tmp`，0700）并检查：`PRAGMA integrity_check`、
`PRAGMA foreign_key_check`、每张表的行数、与备份文件逐字节比对；打印结果后删除临时目录。**不碰正在使用的账本**（不打开它，
也拒绝把它当成 `--file`）。退出码：0 通过；1 恢复或检查失败；2 用法错误或没有备份。建议每次升级前和每周跑一次。
子工程仓库没有远程，备份目录之外还要每日打包到 DMR_WebUI 目录之外（计划报告 4.13，另行安排）。

## systemd 用户级单元模板（本轮不安装）

`deploy/systemd/` 下有五个模板：`dmr-standin.service`、`dmr-kernel.service`、`dmr-console.service`、
`dmr-heartbeat.service`、`dmr-heartbeat.timer`。三个服务 `Restart=on-failure`，只绑回环，互不连带（只有 `Wants`/`After`：
重启控制台不影响内核）；内核单元用 `ExecCondition` 遵守停用标记；`dmr-heartbeat.timer` 每 30 秒触发一次心跳看护
（`heartbeat_watch.py`：心跳停止超过 60 秒即推送，90 秒内应收到；Apprise 地址放在仓库之外的 `~/.config/dmr-webui/alerts.env`，
权限 0600，内容形如 `DMR_ALERT_URLS=...`；没有这个文件也能运行，只是不推送）。

模板里的 `@WEBUI_ROOT@` 是占位符，安装时换成 webui 目录的绝对路径。要装的时候（人工执行，本轮没有装）：

```
WEBUI_ROOT=/path/to/DMR_WebUI/webui
mkdir -p ~/.config/systemd/user ~/.config/dmr-webui
for unit in dmr-standin.service dmr-kernel.service dmr-console.service dmr-heartbeat.service dmr-heartbeat.timer; do
  sed "s|@WEBUI_ROOT@|$WEBUI_ROOT|g" "$WEBUI_ROOT/deploy/systemd/$unit" > ~/.config/systemd/user/$unit
done
systemctl --user daemon-reload
systemctl --user enable --now dmr-standin.service dmr-kernel.service dmr-console.service dmr-heartbeat.timer
loginctl enable-linger "$USER"     # 退出登录后继续运行、开机自启（本机已启用时可以省略）
systemctl --user status dmr-kernel.service
journalctl --user -u dmr-kernel.service -f
```

注意：

- 用 systemd 守护内核时不要同时运行 `supervise.sh`/`dev_start.sh`（两个守护会争同一把内核锁）；停服务用 `systemctl --user stop`，
  不要用 `dev_stop.sh`。
- 内核的业务装配（T0.22）完成之前只能空壳运行：在 `dmr-kernel.service` 的 `[Service]` 里加一行 `Environment=DMR_KERNEL_SHELL=1`。
- 单元里的端口是 18220/18200/18210；要改端口，同时改 `ExecStart`、`Environment=` 和别的单元里对它的引用。
- 卸载：`systemctl --user disable --now …`，删除 `~/.config/systemd/user/dmr-*`，`systemctl --user daemon-reload`。
