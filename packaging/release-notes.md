Linux x86_64 / WSL，glibc 2.35+（例如 Ubuntu 22.04+）。解压即用，含 Python、官方 MCP SDK 和 OpenSSH；无需 pip、venv 或额外客户端安装。

- CLI 与 7 个 MCP 工具：后台任务、task_id 恢复、同请求去重、增量日志、超时/取消、流式传输与 SHA256 校验。
- 短任务提交与等待合并为一次请求，SSH 断开后按 task_id 找回。
- 内置公开服务器目录；维护者受管环境已验证 solvinglab 的免密码密钥登录。包中不含私钥，新的机器需持有已授权密钥。
- 详细 agent 操作、故障处理、服务器登记及测试范围已随源码和包提供。

远端需要 Python 3.10+ / tmux；本版不实现交互终端键盘自动控制、sudo 或服务器重启后自动重跑。

```bash
curl -fsSL https://raw.githubusercontent.com/Vinnish-A/ssh4codex/main/install.sh | bash
ssh4codex catalog
ssh4codex doctor solvinglab
```
