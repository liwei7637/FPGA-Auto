"""Standalone Vivado Debug MCP stdio entry point."""
from vivado_debug.bootstrap import enable_vendor
enable_vendor()
from vivado_debug.mcp_server import main

if __name__ == "__main__":
    main()
