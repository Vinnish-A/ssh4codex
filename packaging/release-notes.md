Linux x86_64 / WSL、glibc 2.35+，包含 Python、官方 MCP SDK 和 OpenSSH，解压即用。

- 新增 `ssh4codex connect solvinglab`：在自己的终端一步接入 SSH / tmux，首次进入配置的 data，之后回到最近选择的会话。
- `--session NAME` 显式选择并记住目标；不存在时创建会话，不踢掉其它客户端。分离后重新接入保留原 shell / 程序状态。
- 人工会话选择与自动 CLI / MCP 任务互不影响。无需修改远端登录脚本或 tmux 配置，MCP 仍提供原有 8 个工具。
- 31 项单元测试通过；新增真实 PTY 验收，覆盖 5 次终端连接、同时连接两个客户端、重新接入原 shell、切换并记住会话。

使用方式见 [Agent 使用指南](https://github.com/Vinnish-A/ssh4codex/blob/main/docs/AGENT_GUIDE.md)。这项改进减少人工操作，不宣称能加速网络或计算；已有网络压力测量和范围见 [性能报告](https://github.com/Vinnish-A/ssh4codex/blob/main/docs/PERFORMANCE.md)。

```bash
curl -fsSL https://raw.githubusercontent.com/Vinnish-A/ssh4codex/main/install.sh | bash
ssh4codex --version
ssh4codex connect solvinglab
```

Solvinglab 已在受管环境验证免密登录；公开包不含私钥，新机器需要自己的授权密钥。远端仍需 Python 3.10+ / tmux。
