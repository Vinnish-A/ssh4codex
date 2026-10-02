# ssh4codex 任务协议

本地 CLI/MCP → 复用 OpenSSH → 远端 Python helper → tmux 独立窗口 → 程序及日志/产物。

远端 helper 按源代码 SHA256 安装到 `~/.local/share/ssh4codex/runtime/<hash>/remote.py`。通过 SSH stdin 传 JSON，脚本按原文存储；上传安装为原子替换。运行中任务继续使用其不可变版本，不受软件升级影响。没有远端常驻 daemon 或新增网络监听端口。

每个任务目录包含 request.json、script、state.json、lock、stdout.log、stderr.log。取消使用 cancel 文件。提交使用文件锁和请求指纹：先发布 queued，再创建 tmux 窗口；worker 获得锁后发布 running。相同 ID/请求读取状态，不再执行。窗口创建失败记录 failed；窗口丢失且没有终态记录则 status 发布 interrupted。已经中断的任务不会自动重新启动。

worker 使用独立子进程组、单独 stdout/stderr、运行超时和取消轮询；进程退出后计算显式产物 SHA256，再原子发布终态。因此 fetch 不以文件是否已出现来推断程序结束。

不保证跨服务器磁盘丢失/人工删除任务记录的 exactly-once，不自动重试状态不明的副作用操作。原子发布针对单文件；多文件 fetch 会逐一核验交付。日志 cursor 是字节偏移，不是行号；UTF-8 增量返回避免截断字符，尾部读取可能以替代字符开头，skipped 告知省略数量。

本地故障分类：configuration、authentication、host_key、transport、transport_timeout、submission_unknown、protocol、setup、remote、artifact_not_ready、artifact_missing、artifact_changed、artifact_collision、transfer、transfer_unknown。错误也返回 task_id（如果已知）。正常运行不打印凭据，保留 OpenSSH 的主机密钥检查。
