# 1.4.3 源码集成验证

日期：2026-11-11。范围为通用工具整理、源码启动与离线回归。没有执行真实 FPGA 工程的综合、布局、布线、BIT 生成或上板，没有验证多台电脑的实机执行。

| 验证 | Python | 选择用例 | 通过 | 跳过 | 未纳入此轮 |
|---|---|---:|---:|---:|---|
| 核心 CLI / quick / 配置 / BIT 验证 / 通知单元测试 | 3.8.10 | 100 | 100 | 0 | 8 项依赖本机进程或网络的集成用例 |
| 同一核心用例集 | 3.14.7 | 100 | 90 | 10 | 同上；便携运行时缺少 Tcl，跳过 10 项 Tcl 用例 |
| ILA/VIO 数据、模型与离线回放 | 3.14.7 | 33 | 33 | 0 | 独立调试 worker 与 MCP SDK/传输集成 |

核心 100 项已包含 7 项 quick Tcl 场景：成功、综合失败、布局失败、过期 run、报告失败、run 名冲突、输出已存在。验证继承 hooks/约束集、恢复默认实现 run、10 项证据输出及不运行 route/BIT。

干净源码目录启动测试未携带 runtime/vendor，使用系统 Python 3.8，覆盖版本号、中文和空格路径、显式 `--root`、只读工程发现、错误退出码，以及禁止硬链接时初始化配置仍不覆盖已有文件。

## 当前环境的限制

未筛选的核心回归曾运行 101 项：93 项通过，8 项因宿主禁止独立后台进程或本地套接字失败/报错（WinError 5 / 10013）；随后加入 7 项 Tcl 用例。保留原集成测试，没有删掉这些测试来宣称全套通过。

MCP SDK/STDIO 集成尝试未在此宿主完成，测试进程已停止；不声称 SDK 集成通过。可选调试代码保留原 1.4.2 功能，但本次只确认上述数据/模型回归。

当前发布**没有全套集成测试通过的结论**。真实工程上的 quick 及异机运行仍需在目标电脑完成，quick passed 也不等于完整 Timing/DRC 或板测通过。

## 复现

在普通 Windows 主机执行完整核心测试：

```powershell
python -B -X utf8 tools/test_source.py core --output build/core-full-01
```

在明确限制独立进程或本地网络的宿主，只运行并记录相同的单元范围：

```powershell
python -B -X utf8 tools/test_source.py core --restricted-host --output build/core-units-01
# 使用 Python 3.10+，本轮使用 3.14.7。
python -B -X utf8 tools/test_source.py debug --restricted-host --output build/debug-units-01
```

`--output` 必须为新目录；每次生成 tests.log/result.json，记录实际跳过项与排除范围。完整 debug 测试去掉 `--restricted-host`，另需安装锁定的 MCP SDK，并具备进程、STDIO 等运行条件。

机器可读结果见 [VALIDATION-1.4.3.json](VALIDATION-1.4.3.json)。本次未重新调用 Vivado 或硬件，不继承历史便携包的板测结论。
