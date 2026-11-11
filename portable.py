"""Entry point for the unpack-and-use Windows package."""
from pathlib import Path
from fpga_auto.portable import main

if __name__ == "__main__":
    raise SystemExit(main(default_root=Path(__file__).resolve().parent.parent))
