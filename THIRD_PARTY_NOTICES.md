# 第三方依赖

构建 CLI 和 quick 只依赖目标电脑提供的 Python 标准库及已安装的 Vivado 2020.2。此源码仓库不分发 CPython、Vivado、器件 IP 或预编译网表。

可选 Vivado Debug MCP 的 Python 依赖与 SHA-256 固定在 `requirements-mcp.lock`，原包记录见 `MCP-DEPENDENCIES.json`。依赖由用户在本机虚拟环境安装，各组件遵循其上游许可证。仓库不包含原包 vendor 二进制或本机 runtime。

如继续使用已有便携包中的 CPython runtime，应同时保留该包的 `runtime/LICENSE.txt` 与依赖许可证文件。
