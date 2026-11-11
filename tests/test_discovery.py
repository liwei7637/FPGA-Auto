import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fpga_auto.discovery import discover, inventory, select_project
from fpga_auto.portable import ensure_config, vivado_candidates, make_parser
from fpga_auto.results import FPGAError


XPR = '''<Project><Configuration><Option Name="Part" Val="xc7a35t"/></Configuration>
<FileSets><FileSet Name="sources_1" Type="DesignSrcs"/></FileSets><Runs>
<Run Id="synth_1" Type="Ft3:Synth" SrcSet="sources_1"/>
<Run Id="impl_1" Type="Ft2:EntireDesign" SynthRun="synth_1" State="current"/>
<Run Id="ip_synth" Type="Ft3:Synth" SrcSet="ip_sources"/>
</Runs></Project>'''


def write_xpr(root, relative):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(XPR, encoding="utf-8")
    return path.resolve()


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fpga discovery 中文 ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "fpga.json"

    def manifest(self, entries, default=None):
        data = {"projects": {alias: {"project": value} for alias, value in entries.items()}}
        if default is not None:
            data["default_project"] = default
        path = self.root / "config/native-projects.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_history_excluded_and_discovery_is_read_only(self):
        active = write_xpr(self.root, "fpga/design/design.xpr")
        for directory in ("build", "archive", "backup", "project.runs", "fpga-auto"):
            write_xpr(self.root, directory + "/old.xpr")
        report = discover(self.root, self.config)
        self.assertEqual([item["project"] for item in report["candidates"]], [str(active)])
        self.assertEqual(report["selection"]["reason"], "unique_candidate")
        self.assertFalse(self.config.exists())
        self.assertFalse((self.root / ".fpga").exists())
        self.assertEqual(len(report["candidates"][0]["runs"]), 2)

    def test_manifest_alias_and_name_resolve_without_vivado(self):
        target = write_xpr(self.root, "fpga/UDP_I17R_OPT/UDP_I17R_OPT.xpr")
        write_xpr(self.root, "fpga/UDP/UDP.xpr")
        self.manifest({"i17r-opt": "fpga/UDP_I17R_OPT/UDP_I17R_OPT.xpr"})
        for selector in ("i17r-opt", "udp_i17r_opt", "UDP_I17R_OPT.xpr"):
            self.assertEqual(select_project(self.root, self.config, selector)[0], target)

    def test_manifest_default_is_used(self):
        target = write_xpr(self.root, "fpga/a.xpr")
        write_xpr(self.root, "fpga/b.xpr")
        self.manifest({"primary": "fpga/a.xpr"}, "primary")
        self.assertEqual(select_project(self.root, self.config), (target, "manifest_default"))

    def test_saved_config_precedes_default(self):
        target = write_xpr(self.root, "fpga/a.xpr")
        ensure_config(self.root, self.config, str(target))
        write_xpr(self.root, "fpga/b.xpr")
        self.manifest({"new": "fpga/b.xpr"}, "new")
        self.assertEqual(select_project(self.root, self.config), (target, "saved_config"))

    def test_config_conflict_never_overwrites(self):
        old = write_xpr(self.root, "fpga/a.xpr")
        target = write_xpr(self.root, "fpga/b.xpr")
        ensure_config(self.root, self.config, str(old))
        before = self.config.read_bytes()
        with self.assertRaises(FPGAError) as raised:
            ensure_config(self.root, self.config, str(target))
        self.assertEqual(raised.exception.reason, "config_exists")
        self.assertEqual(self.config.read_bytes(), before)
        report = discover(self.root, self.config, "b")
        self.assertEqual(report["warnings"][0]["reason"], "config_conflict")

    def test_duplicate_names_require_path(self):
        for directory in ("a", "b"):
            write_xpr(self.root, directory + "/design.xpr")
        for selector in (None, "design"):
            with self.assertRaises(FPGAError) as raised:
                select_project(self.root, self.config, selector)
            self.assertEqual(raised.exception.reason, "project_selection_required")

    def test_misspelled_explicit_path_never_falls_back(self):
        write_xpr(self.root, "fpga/design.xpr")
        with self.assertRaises(FPGAError) as raised:
            select_project(self.root, self.config, "missing/design.xpr")
        self.assertEqual(raised.exception.reason, "project_missing")

    def test_missing_manifest_default_never_falls_back(self):
        write_xpr(self.root, "fpga/other.xpr")
        self.manifest({"primary": "fpga/missing.xpr"}, "primary")
        with self.assertRaises(FPGAError) as raised:
            select_project(self.root, self.config)
        self.assertEqual(raised.exception.reason, "project_missing")

    def test_broken_saved_config_never_falls_back(self):
        write_xpr(self.root, "fpga/other.xpr")
        self.config.write_text("{}", encoding="utf-8")
        with self.assertRaises(FPGAError) as raised:
            select_project(self.root, self.config)
        self.assertEqual(raised.exception.reason, "config_invalid")

    def test_explicit_history_allowed_but_never_auto_selected(self):
        target = write_xpr(self.root, "archive/design.xpr")
        report = discover(self.root, self.config, include_history=True)
        self.assertEqual(len(report["candidates"]), 1)
        self.assertEqual(report["selection"]["status"], "unresolved")
        self.assertEqual(select_project(self.root, self.config, str(target))[0], target)

    def test_invalid_manifest_can_be_bypassed_by_exact_path(self):
        target = write_xpr(self.root, "fpga/design.xpr")
        self.manifest({"a": "fpga/design.xpr"})
        (self.root / "config/native-projects.json").write_text("broken", encoding="utf-8")
        self.assertEqual(select_project(self.root, self.config, str(target))[0], target)
        with self.assertRaises(FPGAError):
            inventory(self.root)

    def test_invalid_xpr_is_not_persisted(self):
        target = write_xpr(self.root, "fpga/design.xpr")
        target.write_text("broken", encoding="utf-8")
        with self.assertRaises(FPGAError) as raised:
            ensure_config(self.root, self.config)
        self.assertEqual(raised.exception.reason, "project_xml_invalid")
        self.assertFalse(self.config.exists())

    def test_custom_vivado_directory_is_a_candidate(self):
        expected = "E:/vivado2020.2/Vivado/2020.2"
        with patch("fpga_auto.portable._executable", side_effect=lambda value: Path(value) if value == expected else None):
            self.assertIn(Path(expected), vivado_candidates())

    def test_operations_accept_explicit_target(self):
        args = make_parser().parse_args(["synth", "--project", "i17r-opt", "--json"])
        self.assertEqual(args.project, "i17r-opt")


if __name__ == "__main__":
    unittest.main()
