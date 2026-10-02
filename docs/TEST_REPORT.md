# 实测报告

2026-10-02；本机 WSL → solvinglab，远端 tmux `data`。

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

- `benchmarks/live_acceptance.json`：服务器验收、测试目录及耗时。
- `benchmarks/mcp_acceptance.json`：实际协议验收。
- `benchmarks/measurement.json`：每次测量、统计与限制。
- `benchmarks/output_samples.json`：比较用的原始返回内容，仅本地保留，不随仓库发布。
- `tests/live_solvinglab.py`、`tests/mcp_acceptance.py`、`benchmarks/measure.py`：复跑入口。

测试在专用 `/mnt/nfs_data/xzh/ssh4codex-tests/<run_id>/` 下，未运行或改写 CRC_Mito 的生物分析。客户端上传的远端 helper 和测试 task 记录保留，以便审计；已结束任务窗口释放。

## 独立 Release 验证

Linux x86_64 / WSL、glibc 2.35，构建 Python 3.10。将 PATH 设为 `/nonexistent` 后，以下均通过：

- CLI 版本、帮助和内置服务器目录。
- 包内 OpenSSH 可执行文件及其依赖库。
- 官方客户端初始化 stdio MCP、发现 7 个工具、读取结构化配置错误。
- MCP 输入 EOF 后正常退出，无标准输出被关闭的 traceback。
- Solvinglab doctor、`data` 会话中的实际任务、MCP remote_run 和任务发现。
- 中文产物 SHA256 下载、流式上传和远端内容比对。
- 离线安装、重复安装、安装后的完整运行时验证。

入口为 `tests/release.py`。离线 CI 不持有服务器密钥；上述真实服务器测试使用本机已有授权。依赖和二进制不代表原生 Windows/macOS 或其它架构已经验证。常规 SSH 跳板配置若依赖外部 ProxyCommand 程序，仍需要提供这些程序。

正式发布验证：[`v0.1.0`](https://github.com/Vinnish-A/ssh4codex/releases/tag/v0.1.0) 的 [GitHub Actions 构建](https://github.com/Vinnish-A/ssh4codex/actions/runs/36969206031) 成功；从 GitHub 实际下载资产、通过 SHA256 安装后，再次完成无外部 PATH 的 CLI/MCP 验证、内置别名免密 doctor 和远端 `data` 任务。`releases/latest` 正确指向此版本。

## v0.2.0 网络压力与性能

26 项单元测试、原有 14 项真实验收和 8 工具 MCP（包括批量状态）通过。最终 11 个网络压力场景通过；96 任务 / 32 路并发全部成功。具体数字、故障注入方式、未通过的初次调优和性能局限见 [网络压力与性能报告](PERFORMANCE.md)。


v0.2.0 [GitHub Actions 发布构建](https://github.com/Vinnish-A/ssh4codex/actions/runs/36972815000) 成功。从 GitHub 正式包完成 SHA256 升级，实际版本 0.2.0；安装包在无外部 Python/SSH PATH 下通过 CLI/MCP 8 工具验证，并完成 Solvinglab doctor。本地独立包还通过 tidy R ggplot2/qs、MCP 批量状态和流式产物下载。初始章节记录 v0.1.0 的耗时，最新 14 项实测数据位于 benchmarks/live_acceptance.json。

## v0.2.1 Codex 协作工作流

三个实际 Codex subagent 完成合成 R 分析、混合 MCP worker 和跨进程交接；主 agent 完成提交确认丢失恢复，以及对 21 个源任务 / 24 份远端产物的独立核验和汇总。并发冷工具缓存下载暴露了 SSH 子进程继承 MCP 输入的问题，修复后全新会话的十一路下载通过。27 项单元测试及独立包无外部 PATH 的 CLI / 内置 SSH / MCP 验证通过。版本、耗时、首次失败和复现入口见 [工作流报告](CODEX_WORKFLOWS.md)。
