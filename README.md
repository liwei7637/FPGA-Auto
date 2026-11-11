# FPGA Auto

**1.4.3：集成通用预编译 `quick`，通过 Git 在多台电脑共用同一版工具。**

基于本机 1.4.2 与通用 quick 扩展整理，保留工程发现、综合、实现、Timing/DRC、BIT、后台完成通知、进度页面和独立 ILA/VIO 调试工具。当前构建执行支持 **Windows 10/11 x64 + Vivado 2020.2**。

## 每台电脑安装

安装 Git、Python 3.8+、Vivado 2020.2 及对应器件支持和许可证。构建 CLI 只用 Python 标准库，无需 pip。源码仓库不包含便携 Python 二进制和 MCP 第三方依赖。

```powershell
git clone https://github.com/liwei7637/FPGA-Auto.git D:/tools/FPGA-Auto
cd D:/tools/FPGA-Auto
.\fpga.cmd --version
.\fpga.cmd --help
```

启动器依次使用本工具目录中的 `.venv/Scripts/python.exe`、`runtime/python.exe`、PATH 上的 `python`、`py -3`。也可直接运行 `python -B -X utf8 portable.py`。无需将工具复制到每个 FPGA 工程。

## 接入各自的工程

共享工具目录时，**每条工程命令都传 `--root`**，指定本机 FPGA 工程根目录。

```powershell
# 只读发现，不编译、不修改工程。
.\fpga.cmd discover --root 'D:/work/MyProject' --json

# 明确 XPR、run 与本机 Vivado，配置保存相对工程路径。
.\fpga.cmd init --root 'D:/work/MyProject' --project 'fpga/design/design.xpr' --synth-run synth_1 --impl-run impl_1 --vivado 'D:/Xilinx/Vivado/2020.2' --jobs 8 --json

# 前台预编译，等待本次命令完成。
.\fpga.cmd quick --root 'D:/work/MyProject' --no-monitor --json

# 完整构建与验收通过后生成 BIT。
.\fpga.cmd build --root 'D:/work/MyProject' --bit --no-monitor --json
```

已有 `fpga.json` 会保留；其他配置用 `--config` 指定，其相对路径基于 `--root`。配置示例见 [examples/fpga.example.json](examples/fpga.example.json)。Vivado 安装位置只进入本机 `.fpga/portable/toolchain.json` 缓存，也可用 `FPGA_VIVADO` 指定。

| 命令 | 作用与验收范围 |
|---|---|
| `discover` / `init` | 发现、明确目标和初始化；不综合 |
| `check` | 检查工程与输入；不代表时序通过 |
| `synth` | 重新综合 |
| `quick` | 重新综合 + 独立布局 run + 布线前 setup 筛查 |
| `impl` | 使用有效综合运行完整实现 |
| `build` | 检查 → 综合 → 完整实现 → Timing → DRC |
| `build --bit` / `bit` | 通过完整 Timing/DRC 后生成 BIT |
| `start <操作>` | 后台 worker，返回 Job ID；可使用完成通知 |
| `status` / `logs` / `monitor` / `cancel` | 指定任务的结果、日志、进度和取消 |

## 预编译的含义

这里的“预编译”是**快速物理预审查**，不是预先编译好的可执行程序或 IP 缓存。`quick` 重新综合当前输入，新建独立布局 run，继承选定实现 run 的参数和 Tcl hooks，仅运行到 `place_design`，并恢复原实现 run 的默认选择。不布线、不生成 BIT，不隐式生成或升级 IP。

输出包括综合后/布局后时序与高扇出、拥塞、CDC、check_timing 和布局检查点。`acceptance_scope=quick_pre_route` 的 passed 只表示此次布线前 setup 筛查通过。完整布线后时序、DRC 与板测分别验收。重新综合会使依赖旧综合的实现过期。详见 [docs/QUICK.md](docs/QUICK.md)。

后台使用先检查通知通道，再提交明确目标：

```powershell
.\fpga.cmd notify-check --root 'D:/work/MyProject' --notify-thread $env:CODEX_THREAD_ID --json
.\fpga.cmd start quick --root 'D:/work/MyProject' --notify-thread $env:CODEX_THREAD_ID --json
```

通知检查失败时不自动改为定时轮询。前台模式可直接等待命令反馈。通知与构建结果相互独立。详见 [docs/CODEX-WORKFLOW.md](docs/CODEX-WORKFLOW.md)。

## 多电脑更新

```powershell
cd D:/tools/FPGA-Auto
git pull --ff-only
.\fpga.cmd --version
git rev-parse HEAD
```

先等待本机正在使用该工具的任务结束，再更新。各电脑核对相同 commit 即为同一版工具；需要固定版本时使用 `git checkout <commit>`。FPGA 工程源码通过各自工程仓库同步。

共享通用代码、Tcl、测试、示例和说明。工程的 `.fpga`、锁、任务状态、Vivado 缓存、产物和本机工具链缓存留在各自电脑。每台电脑使用自己的工程副本、配置和输出目录。同一 XPR 的竞争操作返回 `project_busy`，不自动排队。

## 可选 ILA/VIO 调试与 MCP

保留原 1.4.2 调试源码，默认 mock，不自动连接硬件。普通构建与 `quick` 不依赖 MCP。

`vivado-debug.cmd` 使用 Python 3.10+。MCP 的历史锁定依赖面向 Windows x64 / CPython 3.14；在本工具目录创建独立虚拟环境后安装：

```powershell
# 使用本机 CPython 3.14，安装位置按实际替换。
& 'C:/Python314/python.exe' -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: -r requirements-mcp.lock
.\vivado-debug.cmd info
.\vivado-debug-mcp.cmd --root 'D:/work/MyProject' --list-tools
```

物理目标与探针配置由各工程维护，不同步已绑定的板卡 profile。详见 [docs/VIVADO-DEBUG-MCP.md](docs/VIVADO-DEBUG-MCP.md)。

## 验证与版本

```powershell
python -B -X utf8 -m unittest discover -s tests -v
# 调试源码测试需要 Python 3.10+；MCP 测试另需锁定 SDK。
# 可保存测试报告；在受限宿主上明确选择单元范围。
python -B -X utf8 tools/test_source.py core --restricted-host --output build/core-units-01
```

验证记录见 [docs/VALIDATION-1.4.3.md](docs/VALIDATION-1.4.3.md)，改动见 [CHANGELOG.md](CHANGELOG.md)，引入前文件哈希见 [SOURCE_BASELINE.json](SOURCE_BASELINE.json)。历史便携包文档不代表本源码仓库自带运行时或已完成异机板测。
