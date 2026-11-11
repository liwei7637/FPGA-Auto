"""Private persistent session worker; not an MCP stdio process."""
import sys
from vivado_debug.sessions import worker

if __name__ == "__main__":
    raise SystemExit(worker(sys.argv[1]))
