"""Internal detached worker entry; use fpga.cmd start to create its request."""
import sys
from fpga_auto.background import worker

if __name__ == "__main__":
    raise SystemExit(worker(sys.argv[1]))
