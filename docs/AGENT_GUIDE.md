# 后续 Agent 使用指南

本连接器为 Codex 等 coding agent 提供可恢复的远程脚本任务。开始连接任务时先读取本文，再读取 [服务器登记](SERVERS.md)。本文件可公开；凭据和本地恢复记录不可上传。

## 一、安装与确认身份

```bash
curl -fsSL https://raw.githubusercontent.com/Vinnish-A/ssh4codex/main/install.sh | bash
ssh4codex --version
ssh4codex catalog
ssh4codex doctor solvinglab
```

Release 支持 Linux x86_64 / WSL、glibc 2.35+，内含 Python/MCP/OpenSSH；不需要 pip、venv 或本机 SSH 安装。下载后亦可直接解压执行 `./ssh4codex`。远端仍需 Python 3.10+ 和 tmux。没有把服务器密码、私钥或已信任主机清单内置。

Solvinglab 在维护者环境中已部署并实测免密登录。拥有已授权私钥的机器可直接使用内置别名 `solvinglab`；本机已有私钥优先检查 `~/.ssh/id_rsa_wsl`、`~/.ssh/id_rsa`。公钥指纹见服务器表。新机器没有该私钥时不能凭下载 Release 获得访问权；使用已有 ssh-agent 或配置已授权密钥。首次遇到未信任主机，先按组织流程核验并记录 SSH host key，工具不跳过校验。

若没有系统 SSH，安装包内仍提供完整 SSH 客户端。核验主机指纹后，可先交互连接一次以记录 known_hosts：

```bash
~/.local/lib/ssh4codex/current/tools/ssh -p 2220 -i ~/.ssh/id_rsa xzh@solvinglab.top
```

跳板配置中的外部 ProxyCommand 等命令仍需自行提供；本包只内置 SSH 客户端本身。

已有自定义配置覆盖内置别名：

```bash
ssh4codex add lab --target user@host --port 2220 --session data \
  --cwd /project --identity-file ~/.ssh/approved_key
```

配置在 `~/.config/ssh4codex/config.json`，权限 600；其中只保存地址、路径和参数，不保存密钥内容/密码。SSH4CODEX_CONFIG 可选择另一个配置文件；SSH4CODEX_STATE 可选择本地任务登记目录。显式 SSH4CODEX_SSH 可切换 SSH 程序。

### 人工终端直接进入 tmux

```bash
ssh4codex connect solvinglab
ssh4codex connect solvinglab --session analysis
```

0.3.0 起可在自己的交互终端一步完成 SSH / tmux 接入。首次使用配置里的 session（Solvinglab 为 data），之后使用最近通过 connect 选择的目标；显式 `--session` 覆盖并保存选择。目标在 SSH 建连前保存，断线后可以用同一命令回去。记录按服务器配置隔离，存于本地 `SSH4CODEX_STATE` 下的 `<配置哈希>.tmux`，权限 600、原子替换，不上传。

采用 SSH PTY 和 `tmux new-session -A -s NAME`：已有会话接入，不存在则创建，不 detach 其它客户端。服务端保留该会话当前活动窗口 / pane 和 shell 状态；tmux 内手工换会话不更新本连接器的记录。默认 `Ctrl-b`、`d` 分离终端；不要用 exit 代替 detach 来保留 shell。

这项功能供人工接管使用，connect 继承终端输入输出并返回 SSH 的退出码，不返回 JSON，也不注册成 MCP 工具。自动任务继续按配置的 session 执行，不读取人工会话选择；无需 attach，也不向已有 Codex pane 注入输入。实现依据：[tmux 官方手册](https://man.openbsd.org/tmux#new-session)、[SSH PTY 手册](https://man.openbsd.org/ssh#t)。

## 二、执行：使用任务，不猜终端提示符

```bash
ssh4codex run solvinglab --command 'hostname; pwd' --cwd /tmp --wait 2
ssh4codex run solvinglab --script /local/path/analysis.R \
  --cwd /remote/project \
  --interpreter /absolute/path/to/Rscript \
  --env OPENBLAS_NUM_THREADS=1 \
  --artifact result/summary.tsv --artifact result/plot.png \
  --task-id analysis-20261002 --wait 2
```

每个任务在指定 tmux 会话中创建独立窗口。不要复用并向已有用户/Codex pane 模拟输入。脚本按原文上传，interpreter 是可执行文件；MCP 可以传完整 argv 列表，如 `['python3','-u']`。环境参数由 env 明确指定；不会继承某个交互窗口里曾激活的 conda。

`run` 的 JSON 要保存 task_id、state、exit_code、stdout.cursor、stderr.cursor。CLI 返回码 0 可以表示仍在 running，必须阅读 state；任务本身退出码在 exit_code 中。返回码 1 是 failed/cancelled/timed_out/interrupted，2 是连接/配置/协议/传输问题。成功退出与预期产物存在分别记录。

## 三、长任务、断线与重试

```bash
ssh4codex wait solvinglab TASK_ID --seconds 10
ssh4codex list solvinglab
ssh4codex status solvinglab TASK_ID
ssh4codex cancel solvinglab TASK_ID
```

wait 仅等待 0..60 秒，超过后返回 running，不终止任务。运行超时由 `run --timeout SECONDS` 单独控制。SSH 断线或客户端退出后任务继续；在新会话根据 task_id 查询或 list 找回。tmux/服务器结束不等于成功，状态丢失时会报告 interrupted。

`submission_unknown` 表示响应未确认；先查询记录的 task_id。只可用原 task_id 和完全相同请求重试提交，不换新 ID 盲目重放。相同 ID/请求被去重，不同请求被拒绝。没有跨服务器磁盘丢失的 exactly-once 承诺。身份和请求记录已被删除时需由用户决定是否重跑。

取消仅作用于选定任务的子进程组，保留共享 tmux 会话和其它任务。程序自行脱离进程组/daemonize 不在保证范围；不要用此工具假装控制一个自行 daemonize 的服务。

## 四、读日志：保存字节游标

```bash
ssh4codex status solvinglab TASK_ID --stdout-cursor 2048 --stderr-cursor 128 --limit 2048
```

每个输出流默认限制 2048 字节，允许 4..1048576；limit 是字节数。cursor 是下一次从哪里读，不是行号。`remaining` 表示仍有未读字节；`skipped` 表示尾部模式跳过了多少。run/wait 默认取尾部，status 默认从指定 cursor 读取。需要全文时从 cursor=0 分段读，不能把尾部摘要当作完整日志。

日志含 stdout、stderr 分开的真实输出；没有模型摘要替代原日志。不要持续重复 capture-pane 或反复 cat 完整日志。已完成任务再次用末尾 cursor 查询应为空。

## 五、文件与作图

```bash
ssh4codex put solvinglab ./input.tsv /remote/project/input.tsv
ssh4codex fetch solvinglab TASK_ID --to ./downloads
```

put 默认远端文件权限 600，CLI 可 `--mode 644`；流式写临时文件，验证 SHA256 后原子替换。输入原文不返回给模型。覆盖目标是显式副作用，按任务授权选择目的路径。

fetch 用一次 SSH 流取回完成时 manifest 和全部文件，并要求任务 succeeded。未完成、缺失、重复 basename 或完成后被修改的产物都拒绝；全部文件及传输退出码校验通过后逐文件原子交付，不承诺多文件整体事务。图像下载后由 agent 使用图片查看工具核验视觉效果；连接器不评价科研图形结论。

## 六、MCP 配置与工具映射

Codex 可使用以下 stdio 配置，command 使用实际安装的绝对路径：

```toml
[mcp_servers.ssh4codex]
command = "/home/YOUR_USER/.local/bin/ssh4codex-mcp"
```

| 工具 | 用途 |
|---|---|
| remote_run | 提交 script、cwd、interpreter、env、artifacts、timeout、task_id；短等待 |
| remote_status_many | 一次查询 1..64 个任务，默认不带日志 |
| remote_status | 状态与 stdout/stderr 字节游标增量 |
| remote_wait | 单次有上限等待；保持后台任务 |
| remote_cancel | 取消一个任务 |
| remote_fetch | 下载显式结果并校验 |
| remote_tasks | 查询最近 20 个远端任务 |
| remote_put | 上传本地文件到指定远端路径 |

远端任务和运行时：`~/.local/share/ssh4codex/`。本地登记：`~/.local/state/ssh4codex/`。MCP 同时返回 text 和 structuredContent，客户端尽量使用 structuredContent；错误含 error 字段。

## 七、新增服务器与发布边界

新服务器先核实用户名、端口、known_hosts、密钥授权，再测试全新连接而非复用旧密码 master，最后更新 `ssh4codex/catalog.json` 和 `docs/SERVERS.md`。记录公钥指纹、验证日期、私钥是否按用户授权复制以及复制位置；不要记录私钥内容、密码、令牌或未经授权的私网拓扑。

公开提交：软件源码、示例、公开使用文档、服务器部署元数据、可复核测试/测量。保持 `.local/`、AGENTS.md、用户配置、真实任务日志、下载图片、虚拟环境、私钥及 token 不入 Git；预编译运行时仅作为 Release asset，不提交到 main。更改文档不得把旧科研结果写入连接器的长期上下文。

## 八、网络波动与多任务性能

查询多个任务优先批量读取，避免每个任务都重复网络往返：

```bash
ssh4codex status-many solvinglab TASK_A TASK_B TASK_C
# 确实需要末尾日志时再追加 --logs
```

status、status-many、wait、list、doctor 在网络失败后最多重试一次，使用新连接绕过无响应的旧 master；不关闭其它连接。fetch 也只在传输故障时重试一次，全流核验前保留已有目标文件。认证/主机指纹/校验错误不会自动重试。任务提交、取消和上传没有自动重放；上传响应丢失返回 transfer_unknown，应核验目标文件后再决定是否重试。

默认启用 SSH 压缩，尤其适合脚本、日志和文本矩阵。PNG、qs、gzip 等数据通常收益较小；不能突破实际链路带宽。配置 `compression=false` 或 add 的 `--no-compression` 可关闭。修改后使用 disconnect 关闭本工具的 master，再建立连接；远端 tmux 任务继续。

可通过 add 设置 `--connect-timeout`（默认 8 秒）、`--rpc-timeout`（默认 10 秒）、`--transfer-timeout`（默认 300 秒）。RPC 等待请求会在 rpc_timeout 基础上加 wait 秒数；新连接重试还允许 connect_timeout 的握手预算。最多两次尝试，不是无限重连。测试用 rpc_timeout=3、connect_timeout=8；不要把较慢网络的握手时间压到 3 秒。

详见 [网络压力与性能报告](PERFORMANCE.md)。当前传输重试从头开始，没有断点续传；超过两次失败时保留明确错误和恢复 ID，由 agent 决定下一步。

## 九、多个 agent 的协作

主 agent 为每个 subagent 分配独立工作目录和明确任务 ID；保持服务器配置一致以复用 SSH master。`SSH4CODEX_STATE` 可分开保存本地记录，远端状态和完成时的产物 manifest 仍是交接依据。避免几个 agent 覆盖同名分析文件。

子 agent 返回 task_id、预期终态、远端产物路径和 SHA256，不把完整矩阵或日志塞进上下文。主 agent 用一次 status-many 核验终态，再让汇总任务读取经过核验的远端产物。需要查看图像时取回图片并实际查看。

subagent 退出不会取消已提交的 tmux 任务。接手时用原 ID 查询；未收到提交确认时保留原 ID / 原请求，不能换 ID 自动重跑。取消只作用于明确选中的任务。网络故障测试使用独立测试连接，不断开大家共享的 master。

MCP 并发下载请使用 0.2.1 或更新版本：0.2.0 的 SSH 下载进程会继承 MCP 输入，在冷工具缓存的并发调用中可能读走后续请求。完整测试和修复证据见 [Codex 工作流报告](CODEX_WORKFLOWS.md)。
