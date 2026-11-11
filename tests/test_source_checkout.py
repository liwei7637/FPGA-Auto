"""Exercise a clean source checkout without runtime or installed site packages."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fpga_auto import __version__
from fpga_auto.vivado import command_line
from fpga_auto.portable import ensure_config
from fpga_auto.results import FPGAError
from test_discovery import write_xpr


@unittest.skipUnless(os.name == "nt", "Windows command launcher")
class SourceCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="FPGA source 中文 ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tool = self.root / "shared tool"
        self.project = self.root / "工程 with spaces"
        self.project.mkdir()
        source = Path(__file__).resolve().parents[1]
        self.tool.mkdir()
        for name in ("portable.py", "fpga.cmd"):
            shutil.copyfile(source / name, self.tool / name)
        shutil.copytree(source / "fpga_auto", self.tool / "fpga_auto",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (self.tool / "tools").mkdir()
        shutil.copyfile(source / "tools/python.cmd", self.tool / "tools/python.cmd")

    def run_cli(self, *args):
        return subprocess.run(command_line(self.tool / "fpga.cmd", args),
                              cwd=str(self.root), capture_output=True, text=True,
                              encoding="utf-8", timeout=30)

    def test_version_from_clean_checkout(self):
        result = self.run_cli("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "FPGA Auto " + __version__)

    def test_shared_tool_respects_explicit_project_and_stays_read_only(self):
        xpr = write_xpr(self.project, "fpga/design.xpr")
        result = self.run_cli("discover", "--root", str(self.project), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        inventory = json.loads(result.stdout)
        self.assertEqual(Path(inventory["root"]), self.project)
        self.assertEqual([Path(c["project"]) for c in inventory["candidates"]], [xpr])
        self.assertEqual(list(self.project.iterdir()), [self.project / "fpga"])
        self.assertFalse((self.tool / "fpga.json").exists())

    def test_invalid_arguments_keep_nonzero_exit_status(self):
        result = self.run_cli("quick", "--bit")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse((self.tool / "fpga.json").exists())

    def test_initialization_works_without_hardlinks_and_preserves_config(self):
        xpr = write_xpr(self.project, "fpga/design.xpr")
        path = self.project / "fpga.json"
        with patch("fpga_auto.portable.os.link", side_effect=PermissionError("no hardlinks")):
            config, created = ensure_config(self.project, path, str(xpr), jobs=2)
        self.assertTrue(created)
        self.assertEqual(config["project_path"], xpr)
        original = path.read_bytes()
        with self.assertRaises(FPGAError):
            ensure_config(self.project, path, str(xpr), jobs=3)
        self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
