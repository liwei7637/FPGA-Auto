# Vivado 调试专用 MCP 1.4.2

面向 Windows x64、Vivado **2020.2** 的独立 ILA/VIO 调试服务。通用层处理会话、器件绑定、VIO、ILA、任务和证据；
双 FPGA 10G UDP 是第一个场景插件。原 `fpga.cmd` 构建入口和完成通知协议保留。

此仓库保留 1.4.2 调试源码。默认 mock 后端，启动服务不连接 hw_server。历史包的硬件验证结果不属于本版；本次验证范围见 [VALIDATION-1.4.3.md](VALIDATION-1.4.3.md)。

构建 CLI 只用标准库。调试 CLI 需要 Python 3.10+，MCP 另需在本机 CPython 3.14 虚拟环境安装 `requirements-mcp.lock` 中的依赖，安装步骤见根 README。源码仓库没有 runtime/vendor。

```powershell
# 在共享工具目录运行，工程根显式指定。
.\vivado-debug.cmd info
.\vivado-debug-mcp.cmd --root 'D:/work/project' --list-tools
.\vivado-debug-mcp.cmd --root 'D:/work/project'
```

Codex 配置模板位于 `examples/codex-mcp.example.toml`，替换解释器、工具和工程路径后使用。SDK 锁定为 mcp 2.3.0。模拟结果、回放和真实板测分别记录。本服务不提供下载、Flash、GT 复位、任意 Tcl 执行或 IP 升级接口。

## MCP 工具

| 工具 | 作用 |
|---|---|
| vivado_debug_info | 查看版本、模式和验收边界 |
| vivado_profile_describe / vivado_ltx_describe | 离线描述工程配置、原 LTX 物理端口和 SHA-256 |
| vivado_session_open / get / close | 创建、查看、关闭持久调试会话 |
| vivado_hw_status | 读取明确绑定的器件 DNA、DONE、CRC 和配置状态 |
| vivado_vio_read / vivado_vio_apply | 读取物理端口；批量设置输出、一次提交并回读 |
| vivado_udp_snapshot | UDP 插件：触发硬件快照，完整读取八页并检查快照编号 |
| vivado_ila_capture_start | 提交限时采集，保存原 ILA 与 CSV、哈希及窗口说明 |
| vivado_recipe_start | 双端配置、启动、停止、排空和收发统计比对 |
| vivado_job_get / vivado_job_cancel | 明确 Job ID 的结果查询和取消 |
| vivado_capture_analyze | 离线 ILA CSV 摘要或 UDP 快照回放 |
| vivado_mac_to_pcap | 离线 MAC CSV 转 PCAP，保留部分帧及采样范围说明 |

端口使用 `in0`、`out0` 等物理编号，核心名来自 profile 的别名。VIO 数值输入为十进制或 `0x` 字符串；
输出同时给出精确 hex/decimal 字符串。64 位包数和计数不得经浮点数转换。
VIO 写入、SNAPSHOT、统计翻页和 ILA 触发会改变状态，工具已标记为非只读。

## 离线会话与场景

```powershell
# 返回 session_id，示例后续命令中的 SESSION 必须替换为该值
.\vivado-debug.cmd --root 'D:/work/project' open --backend mock
.\vivado-debug.cmd --root 'D:/work/project' submit SESSION hw_status
# 参数文件示例在 examples/udp-recipe.json；返回明确 job_id
.\vivado-debug.cmd --root 'D:/work/project' submit SESSION recipe --params-file examples/udp-recipe.json
.\vivado-debug.cmd --root 'D:/work/project' job JOB
.\vivado-debug.cmd --root 'D:/work/project' submit SESSION close
```

`wait JOB` 仅供明确选择的前台兼容或程序测试使用；代理正常长任务采用完成事件，不保持等待中的模型循环。
提交后的模拟任务不会因原 MCP 连接关闭而主动取消。会话默认空闲 900 秒后释放自身资源；关闭会话不等于停止 FPGA 上所有流量。
Windows 宿主若禁止子进程脱离其 Job Object，返回 `worker_start_failed`，不伪装成后台成功。
此类宿主可先从允许独立 worker 的终端通过 CLI 打开会话，再由 MCP 使用其 session_id；不自动启动系统服务或计划任务绕过限制。

默认场景使用两颗模拟 FPGA。可选 `normal`、`drop_tail`、`not_ready`、`device_lost`、`write_timeout`、`ila_timeout`、`stop_stuck`。
这些模式只用于离线验证。自定义 JSON LTX 可先通过以下命令生成通用配置，复核后使用：

```powershell
.\vivado-debug.cmd profile-from-ltx 'D:/captures/design.ltx' 'D:/captures/profile.json' --part xcku040
```

profile 记录核 UUID、cell、方向和位宽；machine 另行记录 hw_server、精确线缆路径、96 位 DNA、IP/MAC。
配置模板为 `examples/switch-udp-machine.example.json`，占位 DNA 故意不能用于真实连接。
同一 BIT 无 A/B 角色宏，运行身份只在 machine 中配置。本工程历史记录 A=.129、B=.128，不能按枚举次序猜物理身份。

## 真实后端的使用边界

执行明确授权的硬件任务时，服务需以 `--enable-hardware` 启动，并显式选择 `backend="vivado"`、
原 LTX、Vivado 路径和完整 machine。缺少其中任何必要绑定，或 UUID/位宽/DNA 不符，均拒绝写入。
未配置器件可读取配置状态，探针操作要求 DONE=1、CRC=0。不会恢复程序或重新下载。

服务创建自己的 Vivado 2020.2 Tcl 进程，通过私有文件邮箱传递命名操作；不附着或修改用户 GUI/XPR，不依赖新版 Vivado webserver。
会话用 DNA 的跨工程 OS 锁保护本服务的访问，不能约束外部 GUI、MCU 或其他软件。外部操作仍可能使测量中断，服务保留诊断记录。
原 LTX 保持字节不变；会话目录中的派生 LTX 只创建唯一的物理整端口显示名，不修改 UUID、端口覆盖和硬件设计。
核 UUID 只证明调试布局匹配，不代表已读回并验证完整 BIT。

已在真实 2020.2 中离线验证命令加载和全部替身 API 桥接。2020.2 的 Labtools 命令按需加载；
不存在 `stop_hw_ila`，取消采集使用 `upload_hw_ila_data` 停止并上传可能不完整的缓存，不把该缓存视为成功采集。
1.4.1 按逻辑任务刷新器件及探针状态，不在 VIO 批量写入或 ILA arm/wait/upload 中途刷新。
1.4.2 与 GUI 使用相同的 `-allow_non_jtag` 连接参数，并主动枚举目标；仅在连接阶段针对 44-494/空目标执行有限重连。
后续仍逐一校验明确的 target、part、DNA、核 UUID 和位宽，连接重试不重放任何 VIO 或 ILA 操作。
2020.2 的 `CONTROL.TRIGGER_MODE` 为只读；`wait_on_hw_ila -timeout` 按分钟接收小数，服务换算为每次约一秒等待。
等待命令正常返回也可能只是到达限时；仅在 ILA 状态为 FULL 且采样数匹配请求深度时接受完成。
超时停止后检查核心已处于 IDLE/FULL，再确认停止；部分/空缓存不作为有效采集。
每次采集清除上次的比较条件；支持 Vivado 的 `neq`，并兼容原 `ne` 简写。断电/掉线及双端打流覆盖仍须后续 RUN。

## UDP 场景的判据

`recipe="switch_udp_v1"` 支持 forward/reverse/duplex、32～1472 字节 payload、递增/PRBS31、固定包数和限时持续测试。
默认 gap80、1472B、PRBS31、10000 包。配置提交成功后才启动，结束后确认发送空闲、等待选中流计数稳定，再冻结最终结果。
比较跨端总包数、payload 字节、正确字节、坏包、序号、头/长度/tuser、中断、ready 下降和 FIFO 事件。
即使序号缺口为 0，也通过跨端 TX/RX 总量发现尾部丢包。未知数据保留为 invalid/INCONCLUSIVE。

持续测试按每端两次原子快照的正确字节差与硬件周期差计算 UDP payload 吞吐率。
它包含 32 字节测试头，使用标称 200 MHz；两端 JTAG 快照不保证同步，也不是线速/FCS/逐 bit BER 证明。
START 通过 JTAG 依次执行，不声称严格同步。共享交换机下的缺包不会自动归因 RTL、交换机或线路。

CSV→PCAP 只适用于此插件的 MAC RX/TX 布局；仅计 valid&&ready，默认丢弃窗口首尾半帧。
采样缺失的拍无法还原；时间为 FPGA 相对 tick，不是实际日期，数据不含前导码/FCS。

## 任务、结果与通知

会话：`<root>/.fpga/debug/sessions/<session_id>/`。任务：`<root>/.fpga/jobs/<job_id>/`。
任务保留 request/state/result、动作意图与回读、原始快照/ILA/CSV及哈希。新增字段在 `result.debug`，不改变原构建验收字段含义。
`execution` 表示执行结果；`debug.measurement_validity` 表示数据有效性；`debug.verdict` 表示具体测试结论；`source_kind` 区分 simulated/hardware/historical_replay。
采集执行成功不自动产生板级 PASS。旧字段 `hardware_validated_by_this_release=false` 保留以兼容原结果；
完整硬件覆盖仍未完成，已验证的单板范围以本版验证文件和实际 RUN 为准，不用该字段替代测量结论。

长任务可传当前接收聊天的 `notify_thread`，逐次验证该 UUID 和桌面管道，不能使用 MCP 启动时遗留的聊天 ID。
复用 FPGA Auto 的完成事件、投递去重和回执：结果落盘和本次采集/打流收尾后再通知；通知不是执行下一步或操作硬件的授权。
会话的器件锁持续到关闭会话，以保护随后的交互；原构建工程锁释放与通知顺序未改。
通知 queued 不等于模型已接收；失败不重跑任务；unknown 不盲目重发；用户取消抑制后续通知。
回执仍可用原 `fpga.cmd notify-ack --config '<root>/vivado-debug.json' --job JOB --event EVENT --json`。
该 config 路径用于定位任务集合，无需初始化或覆盖构建用 fpga.json。

VIO 提交或回读不确定时，会话禁止后续写入；关闭后重新核对，不自动重放 START/CLEAR/APPLY。
取消结果单列清理确认。器件离线或无法确认 STOP 时保存 unconfirmed，不把取消请求当成“已停止硬件”。

## 验证与发布

```powershell
.\runtime\python.exe -B -X utf8 fpga-auto/tests/run_offline_suite.py --output 'D:/evidence/new-suite'
# 独立离线 Vivado 检查：不存在的新目录；不打开工程或连接硬件
.\runtime\python.exe -B -X utf8 fpga-auto/tests/verify_debug_2020_2.py --vivado 'D:/Xilinx/Vivado/2020.2/bin/vivado.bat' --output 'D:/evidence/new-offline-check'
```

历史样本来源、哈希见 `tests/fixtures/debug/PROVENANCE.json`。发布验收见 `RELEASE-VALIDATION-1.4.2.json`，旧记录保留。
原构建回归与新 MCP 回归分别在独立进程执行，保持原标准库入口不加载可选 SDK；测试分别位于 tests 与 debug_tests。
模拟/回放验证不修改工程 TEST_MATRIX、STATUS 或板级 RUN。新 ZIP 附 SHA-256 和全文件清单；旧 1.3.0 ZIP 保留。
