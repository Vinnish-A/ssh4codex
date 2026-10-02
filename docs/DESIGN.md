# ssh4codex 任务协议

本地 CLI/MCP → 复用 OpenSSH → 远端 Python helper → tmux 独立窗口 → 程序及日志/产物。

CLI `connect` 是独立的人工终端入口：复用相同 SSH 配置 / master，以 PTY 直接执行 `tmux new-session -A`。最近选择的会话目标按服务器配置哈希保存在本地私有状态目录，SSH 建连前原子保存以保留断线后的目标。自动任务仍只读取服务器配置 session，不受人工 connect 的选择影响；MCP 不暴露交互终端工具。connect 返回 SSH 退出码和原始终端输出，不遵循任务命令的 JSON 返回格式。

远端 helper 按源代码 SHA256 安装到 `~/.local/share/ssh4codex/runtime/<hash>/remote.py`。通过 SSH stdin 传 JSON，脚本按原文存储；上传安装为原子替换。运行中任务继续使用其不可变版本，不受软件升级影响。没有远端常驻 daemon 或新增网络监听端口。

每个任务目录包含 request.json、script、state.json、lock、stdout.log、stderr.log。取消使用 cancel 文件。提交使用文件锁和请求指纹：先发布 queued，再创建 tmux 窗口；worker 获得锁后发布 running。相同 ID/请求读取状态，不再执行。窗口创建失败记录 failed；窗口丢失且没有终态记录则 status 发布 interrupted。已经中断的任务不会自动重新启动。

worker 使用独立子进程组、单独 stdout/stderr、运行超时和取消轮询；进程退出后计算显式产物 SHA256，再原子发布终态。因此 fetch 不以文件是否已出现来推断程序结束。

不保证跨服务器磁盘丢失/人工删除任务记录的 exactly-once，不自动重试状态不明的副作用操作。原子发布针对单文件；多文件 fetch 以一次 SSH 的 tar 流携带完成时 manifest 和编号内容，边读边 SHA256 核验；全流和退出码通过后再逐文件原子交付，不解压远端文件路径。日志 cursor 是字节偏移，不是行号；UTF-8 增量返回避免截断字符，尾部读取可能以替代字符开头，skipped 告知省略数量。

本地故障分类：configuration、authentication、host_key、transport、transport_timeout、submission_unknown、protocol、setup、remote、artifact_not_ready、artifact_missing、artifact_changed、artifact_collision、transfer、transfer_unknown。错误也返回 task_id（如果已知）。正常运行不打印凭据，保留 OpenSSH 的主机密钥检查。

只读 RPC 和下载在网络故障后最多重试一次，并绕过失效 master；提交不自动重放；上传按持久 transfer_id 核验前缀后有界续传。无完整提交 JSON 时保留 submission_unknown/task_id。本地任务归属使用锁与原子记录，防止跨进程重试读取半份 JSON。status_many 复用一次 tmux pane 扫描；默认不返回日志。SSH 压缩默认开启，可按服务器关闭。

连接定义仅从仓库外的用户配置读取，必须包含 SSH 目标和 tmux 会话；运行时不包含服务器目录或部署模板。真实验收报告、认证记录和临时测试缓存保存于外部私有目录，不进入发布包。


恢复请求保存于本地私有 task 记录；recover 先通过独立连接读取状态，仅在明确 task_not_found 且用户指定 retry 时重发原请求。新增 inputs/requires 字段仅在非空时参与指纹，保留旧版普通任务的指纹兼容性。输入先完成上传，远端创建窗口前再次核对文件和环境；调用者仍负责防止任务开始后输入被改动。

上传分为 prepare、独立流和 status。transfer_id 由端点、目标路径、大小、SHA256、权限确定。prepare 返回持久部分文件的大小与前缀哈希；流从确认的偏移继续写入，完成后核对完整哈希和目标最初指纹，再原子替换。传输锁防止同 ID 并发写入，目标锁串行化多个连接器传输的提交；外部程序并不遵守此锁，不能将此机制视作跨所有程序的事务隔离。回执丢失后先查询 / prepare，已完成则直接返回。

新增错误包括 task_not_found、request_conflict、request_unavailable、preflight_failed、transfer_busy、transfer_conflict、transfer_integrity、transfer_incomplete、transfer_not_found。上传错误包含 transfer_id 和有限重试信息；非空诊断替代 SSH 返回空 stderr 的情况。保留的请求、部分文件和日志均为私有运行状态。
