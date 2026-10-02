# 开源连接工具调研

核对日期：2026-10-02。查看官方仓库 README / 部分实现；没有安装或跑这些项目的横向性能测试。以下是文档能力与本项目的需求取舍，不是对其质量的排名。

| 项目 | 官方描述/可复用能力 | 本项目决定 |
|---|---|---|
| [mcp-ssh-tmux](https://github.com/devnullvoid/mcp-ssh-tmux) | tmux 持久 SSH、终端 snapshot、文件访问；其理念是模型观察屏幕 | 可参考交互模式，批处理不采用：本次要把退出码和任务状态交给程序判断 |
| [slepp/ssh-mcp](https://github.com/slepp/ssh-mcp) | 系统 OpenSSH、CLI 配置、远程 exec、持久 shell、传输与 MCP | 最接近的通用现成方案；沿用系统 OpenSSH 的思路。本项目集中于远端 tmux 的任务状态、幂等及产物约定 |
| [talkincode/sshx](https://github.com/talkincode/sshx) | Agent-oriented JSON 执行、脚本、文件操作、凭据管理、MCP | 可作为一次性执行替代；所读 README 没有发现本次所需的 tmux task_id 断线恢复合同。不推断它永远不能增加这些能力 |
| [AsyncSSH](https://github.com/ronf/asyncssh) | 异步 SSH、多通道、SFTP、认证与跳板能力 | 第一版不用：已有 OpenSSH 已能复用连接并兼容用户 SSH 配置，无需新增另一套 SSH 协议栈 |
| [ekzhang/sshx](https://github.com/ekzhang/sshx) | 协作式 Web 终端、重连、实时共享 | 更适合人工共同观察终端；不是本次主要任务 API |

没有复制这些项目源代码。本项目直接调用系统 OpenSSH 和 tmux；MCP 使用[官方 Python SDK](https://github.com/modelcontextprotocol/python-sdk)。OpenSSH 的复用、超时与认证配置依据[官方手册](https://man.openbsd.org/ssh_config)，tmux 控制与持久会话依据[官方文档](https://github.com/tmux/tmux/wiki/Control-Mode)。

当前不需要叠加安装多个 SSH MCP 服务。一个系统 SSH 传输层、一个可审阅的标准库远端 helper，以及薄 MCP 适配即可覆盖本次科研任务。若后续主要任务变成交互终端，再评估直接采用现成 terminal MCP，而非把当前脚本 runner 扩成终端模拟器。
