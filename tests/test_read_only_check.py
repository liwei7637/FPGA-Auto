"""Exercise the actual Tcl preflight with a mutating IP-report model."""
from pathlib import Path
import tempfile
import unittest
try:
    import tkinter
except ImportError:
    tkinter = None


@unittest.skipIf(tkinter is None, 'Tcl interpreter is unavailable')
class ReadOnlyCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fpga-readonly-check-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'reports').mkdir()
        self.source = self.root / 'source.v'
        self.source.write_text('module source; endmodule\n')
        self.interp = tkinter.Tcl()
        for name, value in dict(FPGA_PROJECT='fixture.xpr', FPGA_SYNTH='synth_1',
                                FPGA_IMPL='impl_1', FPGA_JOBS='1',
                                FPGA_JOB_DIR=str(self.root), FPGA_OPERATION='check').items():
            self.interp.setvar('env(' + name + ')', value)
        self.interp.setvar('source_file', str(self.source))
        self.interp.eval('''
            set ip_report_calls 0
            proc version {args} {return "2020.2"}
            proc open_project {args} {set ::readonly [expr {"-read_only" in $args}]}
            proc get_runs {args} {return {synth_1 impl_1}}
            proc get_filesets {args} {return {sources_1}}
            proc get_files {args} {return [list $::source_file]}
            proc list_property {args} {return {NAME}}
            proc get_property {property object} {
                switch $property {
                    NAME {return $object}
                    DIRECTORY {return "."}
                    IS_SYNTHESIS {return [expr {$object eq "synth_1"}]}
                    IS_IMPLEMENTATION {return [expr {$object eq "impl_1"}]}
                    PARENT {return "synth_1"}
                    FILESET_TYPE {return "DesignSrcs"}
                    default {error "Unexpected property $property"}
                }
            }
            proc report_ip_status {args} {
                incr ::ip_report_calls
                if {$::readonly} {error "Project 1-228: read-only BD initialization"}
            }
        ''')
        self.interp.call('source', str(Path(__file__).resolve().parents[1] / 'tcl/common.tcl'))

    def test_read_only_preflight_avoids_mutating_report(self):
        self.interp.call('fpga::check', 1)
        self.assertEqual(int(self.interp.getvar('ip_report_calls')), 0)
        self.assertTrue((self.root / 'reports/ip_status_not_queried.txt').is_file())

    def test_writable_preflight_still_reports_ip(self):
        self.interp.call('fpga::check', 0)
        self.assertEqual(int(self.interp.getvar('ip_report_calls')), 1)

    def test_read_only_preflight_still_rejects_missing_source(self):
        self.interp.setvar('source_file', str(self.root / 'missing.v'))
        with self.assertRaises(tkinter.TclError):
            self.interp.call('fpga::check', 1)
        self.assertEqual(int(self.interp.getvar('ip_report_calls')), 0)


if __name__ == '__main__':
    unittest.main()
