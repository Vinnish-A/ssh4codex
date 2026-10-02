Linux x86_64 / WSL、glibc 2.35+，包含 Python、官方 MCP SDK 和 OpenSSH，解压即用。

- 修复并发 MCP 下载的问题：SSH 下载子进程现在使用独立的空输入，避免读取后续 JSON-RPC 工具请求。此前文件可能已经取回，但 MCP 会话仍卡住或关闭。
- 新增真实 Codex subagent 工作流验收：合成表达矩阵的 R 分析和 qs 缓存、依赖绘图、多任务调度、跨进程交接、同 ID 竞争，以及提交响应丢失后的恢复。
- 27 项单元测试通过；正式运行时在没有系统 Python / SSH PATH 的环境验证 CLI、内置 SSH 和 MCP。

本次工作流和发现问题的过程见 [Codex 工作流报告](https://github.com/Vinnish-A/ssh4codex/blob/main/docs/CODEX_WORKFLOWS.md)。已有网络压力测量和范围见 [性能报告](https://github.com/Vinnish-A/ssh4codex/blob/main/docs/PERFORMANCE.md)。

```bash
curl -fsSL https://raw.githubusercontent.com/Vinnish-A/ssh4codex/main/install.sh | bash
ssh4codex --version
```

Solvinglab 已在受管环境验证免密登录；公开包不含私钥，新机器需要自己的授权密钥。远端仍需 Python 3.10+ / tmux。
