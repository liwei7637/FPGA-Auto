# 通用预编译 / quick

quick 来自 MIG 的快速物理预审查：先重新综合当前输入，再检查综合后与布局后风险。不执行布线，不生成 BIT/LTX，不替代完整 Timing/DRC 或板测验收。重新综合会使依赖旧综合的实现过期，先确认目标和授权。

推荐依照本项目原有通知流程使用：
    FPGA-Auto.cmd start quick --root <工程根> --config <本项目配置> --notify-thread <本次任务ID> --json

没有根启动器时使用 fpga-auto/fpga.cmd。首次先 discover 定位目标。notify-check 失败时停止提交；不自动改为轮询。只有明确选择前台模式时使用 quick --no-monitor。quick 不接受 --bit。

## 工程隔离

- 不复制 MIG 的 packet_debug_clock、MIG UI 时钟名称、XPR、配置、状态、锁、IP、RTL 或约束到其他工程。
- MIG 保留原项目专属 quick Tcl 路线和 UI-domain 附加报告。
- 其他项目每次使用当前配置的综合 run，并新建 fpga_auto_quick_<job_id> 布局专用 run。继承选定实现 run 的器件、flow、strategy、constraint set、步骤 ARGS 参数、IS_ENABLED 开关及 Tcl hooks（原生 REPORTS 引用不复制，quick 使用自己的必需报告），只有 placement directive 改为 RuntimeOptimized。只启动到 place_design，原实现 run 不会被该 run 冒充。默认实现选择在成功/失败后恢复。
- 专用 run 的原生工作目录保留在工程 .runs 中，报告、检查点及运行日志归档到此次作业以便诊断，不覆盖或清理旧 run；它会作为额外 run 登记在同一个 XPR 中，不新增工程副本。项目 hook 仍按项目本身语义执行，其失败不会被忽略。
- 通用工具仅支持已有的 Vivado 2020.2 合约，不隐式生成/升级 IP。

## 结果

验收范围为 quick_pre_route。10 项必需非空报告/检查点位于本次 reports/quick_check；主日志和综合日志用于识别约束加载错误。passed 只表示证据完整、布局后 setup 筛查通过且没有指定的约束加载失败。hold、布线后时序、完整 DRC 和板测仍未验收，CDC/拥塞报告仍需分析。

本次同步运行离线单元测试、Tcl API 桩验证，并用 Vivado 2020.2 全新空白工程验证 run 参数复制 API；没有运行真实综合、布局、实现或硬件。真实工程上的 quick 仍需另行执行并查看本次结果。
