Linux x86_64 / WSL、glibc 2.35+，内含 Python、OpenSSH 和 MCP SDK，下载解压即可使用。

- 上传支持断点续传、前缀与完整 SHA256 校验，使用独立连接，保护传输期间被修改的目标文件。
- 新增 recover：先查询原任务，明确指定重试后才恢复保存的原请求，避免重复执行。
- 提交前校验输入文件和所需程序；支持外部执行 profile、独立日志游标、单次连接选项及成功后取回产物。
- CLI 与 MCP 共用恢复机制，MCP 提供 12 个工具。

已通过 54 项本地测试、六组真实服务器验收、14 个混合 MCP 任务及 11 路并发下载。13 MiB 随机文件中途断线后成功续传；50 MiB 上传期间状态查询仍可用。具体条件及局限见测试文档。

连接配置、认证材料、任务记录及原始测试报告继续保存在仓库外，更新保留现有外部配置。

[使用指南](https://github.com/Vinnish-A/ssh4codex/blob/v0.5.0/docs/AGENT_GUIDE.md) · [测试记录](https://github.com/Vinnish-A/ssh4codex/blob/v0.5.0/docs/TEST_REPORT.md)
