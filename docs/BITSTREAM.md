> 原便携包文档保留供参考。当前源码仓库的安装、目录和验证范围以 [README](../README.md) 与 [1.4.3 验证记录](VALIDATION-1.4.3.md) 为准；不包含原包 runtime/vendor 或历史硬件证据。

# bit 生成（1.3.0，2026-09-17）

新增 `bit` / `start bit`，以及 `build --bit` / `start build --bit`。原 `build` 仍只做综合、实现和验收。

```powershell
# 已有有效实现结果：重新验收后生成 bit，不重跑综合/实现
.\fpga-auto\fpga.cmd bit --root '工程根目录' --config 'fpga.json' --json

# 从综合开始执行完整流程并生成 bit
.\fpga-auto\fpga.cmd start build --bit --root '工程根目录' --config 'fpga.json' --notify-thread '当前会话UUID' --json

# 只将已有实现转换为 bit，也支持后台和自动唤醒
.\fpga-auto\fpga.cmd start bit --root '工程根目录' --config 'fpga.json' --notify-thread '当前会话UUID' --json
```

菜单选项 7 为独立生成 bit，选项 8 为完整构建并生成 bit。`--bit` 仅用于 build，不能附加到 synth/impl 等命令。

## 生成条件及执行顺序

独立 bit 要求选定综合及实现 run 已完成、未过期，且实现已完成布线或启用的布线后物理优化。读取现有结果并重新生成 Timing/DRC 报告，不使用其他历史 Job 的验收结果。

请求 bit 时，DRC 使用 Vivado 2020.2 的 `bitstream_checks` rule deck。沿用既有报告解析和验收策略：时序必须通过，DRC 不能含错误或严重警告；报告缺失或无法可靠解析时为 unknown。只有验收通过才进入 bit 阶段，不降低 DRC 严重性。

当前流程的报告验收位于 Python 层。因此同一个 worker、Job 和工程锁内先完成报告及验收，再调用第二次 Vivado batch，打开有效实现并执行 `write_bitstream`。两次调用共用一个总超时，阶段切换检查取消请求；第二次调用清除上一次完成标志。

本扩展生成普通完整 `.bit` 文件，使用实现设计中保存的 bitstream 属性。直接写独立产物，不执行 XPR 中 write_bitstream run 的 Tcl PRE/POST hooks 或额外命令行格式选项；本次不新增 bin/mcs、烧录、IP 升级或 DFX 多产物管理。

## 产物与结果

每个 Job 独占 `<Job目录>/artifacts/`，输出 `design.bit` 及 `bitstream.json`。先写 `design.partial.bit`，Vivado 成功后才改为最终文件名；遗留临时文件不算成功产物。已存在的产物目录不复用、不覆盖。

最终 `result.json` 新增 `bitstream`：包含 status，成功时包含绝对 path、size_bytes、sha256；`paths.bitstream` 也指向成功产物。成功要求当前调用的完成标志、准确产物路径和非空文件均有效。

状态包括 not_requested、not_started、skipped、running、passed、failed、cancelled。执行失败或取消不会发布成功产物路径。设计验收与文件生成状态分别记录：设计验收通过后，bit 写出仍可能失败。

退出码继续使用：0 成功；1 执行失败；2 验收失败（不生成 bit）；3 验收未知（不生成 bit）；130 取消。

## 监控和完成事件

监控页增加“生成 bit”阶段，结束后展示文件位置、大小及哈希。请求 bit 但未生成时，该阶段不会显示完成。

后台任务只在包括 bit 在内的全部操作结束、结果落盘、进程清理和工程锁释放后发送一次完成事件。事件含 bitstream 结果，保留原会话唤醒及 event_id 去重规则。提交后模型结束本轮，无需状态轮询。

## 独立真实工具回归

```powershell
.\fpga-auto\runtime\python.exe -B -X utf8 .\fpga-auto\tests\prepare_bit_regression.py --vivado 'D:/vivado_2020_2/Vivado/2020.2' --output '测试目录/archive/bit-regression'
```

该脚本仿真测试计数器，通过 IO 规划设计查询器件管脚，并创建两个全新且具有完整 IO/时序约束的小工程：route-only 和 post-route。它只做准备，不替代正式 CLI 构建。使用输出的各 config 分别运行 `start build --bit`，然后对已有结果运行 `start bit`；核对文件、哈希、终态和完成事件。

默认器件 xcku040-ffva1156-2-i，可用 --part 指定已安装器件。管脚仅用于工具回归，不代表实际电路板，测试产物不用于硬件烧录。

模拟自动唤醒回归可使用 `tests/verify_desktop_wakeup.py --launch --rollout <当前会话记录> --bit`；它测试完整的模拟 build → 验收 → bit → 完成事件链路，模拟文件不是真实 FPGA 配置文件。
