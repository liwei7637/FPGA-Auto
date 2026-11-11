# Prepare fully constrained device-only test projects. Never use board hardware.
set root $::env(FPGA_BIT_TEST_ROOT)
set part $::env(FPGA_BIT_TEST_PART)
# Query the device through an I/O planning design; RTL elaboration is unnecessary.
# Single-ended clocks must use the P member of the clock-capable pair.
create_project -in_memory pin_probe -part $part
set_property design_mode PinPlanning [current_fileset]
open_io_design -name io_1
set clocks [get_package_pins -filter {IS_GENERAL_PURPOSE == 1 && PIN_FUNC =~ IO_L*P_*GC*}]
if {![llength $clocks]} { error "No clock-capable GPIO found for $part" }
set clock_pin [lindex $clocks 0]
set bank [get_property BANK $clock_pin]
set others {}
foreach pin [get_package_pins -filter "IS_GENERAL_PURPOSE == 1 && BANK == $bank"] {
    if {$pin ne $clock_pin} { lappend others $pin }
}
if {[llength $others] < 2} { error "Insufficient GPIO in bank $bank" }
close_design
close_project

foreach mode {route-only post-route} {
    set directory [file join $root $mode]
    create_project bit_regression [file join $directory project] -part $part
    add_files [list [file join $directory bit_regression_top.v] [file join $directory bit_regression_counter.v]]
    set_property top bit_regression_top [current_fileset]
    # These device pins are test constraints, not a physical board pinout.
    set xdc [file join $directory bit_regression.xdc]
    set output [open $xdc w]
    foreach port {clk rst led} pin [list $clock_pin [lindex $others 0] [lindex $others 1]] {
        puts $output [format {set_property PACKAGE_PIN %s [get_ports {%s}]} $pin $port]
    }
    puts $output {set_property IOSTANDARD LVCMOS18 [get_ports *]}
    puts $output {create_clock -period 20.000 [get_ports clk]}
    puts $output {set_input_delay -clock [get_clocks clk] -min 0.000 [get_ports rst]}
    puts $output {set_input_delay -clock [get_clocks clk] -max 2.000 [get_ports rst]}
    puts $output {set_output_delay -clock [get_clocks clk] -min 0.000 [get_ports led]}
    puts $output {set_output_delay -clock [get_clocks clk] -max 2.000 [get_ports led]}
    puts $output {set_property CFGBVS GND [current_design]}
    puts $output {set_property CONFIG_VOLTAGE 1.8 [current_design]}
    close $output
    add_files -fileset constrs_1 $xdc
    set_property STEPS.POST_ROUTE_PHYS_OPT_DESIGN.IS_ENABLED [expr {$mode eq "post-route"}] [get_runs impl_1]
    update_compile_order -fileset sources_1
    close_project
}
puts "BIT_REGRESSION_PROJECTS_PREPARED"
exit 0
