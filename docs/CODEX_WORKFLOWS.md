# Codex 工作流实测

2026-10-02，在 Solvinglab 的 `tmux data` 中测试已安装的独立 Release。主 Codex 启动了三个真实 subagent：一个分析表达矩阵并绘图，一个调度混合 MCP 任务，一个测试跨进程交接。主 agent 另外注入网络故障，最后独立核验子任务状态，并提交远端汇总任务读取各 agent 的实际产物。

远端运行的是 Python / R 脚本，没有调用服务器上的 Codex，也没有通过 `send-keys` 操纵已有交互 pane。所有输入为合成数据，工作目录隔离在 `/tmp/ssh4codex-codex-…/`，没有修改科研项目结果。

## 分析、任务依赖与产物

分析 subagent 生成固定种子的 300 基因 × 12 样本表达矩阵，上传后调用远端 tidy 环境的 Rscript。统计任务输出差异表、组统计和 qs 缓存；后续绘图任务读取该缓存，输出 ggplot2 PNG / PDF。

独立 Python 核验得到 20 个上调、20 个下调、260 个无变化，与预设真值逐基因一致。R 和 Python 算出的表达差值最大误差为 `4.9e-15`。两份输入和七份产物均核对 SHA256 / 大小，PNG 由 agent 实际查看。该测试证明连接器可支持分析与依赖绘图，不代表对真实生物数据的算法有效性评估。

## 多任务与真正的 subagent

三个真实 Codex subagent 共享服务器配置和 SSH master，各自使用独立工作目录。多任务 subagent 还通过一个官方 MCP SDK stdio 会话并发提交 14 个远端脚本：计算、表格、进度输出、指定退出码 7、超时以及选定任务取消。

这里的 14 个 worker 是远端脚本，不能说成 14 个 Codex 模型。只有主 agent 启动的三个才是本次真实 Codex subagent。

预期终态为 11 个 succeeded、1 个 failed、1 个 timed_out、1 个 cancelled。批量状态查询不携带日志；只读取进度和错误任务的日志并保存字节游标。11 个成功任务的文件内容逐项验证。取消时其它任务仍在运行，并继续完成。

## 跨进程交接与断线恢复

交接测试让 producer 客户端提交后退出，确认远端任务仍为 running。使用空本地状态目录启动 consumer，由远端列表找到任务、等待并下载，再提交依赖该文件的汇总任务。

三个独立进程同时提交同一 task_id 和完全相同请求，实际只有一个新任务，另外两个返回 reused；远端追加记录证明只执行一次。不同请求复用同一 ID 被拒绝。交接场景已独立运行两次通过。

网络测试通过独立 TCP 代理给链路增加延迟和抖动。在另一条连接确认分析任务 running 后，主动断开代理链路，让提交确认丢失。调用方收到 `submission_unknown` 和原 task_id；新 agent 状态目录通过该 ID 恢复、等待、下载并核验 50,000 行计算结果。原 ID / 原请求再次提交返回 reused，执行次数为一。故障只作用于专用测试连接。

## 实际发现并修复的问题

0.2.0 的首次多任务测试中，14 个任务终态都正确，11 个下载文件也已在磁盘上，但 MCP 会话关闭。进一步探测发现：冷工具 schema 缓存下，串行和两路下载通过；四路和十一路下载只返回一个结果，随后等待 `tools/list` 超时。预先加载工具 schema 后，十一路下载和十二个状态请求又能完成。

根因是下载的 SSH 子进程继承了 MCP 标准输入。SSH 会读取并转发后续 JSON-RPC 请求，导致 MCP 服务端看不到它们。关闭会话时出现的 `ClosedResourceError` 和 SDK 字典迭代异常是清理阶段的错误，不能据此把下载失败归因于网络。

0.2.1 使用 `stdin=DEVNULL` 隔离下载进程输入，保留其它 RPC / 上传需要的输入管道。新增单元回归和全新 MCP 会话的十一路并发下载回归；回归不预加载 `tools/list`，直接触发原故障条件。原失败记录保存在 [中间报告](../benchmarks/codex_multitask_intermediate.json)，没有覆盖为成功。

## 测量与最终独立核验

| 场景 | 独立程序版本 | 测量耗时 | 结果 / 报告 |
|---|---|---:|---|
| R 分析、qs 和依赖绘图 | 0.2.0 | 6.658 秒 | 8 次 CLI 调用，返回 3,833 字节；[报告](../benchmarks/codex_analysis.json) |
| 跨进程交接及三进程同 ID 竞争 | 0.2.0 | 15.387 秒 | 16 次 CLI 调用，返回 6,262 字节，实际执行一次；[报告](../benchmarks/codex_handoff.json) |
| 提交确认丢失、换 agent 恢复 | 0.2.0 | 14.854 秒 | 10 次 CLI 调用，返回 5,506 字节，50,000 行计算正确；[报告](../benchmarks/codex_network.json) |
| 14 个混合 MCP worker | 0.2.1 | 工作负载 27.909 秒；完整测试 38.004 秒 | 预期终态全部匹配；[报告](../benchmarks/codex_multitask.json) |
| 11 份文件并发取回 | 0.2.1 | 0.414 秒 | 30,055 字节，11 份内容和 manifest 均核验 |
| 全新 MCP 会话的 11 路下载回归 | 0.2.1 | 0.941 秒 | 不预加载工具 schema，11 个调用全部响应 |
| 主 agent 批量核验及远端汇总 | 0.2.1 | 3.431 秒 | 21 个源任务、24 份远端产物；[报告](../benchmarks/codex_coordinator.json) |

主 agent 一次无日志批量查询核验 21 个源任务：18 个成功，以及预期的失败、超时和取消各一个。汇总脚本直接读取三个 subagent 和网络测试的远端文件，核验全部 24 份文件的 SHA256 / 大小，再交叉检查统计真值、表格、计算和执行计数。取回汇总后，主 agent 在本地独立重算核对，不只相信子 agent 的文字报告。

多任务测试的 27.909 秒包含人为设置的 25 秒进度任务，不能当作连接器纯开销。完整测试包含读取约 29.7 KB 的完整诊断日志和两次文件取回，共 78 次工具调用。SDK 返回 JSON 约 305.6 KB，含 text / structuredContent 两种表示，不是模型实际 token 消耗，也不是 SSH 线路字节数。日常轮询应直接用一次批量状态、仅取必要日志，不照搬压力测试的频繁轮询。

这些时间是当次运行测量，包含网络及远端负载，不含 Codex 思考和写代码耗时。0.2.0 与 0.2.1 混合记录被明确保留：前三项在旧独立程序完成；修复涉及的 MCP 并发和最终汇总在 0.2.1 完成。可靠性改进不能包装成普遍速度倍数。

## 如何复现

安装 Release、配置自己的授权密钥后，在仓库目录运行。测试驱动本身需要本地 Python / MCP SDK；这与被测试的独立 Release 无 Python / SSH PATH 要求不同。远端分析测试还依赖指定的 R tidy 环境；使用其它服务器需调整 Rscript 路径。

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[mcp,test]'
.venv/bin/python tests/codex_analysis.py --run
.venv/bin/python tests/codex_handoff.py --run
.venv/bin/python tests/codex_multitask.py --run
.venv/bin/python tests/codex_network.py --run
.venv/bin/python tests/codex_coordinator.py --run
```

Coordinator 读取以上四份报告中的远端路径和任务 ID，因此应在这些任务产物仍保留时运行。其它测试均使用新 ID / 专用目录，成功后将最新测量写入 `benchmarks/codex_*.json`。测试不会结束共享 tmux 会话。

源码、测量和合成任务 ID 可以公开；输入矩阵、图片、完整日志、真实配置和私钥留在忽略的 `.local/` 中。报告中的远端临时路径不是稳定下载链接。
