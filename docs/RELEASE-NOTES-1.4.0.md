> 原便携包文档保留供参考。当前源码仓库的安装、目录和验证范围以 [README](../README.md) 与 [1.4.3 验证记录](VALIDATION-1.4.3.md) 为准；不包含原包 runtime/vendor 或历史硬件证据。

# FPGA Auto 1.4.0 / Vivado Debug MCP

日期：2026-11-06。Windows x64，Vivado 2020.2，Python 3.14.7。

- 新增独立 `vivado-debug-mcp.cmd` STDIO 服务和 `vivado-debug.cmd` 调试 CLI。
- 提供通用 ILA/VIO 会话、精确物理探针、设备绑定、后台采集、取消和证据归档。
- 双 FPGA 对称 10G UDP 为首个插件，支持固定/持续、单向/反向/双向和统计分析。
- 新增历史数据回放及 CSV→PCAP；缺页、失效快照和中断不判 PASS。
- MCP SDK 及依赖锁定并随包提供，使用时无需 pip 或联网。
- 保留原构建/bit/通知实现，以及工作区相对参考 1.3.0 ZIP 已有的 desktop.py、common.tcl 和 test_desktop.py 修复。

验收范围为离线软件测试、实际 MCP STDIO 通信、真实 Vivado 2020.2 命令加载及替身 API 检查。
新 MCP 的真实 ILA/VIO 后端验收 PENDING。开发期间按追加单板测试请求另用既有 Tcl 通路盘点现场：
仅 A 的 JTAG 可见且未配置，一次同版易失配置恢复报 EOS LOW，六次回读仍 DONE=0；
未能执行新 MCP 的真实 ILA/VIO 测试，没有写 Flash。该现场诊断不代替本版后端验收。
既有 1.3.0 发布验收记录仍为其历史范围，不用作本版硬件证据。

完整用法见 [Vivado Debug MCP](VIVADO-DEBUG-MCP.md)。
