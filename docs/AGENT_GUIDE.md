# Agent 使用指南

本软件提供 CLI 和 stdio MCP。所有部署定义、认证材料、会话选择和运行记录都由使用环境提供，保存在仓库外。发布包不包含可连接的服务器、账号、密钥部署记录或预设会话。

## 安装

下载 Release 解压或使用 README 中的安装器。Linux x86_64 / WSL、glibc 2.35+；客户端内含 Python、OpenSSH 和 MCP SDK。远端需 Python 3.10+、tmux 和 SSH 服务。安装更新保留仓库外的用户配置与密钥。

## 外部配置

连接定义从 `~/.config/ssh4codex/config.json` 读取，可用 `SSH4CODEX_CONFIG` 指定另一个外部文件。软件不提供内置定义或部署模板；服务器目标和 tmux 会话必须显式设置。没有配置时返回 configuration 错误，不尝试预设地址。

用 CLI 保存自己的定义。以下大写参数都是调用者提供的占位符：

```bash
ssh4codex add SERVER --target SSH_TARGET --session TMUX_SESSION --cwd REMOTE_DIRECTORY
ssh4codex doctor SERVER
```

端口、私钥路径、SSH 配置路径和传输超时等选项见 `ssh4codex add --help`。使用 OpenSSH 的已有授权密钥或 ssh-agent，保留主机身份校验；不要把凭据、主机清单或密钥记录写回此仓库。

## 人工接入

```bash
ssh4codex connect SERVER
ssh4codex connect SERVER --session TMUX_SESSION
```

在交互终端一步接入 SSH / tmux。首次进入外部定义的会话，之后回到最近通过 connect 选择的目标。目标在建连前原子保存，连接失败或中途断开也保留；不追踪 tmux 内部手工换会话。已有会话直接接入，不存在则创建，不分离其它客户端。默认快捷键 Ctrl-b、d 分离并保留程序；不存在的原进程不会通过重建会话恢复。

connect 继承终端输入输出并返回 SSH 退出码，不返回 JSON，也不作为 MCP 工具。自动任务使用外部定义的会话，不随人工选择改变。

## 自动任务

```bash
ssh4codex run SERVER --script SCRIPT --interpreter INTERPRETER --artifact ARTIFACT --wait 2
ssh4codex status-many SERVER TASK_A TASK_B
ssh4codex wait SERVER TASK_ID --seconds 10
ssh4codex status SERVER TASK_ID --stdout-cursor CURSOR --stderr-cursor CURSOR
ssh4codex fetch SERVER TASK_ID --to LOCAL_DIRECTORY
ssh4codex put SERVER LOCAL_FILE REMOTE_FILE
ssh4codex cancel SERVER TASK_ID
ssh4codex list SERVER
```

每个脚本在配置的 tmux 会话中新建独立窗口。任务不继承已有交互 pane 的 conda 环境，需明确传解释器和环境变量。不要向已有用户或模型 pane 注入输入。

保存 task_id、state、exit_code 及每个日志流的字节 cursor。CLI 0 可以表示任务仍运行；1 表示任务失败或终止；2 表示配置、连接或文件等错误。以 state 判定终态，不能把工具成功返回当成程序完成。

wait 最多等待 60 秒，不终止正在运行的任务；run 的 timeout 才限制运行时间。提交确认丢失时保存原 task_id，先查询；只有原 ID / 完全相同请求可去重重试，不能换 ID 自动重放。服务器状态被删除或任务 interrupted 时不自动重跑。

日志每流默认最多 2048 字节。status 用保存的 cursor 读取新增部分，remaining / skipped 表示未读与省略范围；需要全文时从零分段读取。多个任务优先一次 status-many，默认不返回日志。

取消只作用于选定任务的进程组，保留共享会话。自行 daemonize 的程序不在该保证范围。只读观察和下载遇到传输故障最多重试一次并绕过失效 master；提交和取消不盲目重放；上传在核对已接收前缀后进行有界续传。

## 文件与协作

上传和完成后的产物下载核验 SHA256。下载使用单个 SSH 流，全部文件和退出码验证后逐文件原子交付；不承诺多文件事务；下载不支持断点续传。查看图像需取回并实际检查。

多个 agent 使用独立工作目录和任务 ID，共享一致的外部 SSH 定义。子 agent 返回任务 ID、终态和产物路径 / 哈希，主 agent 批量核验后读取远端产物汇总。不要把完整矩阵或重复日志塞入上下文。

## 私有运行记录

本地状态默认 `~/.local/state/ssh4codex/`，可用 `SSH4CODEX_STATE` 覆盖；最近会话记录按连接定义哈希隔离，权限 600。远端 helper 和任务保存在用户自己的 `~/.local/share/ssh4codex/`。这些均不属于仓库或 Release。

真实测试需显式传入服务器别名；原始报告默认写入仓库外的 `~/.local/state/ssh4codex/reports/`，可用 `SSH4CODEX_REPORTS` 更改。测试缓存默认在外部 `~/.local/state/ssh4codex/tests/`，可用 `SSH4CODEX_TEST_STATE` 更改。原始报告可能含部署元数据，不应直接发布。

## MCP

使用已安装 `ssh4codex-mcp` 的绝对路径注册 stdio 服务，具体注册位置由客户端管理。提供 remote_run、remote_status、remote_status_many、remote_wait、remote_cancel、remote_fetch、remote_tasks、remote_put、remote_recover、remote_poll、remote_transfer_status、remote_doctor。优先读取 structuredContent，保存状态和游标；不通过 MCP 附着交互终端。

## 断线恢复与续传

```bash
ssh4codex recover SERVER TASK_ID
ssh4codex recover SERVER TASK_ID --retry
ssh4codex put SERVER LOCAL_FILE REMOTE_FILE --progress
ssh4codex transfer-status SERVER TRANSFER_ID
```

`recover` 使用独立 SSH 连接查询原任务。发现任务时直接返回状态，即使带 `--retry` 也不会重新执行。只有明确返回 `task_not_found` 且指定 `--retry` 才提交本地保存的原请求，保持原 ID、脚本、环境和输入校验信息；连接不可用时不推断任务不存在。旧版仅保存 ID 的记录可以查询，但无法自动重建请求。人为删除远端任务记录会丢失去重依据，恢复命令无法保证这种情况下不重复执行。

上传使用独立 SSH 连接和持久临时文件。相同目标、内容、权限和连接端点生成相同 transfer_id；重试先核对已接收前缀的 SHA256，再发送剩余内容，完整哈希通过后原子替换。默认最多重试两次；`--retries 0..5` 调整预算。最后回执丢失也会先核验是否已经完成。`transfer_unknown` 包含 transfer_id，可重新执行相同 put 续传，不需要自行 base64 编码或拆分文件。

`--progress` 在 stderr 输出 transfer_id 和续传检查点，stdout 保持一个最终 JSON。用另一调用执行 transfer-status 查看远端 `bytes_received`；只有 `phase=complete` 表示已经交付。接收字节数并不等于已通过完整哈希校验。保留的部分文件和记录不会自动清理，便于跨进程恢复；它们位于远端记录目录及目标目录中的 `.ssh4codex-*.part` 文件。

若目标在一次未完成的传输期间被另一个任务改动，返回 `transfer_conflict`，保留现有目标。已完成传输的记录也不会被用于覆盖后来修改的文件；请改用新的目标路径，或明确处理旧记录后再开始有意覆盖。上传源文件应在传输期间保持不变。续传解决已送达数据的重复传输，不会消除网络拥塞或增加链路带宽。

## 提交前准备输入与环境

```bash
ssh4codex run SERVER --script SCRIPT --input LOCAL_FILE=ABSOLUTE_REMOTE_FILE --require EXECUTABLE
ssh4codex doctor SERVER --require EXECUTABLE
ssh4codex run SERVER --script SCRIPT --profile PROFILE
```

每个 `--input` 先完成校验上传，任一个失败都不提交任务。远端创建任务窗口前再次核验输入文件大小、哈希、解释器、工作目录与 `--require` 可执行程序。明确缺失返回 `preflight_failed`，不再让下游脚本因为未上传的资源立即失败。这不是文件锁：任务开始后仍须避免其它任务修改同一输入。

重复使用的执行环境可保存在仓库外连接定义的 `profiles` 映射中。每个自定义名称对应可选 `cwd`、`interpreter`（参数列表）、`env`（字符串映射）和 `requires`（可执行程序名称列表）。run 的显式 cwd/interpreter 覆盖对应 profile 值；env 合并并以显式值优先，requires 合并去重。doctor 可指定同一 profile。软件不自动继承已有交互 pane 的环境，也不内置任何项目解释器或配置。

任务请求包含脚本和环境变量，以权限 600 原子保存在外部本地状态目录中，用于恢复。不要发布这些记录；不需要恢复时可自行删除。服务器目标改变后不会将旧请求自动重放到新目标。

## 减少重复调用与日志

```bash
ssh4codex poll SERVER TASK_ID --consumer AGENT_ID
ssh4codex poll SERVER TASK_ID --consumer AGENT_ID --reset
ssh4codex run SERVER --script SCRIPT --artifact ARTIFACT --fetch-to LOCAL_DIRECTORY
ssh4codex wait SERVER TASK_ID --fetch-to LOCAL_DIRECTORY
ssh4codex status SERVER TASK_ID --fresh-connection --rpc-timeout SECONDS
```

poll 将游标按连接端点、任务、consumer 隔离保存。每个 agent 使用自己的 consumer 名称；同名并发读取由锁串行化。需要回看时用 `--reset` 或显式 status 游标。游标在本地保存后若工具回复丢失，下一次 poll 可能已经前进，所以它不是日志的 exactly-once 消费协议。

`--fetch-to` 只在本次返回 succeeded 时下载声明的产物；仍在运行时不提前下载。交付失败保留 succeeded 任务状态，另在 delivery 中报告错误，CLI 返回 2，直接重试 fetch 即可，无须重跑任务。

CLI 网络命令支持 `--fresh-connection`、`--rpc-timeout` 和 `--transfer-timeout`；Python Client 构造函数也支持同名下划线参数。需要绕过异常 master 时使用单次独立连接，不必 disconnect 影响其它调用。SSH 的连通性检测与调用超时仍决定故障被发现的速度。
