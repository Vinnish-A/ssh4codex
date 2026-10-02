Linux x86_64 / WSL、glibc 2.35+，包含 Python、官方 MCP SDK 和 OpenSSH，解压即用。

- 新增批量任务查询：CLI `status-many` / MCP `remote_status_many`，1..64 个任务共用一次请求，默认不返回日志。
- 多文件下载合并为一次 SSH 流，边读边哈希，全流验证后原子交付；保留完成时校验、缺失/修改检测。
- 只读查询和下载遇到网络故障最多重试一次，绕过无响应的 master；提交和上传保持不自动重放。加强本地任务记录并发保护和响应丢失分类。
- 修复 OpenSSH 复用连接中断后退出码为 0、但响应已截断的情况，识别声明长度后安全重试读取。
- 默认 SSH 压缩，可关闭；支持建连、RPC、文件传输超时配置。
- 26 项单元测试、真实 R/MCP/传输测试、11 个网络压力场景及两项文件/响应中途截断测试、96 任务 / 32 路并发通过。

延迟测试：24 个小文件约 25.5→1.1 秒；16 个任务逐个查询约 19.9 秒，批量约 1.3 秒。8 MiB 高度可压缩测试数据约 213→3.8 秒，不能将此收益推广到 PNG/qs/随机数据。完整数据、初次调优失败和范围见 [性能报告](https://github.com/Vinnish-A/ssh4codex/blob/main/docs/PERFORMANCE.md)。

```bash
curl -fsSL https://raw.githubusercontent.com/Vinnish-A/ssh4codex/main/install.sh | bash
ssh4codex --version
```

Solvinglab 已在受管环境验证免密登录；公开包不含私钥，新机器需要自己的授权密钥。远端仍需 Python 3.10+ / tmux。
