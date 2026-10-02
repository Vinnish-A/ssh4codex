# ssh4codex

为 Codex 设计的 SSH 任务连接器：提交脚本获得 task_id，在远端 tmux 持续执行，用少量 JSON 查询状态和新增日志，完成后校验并取回产物。客户端退出或连接中断后，可以继续查询同一个任务。

## 下载即用

[下载最新 Release](https://github.com/Vinnish-A/ssh4codex/releases/latest)。Linux x86_64 / WSL（glibc 2.35+）版本已包含 Python、MCP SDK、OpenSSH 及所需库，**无需安装 Python、conda、venv、pip 或本地 OpenSSH**。不是 Windows 原生可执行文件；远端仍需要 Python 3.10+、tmux 和 SSH 服务。

一条命令安装或更新：

```bash
curl -fsSL https://raw.githubusercontent.com/Vinnish-A/ssh4codex/main/install.sh | bash
~/.local/bin/ssh4codex --version
~/.local/bin/ssh4codex catalog
```

安装器验证 SHA256，保留旧版本，并且保留现有配置和密钥。安装需要常规 Linux 工具 bash、curl、tar、sha256sum。也可以直接解压 Release 的 tar.gz，执行其中的 `./ssh4codex` 或 `./ssh4codex-mcp`；必须保留完整目录，不可只复制一个可执行文件。

## 已登记的免密服务器

**我们的受管环境已经为 Solvinglab 配置公钥授权、复制获准的私钥，并实际验证免密登录。** 内置服务器别名 `solvinglab`，默认 tmux 会话 `data`。部署位置、日期和公开指纹见 [服务器登记表](docs/SERVERS.md)。

**仓库和 Release 不包含私钥或密码。** 内置别名会寻找本机 `~/.ssh/id_rsa_wsl` 或 `~/.ssh/id_rsa`；新机器需要提供已经授权的密钥或 SSH agent。复制私钥到服务器用于该服务器主动发起连接；本机到服务器的免密登录来自 `authorized_keys` 公钥授权，两者应区分。

```bash
ssh4codex doctor solvinglab
ssh4codex run solvinglab --command 'printf "hello\n"' --wait 2
```

如果 `~/.local/bin` 不在 PATH 中，使用上面的完整路径。首次连接使用正常 SSH 主机身份校验；不会关闭 host key 检查。

## 自己的服务器与任务

```bash
ssh4codex add lab --target user@host --port 2220 \
  --session data --cwd /path/to/project --identity-file ~/.ssh/id_ed25519
ssh4codex doctor lab
ssh4codex run lab --script analysis.R \
  --interpreter /opt/conda/envs/tidy/bin/Rscript \
  --artifact result/table.tsv --artifact result/figure.png --wait 2
ssh4codex wait lab TASK_ID --seconds 10
ssh4codex status-many lab TASK_ID1 TASK_ID2 TASK_ID3
ssh4codex status lab TASK_ID --stdout-cursor 2048 --stderr-cursor 0
ssh4codex fetch lab TASK_ID --to ./downloads
ssh4codex cancel lab TASK_ID
ssh4codex list lab
ssh4codex put lab ./input.tsv /remote/path/input.tsv
```

任务不继承交互 pane 的 conda 环境，须指定解释器。`--wait` 只控制客户端等待，`--timeout` 才限制程序运行。状态包括 queued、running、succeeded、failed、cancelled、timed_out、interrupted。脚本的原始退出码保留在 JSON；CLI 本身 0 表示正常返回（包括正在运行），1 表示任务失败/终止，2 表示配置、连接、协议或文件错误。

同一 task_id 的同一请求不会重复执行；不同请求不能共用 ID。提交结果不明确时先查该 ID，**不要换 ID 自动重跑**。默认每个日志流返回最多 2048 字节，`skipped` 和 `remaining` 明确表示省略/未读内容，完整日志仍在远端。取消只针对该任务及其子进程组，不结束共享 tmux 会话。

## 后续 Agent 从这里开始

详细操作、恢复步骤、日志游标、文件传输、MCP、凭据边界和登记新服务器的方法，请阅读 **[Agent 使用指南](docs/AGENT_GUIDE.md)**。

MCP 配置示例（将路径中的 YOUR_USER 替换为实际用户）：

```toml
[mcp_servers.ssh4codex]
command = "/home/YOUR_USER/.local/bin/ssh4codex-mcp"
```

提供 8 个工具：remote_run、remote_status、remote_status_many、remote_wait、remote_cancel、remote_fetch、remote_tasks、remote_put。CLI 与 MCP 共用任务引擎。

本地配置 `~/.config/ssh4codex/config.json`；本地恢复记录 `~/.local/state/ssh4codex/`；远端任务日志 `~/.local/share/ssh4codex/tasks/`。支持 `SSH4CODEX_CONFIG`、`SSH4CODEX_STATE` 覆盖。真实配置、私钥、上传内容和研究数据均不随仓库发布。

## 实测与开发

26 项单元测试、14 项 Solvinglab 实际验收以及官方 SDK stdio MCP 调用通过，覆盖并发幂等、退出码、UTF-8 日志、断线恢复、超时/取消、二进制文件往返、R ggplot2/qs 产物。独立 Release 另外验证无 Python / SSH PATH 下的 CLI、内置 SSH 和 MCP，以及真实服务器任务。

一次五次采样的持久短任务中位数为 0.359 秒，裸 SSH 冷连接 1.888 秒，裸 SSH 复用连接 0.465 秒。一次生成日志的三个观察返回值，o200k_base 估算由 3330 降至 593 token，减少 82.2%。这是特定返回内容的测量，不是完整会话开销或性能保证；MCP 封装和工具 schema 未计入。[测试报告](docs/TEST_REPORT.md) · [网络压力与性能报告](docs/PERFORMANCE.md)

本版支持非交互任务，没有自动控制远端 Codex/REPL、管理员 sudo、断点续传或服务器重启后自动重跑。远端 tmux 被结束时报告 interrupted，不自动重放任务。

从源码开发：

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[mcp]'
.venv/bin/ssh4codex --help
```

[打包与 Release](packaging/README.md) · [开源工具调研](docs/OPEN_SOURCE_REVIEW.md) · [任务协议](docs/DESIGN.md) · [变更记录](CHANGELOG.md)
