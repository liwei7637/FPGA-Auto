"""Placement-only quick flow against Tcl API stubs; never runs Vivado."""
from pathlib import Path
import tempfile
import unittest
try:
    import tkinter
except ImportError:
    tkinter = None

STUB = r'''
namespace eval fpga {variable synth synth_custom;variable impl impl_custom;variable jobs 3;variable out}
set calls {}
set events {}
set selected impl_user_selected
set copied {}
set fresh 0
proc fpga::run_named {name} {return $name}
proc fpga::fail {code message} {return -code error -errorcode [list FPGA_AUTO $code] $message}
proc fpga::emit {args} {lappend ::events $args}
proc fpga::synth {} {lappend ::calls synthesis; if {$::scenario eq "synthesis_failure"} {error "synthesis failed"};set ::fresh 1}
proc list_property {run} {return {STEPS.INIT_DESIGN.TCL.POST STEPS.OPT_DESIGN.TCL.PRE STEPS.OPT_DESIGN.TCL.POST STEPS.PLACE_DESIGN.TCL.PRE STEPS.PLACE_DESIGN.ARGS.DIRECTIVE STEPS.INIT_DESIGN.REPORTS NAME}}
proc get_property {key run} {
    switch -- $key {
      FLOW {return {Vivado Implementation 2020}}
      STRATEGY {return Default}
      CONSTRSET {return constraints_custom}
      PART {return xcku040-ffva1156-2-i}
      DIRECTORY {return $::run_directory}
      STATUS {if {$::scenario eq "placement_failure"} {return "place_design ERROR!"};return "place_design Complete!"}
      NEEDS_REFRESH {return [expr {$::scenario eq "stale"}]}
      default {return "value_for_$key"}
    }
}
proc get_runs {args} {if {$::scenario eq "collision"} {return existing};return {}}
proc open_run {name} {if {!$::fresh} {error "stale synthesis opened"};lappend ::calls [list open_run $name]}
proc close_design {} {lappend ::calls close_design}
proc current_run {args} {
    if {[llength $args] == 1} {return $::selected}
    set ::selected [lindex $args 1]
    return $::selected
}
proc create_run {name args} {
    if {!$::fresh} {error "no fresh synthesis"}
    set ::created $name;set ::create_args $args
    set ::run_directory [file join $::fpga::out stub_native_runs $name]
    file mkdir $::run_directory
    set f [open [file join $::run_directory runme.log] w];puts $f {clean quick run log};close $f
    set ::selected $name
}
proc set_property {args} {
    if {[lindex $args 0] eq "-dict"} {set ::copied [lindex $args 1]} else {set ::directive [lindex $args 1]}
}
proc launch_runs {name args} {
    if {$name eq "impl_custom" || [lrange $args 0 1] ne {-to_step place_design}} {error "unsafe launch"}
    lappend ::calls [list launch $name $args]
}
proc wait_on_run {name} {lappend ::calls wait}
proc fixture_report {command args} {
    if {$::scenario eq "report_failure" && $command eq "report_cdc"} {error "CDC report failed"}
    set i [lsearch -exact $args -file]
    if {$i < 0} {set path [lindex $args end]} else {set path [lindex $args [expr {$i+1}]]}
    set f [open $path w];puts $f "fixture $command";close $f
    lappend ::calls $command
}
foreach c {report_timing_summary report_timing report_high_fanout_nets report_design_analysis check_timing report_cdc write_checkpoint} {
    proc $c {args} [format {fixture_report %s {*}$args} $c]
}
'''

@unittest.skipIf(tkinter is None, "Tcl runtime from tkinter is not installed")
class QuickTclTests(unittest.TestCase):
    def run_case(self, scenario):
        with tempfile.TemporaryDirectory(prefix="quick Tcl 中文 ") as temp:
            out = Path(temp)
            interp = tkinter.Tcl()
            interp.eval(STUB)
            interp.call("set", "scenario", scenario)
            interp.call("set", "::fpga::out", str(out))
            if scenario == "existing_output":
                (out / "reports/quick_check").mkdir(parents=True)
            script = Path(__file__).resolve().parents[1] / "tcl/quick_common.tcl"
            interp.call("source", str(script))
            code = interp.eval("catch {fpga::quick} message options")
            self.assertEqual(code == "0", scenario == "success", interp.eval("set message"))
            self.assertEqual(interp.eval("set selected"), "impl_user_selected")
            self.assertNotIn("route_design", interp.eval("set calls"))
            self.assertNotIn("write_bitstream", interp.eval("set calls"))
            if scenario == "success":
                self.assertEqual(interp.eval("dict get $copied STEPS.OPT_DESIGN.TCL.PRE"), "value_for_STEPS.OPT_DESIGN.TCL.PRE")
                self.assertEqual(interp.eval("dict get $copied STEPS.INIT_DESIGN.TCL.POST"), "value_for_STEPS.INIT_DESIGN.TCL.POST")
                self.assertEqual(interp.eval("set directive"), "RuntimeOptimized")
                self.assertEqual(interp.eval("dict exists $copied STEPS.INIT_DESIGN.REPORTS"), "0")
                self.assertEqual(interp.eval("dict get $create_args -constrset"), "constraints_custom")
                self.assertEqual(interp.eval("dict get $create_args -parent_run"), "synth_custom")
                self.assertEqual(len(list((out / "reports/quick_check").iterdir())), 10)
                self.assertIn("quick_complete true", interp.eval("set events"))
            else:
                self.assertNotIn("quick_complete true", interp.eval("set events"))

    def test_success_preserves_template_and_restores_selection(self):
        self.run_case("success")

    def test_synthesis_failure(self):
        self.run_case("synthesis_failure")

    def test_placement_failure(self):
        self.run_case("placement_failure")

    def test_stale_run(self):
        self.run_case("stale")

    def test_report_failure(self):
        self.run_case("report_failure")

    def test_run_collision(self):
        self.run_case("collision")

    def test_existing_output_rejected(self):
        self.run_case("existing_output")

if __name__ == "__main__":
    unittest.main()
