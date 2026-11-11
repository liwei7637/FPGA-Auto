> 原便携包文档保留供参考。当前源码仓库的安装、目录和验证范围以 [README](../README.md) 与 [1.4.3 验证记录](VALIDATION-1.4.3.md) 为准；不包含原包 runtime/vendor 或历史硬件证据。

# XPR 选择和阶段完成通知

bit 扩展支持 `start bit` 以及 `start build --bit`；完成事件包含 bitstream 状态及成功产物路径。
build --bit 的通知边界包含 bit 文件生成，验收失败/未知时跳过生成并返回明确结果。
收到事件后按下面既有去重和回执规则处理，不能把已完成综合/实现当作 bit 成功。详见 [bit 说明](BITSTREAM.md)。

工程发现可直接使用；自动唤醒默认使用运行中 Codex 桌面提供的 app-tools 命名管道。
工具只处理已有 XPR，不自动生成、迁移工程或升级 IP。

## 目标与验收：类似中断响应

正常流程为：Codex 提交 → Python 接管并确认 Job ID → Codex 结束本轮 → Python 独立编译 →
完成/失败事件 → 唤醒同一接收任务的新一轮 → Codex 读取结果并继续授权范围内的工作。

- 提交确认后到阶段终态之前，Codex 不调用 status/logs，不使用 write_stdin 等方式保留等待中的模型循环，
  不安排 heartbeat 或定时模型调用来探测任务是否结束。用户主动询问进度不属于自动轮询。
- Python 自己等待进程、采集日志、检查超时和处理取消；这些本地程序操作不调用模型，不消耗模型 tokens。
- 只有选定阶段的最终结果需要模型决策时才发完成事件；普通进度和日志增长不唤醒模型。
- 唤醒后仍需要正常的模型输入/输出 tokens；目标是消除等待期间反复推理和读取上下文的开销。
- 端到端验收必须观察：模型轮次已结束、编译期间没有自动模型请求、Python 事件送达指定任务、
  指定任务无需用户输入开始一轮处理。仅观察 queue 返回 0、能读任务历史或另起一个 CLI 会话，均不满足验收。

start 返回 agent_action=end_turn；resume_trigger 为 completion_notification 或（未启用通知时）user_request。
唤醒通道失败时报告该阻塞，不自动转成低频轮询。纯后台手动查询或前台模式需用户另行选择。

## 工程发现契约

`discover --json` 不调用 Vivado，不创建 fpga.json/.fpga。输出包括：

- `candidates`：规范化绝对路径、工程名、清单别名、来源、是否存在、器件、顶层综合/实现 run。
- `selection`：selected/unresolved、选择理由和目标路径。清单过期、名称不唯一、配置损坏分别报告。
- `warnings`：明确目标与保存配置冲突、清单指向被默认排除的历史目录等。

精确路径（含用户链接目标）优先，缺失就报告该路径，不因缺失而构建另一个工程。
短名称只做不区分大小写的完整匹配，不做子串或模糊匹配；重名必须给路径。
没有明确目标时依次使用已有有效配置、清单中的显式 default_project、唯一活动候选。
初始化校验 XML/run，实际工程检查需调用 check；发现成功不代表 Vivado 验收成功。
已有配置不能覆盖；更换目标使用另一 config，之后所有操作和查询都复用它。

支持工程根的 `config/native-projects.json`，保持已有 projects 结构，default_project 可选：

```json
{
  "projects": {
    "i17r-opt": {"project": "fpga/UDP_I17R_OPT/UDP_I17R_OPT.xpr"},
    "desktop-current": {"project": "fpga/UDP/UDP.xpr"}
  },
  "default_project": "i17r-opt"
}
```

default_project 可以是别名、唯一完整工程名或路径，只有用户/工程配置明确指定时才设置。
清单路径相对工程根。清单失效时不自动忽略错误；明确 XPR 路径可以绕过无关损坏清单初始化。
默认跳过 build/archive/archives/backup/backups/dist、工具/依赖目录及 .runs/.cache 等产物。
`discover --include-history` 仅扩展盘点范围，不能因历史副本唯一而自动选中；历史目标用明确路径选择。

## 通知模式的前提

```powershell
& "$root/fpga-auto/fpga.cmd" notify-check --json
```

工具读取 `CODEX_APP_TOOLS_PIPE_PATH` 和 `CODEX_THREAD_ID`，检查桌面实时工具目录，
通过只读 `read_thread` 核对目标 UUID、任务类型和 local 主机。不启动 daemon、不创建任务、不投递探测消息。
`notify-check --notify-thread UUID` 可检查指定目标；省略时检查当前桌面任务。
`probe=passed` 不证明特定桌面任务可收到消息；工具不会将 `wake_verified` 标为 true。
探测失败时 `start --notify-thread` 在启动 worker 之前失败，不能悄悄退化为无人接收结果的后台任务。

worker 通过继承的本地管道调用桌面 `send_message_to_thread`，保留原任务、模型及设置。
Python 只在终态后调用一次，不启动另一个 CLI 模型会话，不修改全局 Codex 配置。
适配器遵循本机 codex-app-tools 0.1.4 的帧协议；这是随桌面版本变化的本地接口，
每次连接都检查工具目录及目标。已核对桌面版本 26.908.9136.0，升级后需要重新探测。
应用必须保持运行且账号会话可用。关闭/重启桌面可能使旧管道失效，事件仍保留在 Job 目录；
在原发送任务中执行 notify-retry 时可采用新提供的管道，不自动猜测其他桌面实例。

显式传 `--codex <codex.exe>` 才选用旧 queue 适配器及 daemon 预检；默认不依赖它，也不会自动回退。
本机旧 daemon 控制目录不存在，连接报 Windows 10050，不能据此判断桌面命名管道不可用。
独立 stdio App Server 能读历史也不等于能唤醒原桌面任务，不作为回退通道。
普通异步 Hook 完成不会启动空闲任务的新一轮，不能代替唤醒接口：
[App Server](https://learn.chatgpt.com/docs/app-server#start-a-turn)、
[异步 Hook](https://learn.chatgpt.com/docs/hooks#how-background-hooks-run)。

## 提交一个阶段

```powershell
$config = "$root/configs/opt/fpga.json"
& "$root/fpga-auto/fpga.cmd" init --root $root --config $config --project 'i17r-opt' --json
# 必须核对属于本次接收任务，不能复制历史示例的 UUID。
$threadId = $env:CODEX_THREAD_ID
& "$root/fpga-auto/fpga.cmd" start synth --root $root --config $config --notify-thread $threadId --json
```

确认 threadId 非空且属于本次接收任务后调用；工具要求完整 UUID，不支持最近任务/名称猜测。
不传 --notify-thread 是纯后台任务，没有自动唤醒能力，需要用户主动查询。
`execution=submitted` 只表示提交，编译仍可能因 project_busy、IP、许可等失败。
保存 job_id、project、config、结果路径；不要根据“最新 Job”推断自己的结果。
`worker_ready=false` 表示启动确认尚未到达，不能重复提交；查询该 job_id 即可。

worker 以 Windows detached + breakaway 方式启动。无法脱离调用方 Job Object 时返回 worker_start_failed，
不伪装为独立运行。worker 自己持有 kill-on-close Job Object；它强制退出时会清理 Vivado 子进程。
提交时记录配置快照；worker 发现 project/run/jobs/version 改变就失败，不编译变化后的目标。
源码/IP 等输入仍需按工程约定保持不变。

一次操作完成是一个通知边界。`start synth` 完成后 Codex 读取综合结果，决定是否执行 `start impl`。
用户已授权完整 build 且不需要中途决策时可 `start build`，只在完整作业结束后通知。
普通进度变化、日志增长、短时无输出不发送事件，不需要唤醒模型。

## 事件、去重和继续处理

worker 顺序：运行操作 → 清理进程/释放锁 → 保存 result.json 和终态 → completion-event.json → 通知。
事件包含 `event_id=<job_id>:terminal`、job_id、operation、execution、acceptance、reason、结果绝对路径。
通知不改变设计验收结果。

Codex 收到事件时：

1. 核对事件 Job、工程、配置和当前用户意图；用户后续停止/改目标指令优先。
2. 读取 result.json，把通知当作提示。文件缺失、状态未结束或 ID 不符时报告异常，不启动下一阶段。
   核对完成后执行 `notify-ack --config $config --job $jobId --event $eventId --json`，
   仅目标任务 UUID 可写入该事件的 `codex-received.json`；status 随后返回 wake_verified=true。
   回执与发送状态分开落盘，避免接收早于发送确认造成覆盖；收到不代表设计验收或后续工作完成。
3. 检查该 Job 目录的 `codex-decision.json`。已处理则不重复动作；上次处于处理中时先核对已有后续
   Job/产物，不能把不确定状态当成“尚未开始”而重跑。
4. 动作前记录 event_id、处理状态和计划；提交后续阶段后记录 next_job_id/config/处理结论。
   只有用户原任务已授权、当前阶段成功且下一阶段前置条件满足时才继续；通知本身不扩大授权。
5. 报告新结果或失败，等待下一事件，停止周期读日志。check/synth/impl 成功不代表完整 Timing/DRC 通过。

协议不声称跨 CLI、应用、模型动作的 exactly-once。发送端抑制已确认入队的重复调用，接收端以 event_id
和后续 Job 记录避免重复操作。传输超时存在“已发送但确认丢失”的可能，必须按不确定状态处理。

## 通知失败与停止

`notification.json` 与 result.json 独立，status 查询同时返回通知状态：

| 状态 | 含义与处理 |
| --- | --- |
| disabled | 未请求通知，纯后台执行 |
| pending | 等待本次操作结束 |
| sending | 正在投递；发送进程中断后需核对接收端 |
| queued | 桌面确认目标 UUID（旧适配器为 queue 返回 0）；不代表模型处理，重复发送命令不会再次投递 |
| failed | 发送前连接/目标校验失败，或旧 queue 返回非 0；保留错误，不重新编译 |
| unknown | 发送后连接中断、确认异常或超时，可能已入队，不自动重试 |
| suppressed | 用户取消，抑制继续通知 |
| not_started | worker 未能启动，未投递通知 |

每次发送最多 30 秒，不无限重试。修好传输后可对 failed 进行一次有记录的重投：

```powershell
& "$root/fpga-auto/fpga.cmd" notify-retry --config $config --job $jobId --json
```

sending/unknown 默认拒绝重投；确认目标任务未处理并已考虑重复事件后才使用 `--retry-uncertain`。
发送异常另存 notification-error.json，不把通知失败改写为 Vivado 失败。
worker 强制退出/机器断电可能来不及写结果或通知；status 会检查所有者，不把遗留 running 当成功。

```powershell
& "$root/fpga-auto/fpga.cmd" cancel --config $config --job $jobId --json
```

cancel 请求落盘后，worker 在前置检查和运行检查点处理，清理进程树并写 cancelled。
版本检查期间可能等其返回（上限 60 秒）；已完成任务返回当前终态，不通过 PID 盲杀进程。
取消不会自动唤醒 Codex 继续构建。已入队消息无法撤回，接收端必须遵从最新停止指令。

## 用户明确选择的前台兼容模式

原前台命令继续可用，但不满足编译期间零模型唤醒的目标，只有用户另行明确选择时才使用。
通知通道未就绪时不得自动降级为前台等待、低频模型轮询或定时模型唤醒。
保持会话存活，接收原命令完成结果；不在每次工具返回 session_id/空输出后另开状态或日志查询。
额外状态查询以用户当前明确要求或工程 AGENTS.md 为准，没有约定时最少 900 秒一次。
工具内部一秒级检查负责超时、取消和阶段文件更新，不调用模型，与 Codex 额外查询间隔无关。

## 桌面唤醒端到端测试

自动测试覆盖管道分帧、连接失败、不确定发送、去重与接收回执。
真实唤醒测试必须单独执行，不能把这些模拟测试或 notify-check 当作验收。

从需要接收事件的当前桌面任务执行包内
`runtime/python.exe -B tests/verify_desktop_wakeup.py --launch --rollout <当前任务的绝对 jsonl 路径>`，
随后结束模型轮次。测试直接用正式 start 路径创建单层 worker；其夹具只读该任务本地记录，
等待本轮 task_complete，再运行 8 秒模拟阶段，
由正式 worker 通知路径发送一次事件。测试不访问真实 FPGA 工程，不调用真实 Vivado。
不要由已脱离的测试进程再做第二次 breakaway；Windows 外层 Job 可能禁止再次脱离。
接收任务执行通知中的 notify-ack，然后执行该脚本 `--verify <verification.json>`，
检查原轮次结束、阶段期间零新模型轮次、同一任务后续轮次及匹配事件回执，写入 verified.json。
还必须在该后续轮次找到 codex_app.send_message_to_thread 投递的准确 event_id；用户手动发消息不能冒充自动唤醒。
证据保存在工具包 `.fpga/wakeup-verification/<测试ID>/`；只检查明确的测试路径。
本机 2026-09-16 实测通过，桌面版本 26.908.9136.0，codex-app-tools 0.1.4：

- 接收任务始终为同一原桌面任务，按完整 UUID 核对。
- 北京时间 16:29:04.503 原轮次结束，16:29:05—16:29:13 运行模拟阶段，16:29:13.797 自动开始下一轮。
- 发送次数 1；阶段期间新增模型轮次 0；接收任务完成 notify-ack，wake_verified=true。
- 后续轮次记录的触发内容是 codex_app.send_message_to_thread 完成事件，未依赖用户再次发消息。
- 原始回执保存在开发工作区；便携包附带 发布验证摘要（历史便携包文件，未纳入本源码仓库），不包含本机运行目录。

这验证的是 Python 阶段完成 → 原桌面任务自动恢复；阶段使用模拟进程，没有执行真实工程综合。
更换桌面版本/运行环境后应重新探测和验收，不将这一次实测作为所有安装环境的保证。
