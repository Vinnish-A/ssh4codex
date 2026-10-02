# 实测报告

2026-10-02；本机 WSL → 配置的远端主机，远端使用外部配置指定的 tmux 会话。

## v0.1.0 初始功能验证

- 14 项单元测试通过，覆盖任务路径、UTF-8 游标、请求去重与冲突、退出码、超时、取消、产物清单和未知提交响应。
- 14 项真实服务器验收通过，以下为实际记录：

- doctor_python_tmux：通过
- short_job_separate_streams：通过，0.673 s
- exit_7_is_failed：通过，0.619 s
- literal_quotes_unicode_binary_fetch：通过，1.708 s
- concurrent_retry_runs_once_and_conflict_rejected：通过，1.155 s
- incremental_utf8_logs_no_duplicates_or_loss：通过，18.366 s
- early_fetch_blocked_then_verified：通过，3.914 s
- changed_artifact_checksum_rejected：通过，1.581 s
- missing_artifact_reported：通过，1.333 s
- timeout_terminates_group：通过，1.537 s
- cancel_only_selected_task：通过，2.601 s
- transport_disconnect_and_fresh_client_recovery：通过，5.732 s
- 512KiB_binary_upload_and_roundtrip：通过，24.584 s
- real_tidy_R_ggplot2_qs_and_PNG_fetch：通过，3.06 s

- 官方 MCP SDK stdio initialize / list_tools / remote_run / remote_tasks 调用通过，7 个工具均被客户端发现。

## 五次采样耗时

| 操作 | 中位耗时 |
|---|---|
| bare_ssh_warm | 0.465 s |
| bare_ssh_cold | 1.888 s |
| ssh4codex_durable_task | 0.358 s |

每项 5 次，网络抖动较大。工具主要减少多次提交/观察的往返，并复用 OpenSSH 连接；不宣称新加密协议更快，也不宣称任务包装一定比裸 SSH 复用更快。冷连接比较的收益大部分来自系统 OpenSSH 本已有的 multiplexing。

## 返回内容 token 示例

同一生成的 300 行日志，完成后观察三次。旧流程每次读最后约 100 行终端历史，新流程第一次返回最多 2048 字节尾部，后两次只读游标之后新增部分。返回内容用 tiktoken o200k_base 估算：3330 → 593 token，减少 82.2%。初次省略 13552 字节，完整日志仍在远端；可从 cursor=0 取回。

这是有明确省略范围的返回内容比较，不是全文读取比较，也不是完整会话或模型总 token 测量。它没有计入工具 schema、输入、MCP 双份 text/structured 封装等成本；o200k_base 并非已核实的 Codex tokenizer。不能把 82.2% 推广到所有任务。

## 可复核文件

- `外部报告目录中的 live_acceptance.json`：服务器验收、测试目录及耗时。
- `外部报告目录中的 mcp_acceptance.json`：实际协议验收。
- `外部报告目录中的 measurement.json`：每次测量、统计与限制。
- `外部报告目录中的 output_samples.json`：比较用的原始返回内容，仅本地保留，不随仓库发布。
- `tests/live_server.py`、`tests/mcp_acceptance.py`、`benchmarks/measure.py`：复跑入口。

测试在专用临时目录下，未运行或改写用户项目的生物分析。客户端上传的远端 helper 和测试 task 记录保留，以便审计；已结束任务窗口释放。

## 独立 Release 验证

Linux x86_64 / WSL、glibc 2.35，构建 Python 3.10。将 PATH 设为 `/nonexistent` 后，以下均通过：

- CLI 版本、帮助和服务器配置查询。
- 包内 OpenSSH 可执行文件及其依赖库。
- 官方客户端初始化 stdio MCP、发现 7 个工具、读取结构化配置错误。
- MCP 输入 EOF 后正常退出，无标准输出被关闭的 traceback。
- 配置的远端主机 doctor、配置的会话中的实际任务、MCP remote_run 和任务发现。
- 中文产物 SHA256 下载、流式上传和远端内容比对。
- 离线安装、重复安装、安装后的完整运行时验证。

入口为 `tests/release.py`。离线 CI 不持有服务器密钥；上述真实服务器测试使用本机已有授权。依赖和二进制不代表原生 Windows/macOS 或其它架构已经验证。常规 SSH 跳板配置若依赖外部 ProxyCommand 程序，仍需要提供这些程序。

正式发布验证：[`v0.1.0`](https://github.com/Vinnish-A/ssh4codex/releases/tag/v0.1.0) 的 [GitHub Actions 构建](https://github.com/Vinnish-A/ssh4codex/actions/runs/36969206031) 成功；从 GitHub 实际下载资产、通过 SHA256 安装后，再次完成无外部 PATH 的 CLI/MCP 验证、外部连接定义的 doctor 和远端配置会话中的任务。该版本发布时验证了 `releases/latest` 的指向。

## v0.2.0 网络压力与性能

26 项单元测试、原有 14 项真实验收和 8 工具 MCP（包括批量状态）通过。最终 11 个网络压力场景通过；96 任务 / 32 路并发全部成功。具体数字、故障注入方式、未通过的初次调优和性能局限见 [网络压力与性能报告](PERFORMANCE.md)。


v0.2.0 [GitHub Actions 发布构建](https://github.com/Vinnish-A/ssh4codex/actions/runs/36972815000) 成功。从 GitHub 正式包完成 SHA256 升级，实际版本 0.2.0；安装包在无外部 Python/SSH PATH 下通过 CLI/MCP 8 工具验证，并完成配置的远端主机 doctor。本地独立包还通过 tidy R ggplot2/qs、MCP 批量状态和流式产物下载。初始章节记录 v0.1.0 的耗时，最新 14 项实测数据位于 外部报告目录中的 live_acceptance.json。

## v0.2.1 Codex 协作工作流

三个实际 Codex subagent 完成合成 R 分析、混合 MCP worker 和跨进程交接；主 agent 完成提交确认丢失恢复，以及对 21 个源任务 / 24 份远端产物的独立核验和汇总。并发冷工具缓存下载暴露了 SSH 子进程继承 MCP 输入的问题，修复后全新会话的十一路下载通过。27 项单元测试及独立包无外部 PATH 的 CLI / 内置 SSH / MCP 验证通过。版本、耗时、首次失败和复现入口见 [工作流报告](CODEX_WORKFLOWS.md)。

[`v0.2.1`](https://github.com/Vinnish-A/ssh4codex/releases/tag/v0.2.1) 的 [GitHub Actions 发布构建](https://github.com/Vinnish-A/ssh4codex/actions/runs/36975183364) 成功。从 GitHub 正式资产下载、SHA256 安装并替换本地候选包后，再次验证无外部 PATH 的 CLI / 内置 SSH / MCP 8 工具、配置的远端主机 doctor、全新 MCP 会话十一路并发下载及 14 个任务批量状态；所有文件 SHA256 与完成时 manifest 一致。

## v0.3.0 直接 tmux 接入

31 项单元测试通过，覆盖交互终端要求、目标校验、断线时保留最近选择、原子私有记录，以及人工选择不改变自动任务的会话。独立包在无外部 PATH 下通过 connect 帮助、CLI / 内置 SSH / MCP 8 工具验证。

`tests/tmux_connect.py --run` 在候选独立包和正式发布包上分别通过 5 次真实 PTY 连接，完整验收耗时分别为 14.026 秒和 6.464 秒（包含观察和验证，不是纯建连延迟，也不能据此推算优化倍数）。测试创建两个专用合成会话，验证配置默认会话直接创建并进入、同时接入两个客户端而不踢掉第一个、分离后重新连接仍能读取原 shell 环境变量，以及显式选择另一个会话后新客户端直接接回该会话。测试只清理自己的两个会话和 SSH master，没有改动既有会话或用户项目。正式包可复核测量见 tmux 接入报告（原始记录保存在外部私有报告目录）。

[`v0.3.0`](https://github.com/Vinnish-A/ssh4codex/releases/tag/v0.3.0) 的 [GitHub Actions 发布构建](https://github.com/Vinnish-A/ssh4codex/actions/runs/36977161933) 成功。从 GitHub 正式资产下载并通过 SHA256 安装后，再次完成无外部 PATH 的 CLI / connect 帮助 / 内置 SSH / MCP 验证，以及上述真实 PTY 连接。维护者本机已升级为 0.3.0。

## v0.4.0 配置分离

35 项单元测试通过，新增未配置连接不启动 SSH、必填目标与会话、外部配置读取不改写的验证。源码和独立包扫描均不包含部署定义、认证部署记录或原始测量报告。无外部 Python / SSH PATH 的 CLI、包内 OpenSSH 和 8 工具 MCP 验证通过。

真实服务器 14 项验收通过；候选独立包完成 5 次 PTY 会话创建、并行接入和接回验证，以及跨进程交接、合成 R / qs / ggplot2 分析、14 个混合 MCP 任务与冷会话并发取回、故障链路上的提交确认丢失和原 ID 恢复。全部使用外部配置与专用测试目录，原始记录保存于仓库外。

## 0.5.0 改进的验收（2026-10-02—03）

- 54 项本地测试通过：断点续传、丢失回执、坏前缀/坏哈希、目标被修改、并发写锁、重复请求、输入失败阻断、跨进程恢复、独立日志游标、交付失败退出语义及原有回归。
- 实际服务器六阶段通过：50 MiB 随机文件与同时查询、13 MiB 中断续传、新客户端重复上传零传输、上传回执丢失、任务恢复避免重复执行、输入/环境预检和 24 任务并发。具体计时见 PERFORMANCE.md。
- 独立程序包在 PATH=/nonexistent 下通过 CLI、内置 OpenSSH、MCP 初始化和退出检查；12 个工具均可发现。
- 冻结 MCP 实际验证了上传、传输状态、输入声明、所需程序检查、原任务恢复和持久日志游标。冻结 CLI 验证了外部执行 profile 与成功后直接取回产物。
- 冻结 MCP 的 14 个混合任务和 11 路冷缓存并发下载通过，保留此前 stdin 隔离修复。失败任务、超时任务、指定取消均正确识别，其余任务正常完成。

首次外部配置验收失败源于旧测试脚本未向 MCP 子进程传递 SSH4CODEX_CONFIG，已修正测试环境传递并重跑通过；未把失败轮次计为成功。真实连接配置、原始报告、任务脚本与运行记录均保存在仓库外。测试仅创建和清理专用会话，未中断原有运行中的会话。
