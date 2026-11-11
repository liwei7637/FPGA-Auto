# Real Vivado 2020.2 integration regression. All files stay under a newly
# created scratch directory from verify_vivado_steps.py; never open a user XPR.
set root $::env(FPGA_STEP_TEST_ROOT)
source [file join [file dirname [info script]] .. tcl common.tcl]
proc require {condition message} {
    if {![uplevel 1 [list expr $condition]]} { error $message }
}
set failed [catch {
    require {[version -short] eq "2020.2"} "This regression requires Vivado 2020.2"
    create_project step_regression [file join $root project] -part $::env(FPGA_STEP_TEST_PART)
    add_files [file join $root top.v]
    add_files -fileset constrs_1 [file join $root top.xdc]
    set_property top top [current_fileset]
    set_property STEPS.POST_ROUTE_PHYS_OPT_DESIGN.IS_ENABLED true [get_runs impl_1]
    update_compile_order -fileset sources_1

    # Retain a negative control: the XPR's internal ID is NOT a launch target.
    set invalid [catch {launch_runs impl_1 -scripts_only -to_step post_route_phys_opt_design} message]
    require {$invalid && [string first "12-1015" $message] >= 0} "Expected old step name to reproduce Vivado 12-1015"
    fpga::emit invalid_step_reproduced true
    close_project

    # Exercise the shipping procedures with real Vivado, including launch_runs,
    # completion recognition, and opening the result for Timing / DRC reports.
    fpga::check 0
    fpga::synth
    fpga::impl
    set post_status [get_property STATUS [get_runs impl_1]]
    fpga::emit post_route_status $post_status
    require {[fpga::completed_status $post_status {phys_opt_design (Post-Route)}]} "Post-route status was not recognized"
    fpga::timing
    fpga::drc
    fpga::emit post_route_reports_read true

    set_property STEPS.POST_ROUTE_PHYS_OPT_DESIGN.IS_ENABLED false [get_runs impl_1]
    fpga::impl
    set route_status [get_property STATUS [get_runs impl_1]]
    fpga::emit route_only_status $route_status
    require {[fpga::completed_status $route_status route_design]} "Route-only status was not recognized"
    fpga::timing
    fpga::drc
    fpga::emit route_only_reports_read true

    # A routed design with timing violations is still execution-complete;
    # pre-route optimization and incomplete/failed states must not pass.
    foreach step [list route_design {phys_opt_design (Post-Route)} write_bitstream] {
        require {[fpga::completed_status "$step Complete!" $step]} "Completion rejected: $step"
        require {[fpga::completed_status "$step Complete, Failed Timing!" $step]} "Timing failure confused with execution failure: $step"
        foreach suffix [list Running Failed Cancelled {Not started}] {
            require {![fpga::completed_status "$step $suffix" $step]} "Incomplete status accepted: $step $suffix"
        }
    }
    require {![fpga::completed_status {phys_opt_design Complete!} {phys_opt_design (Post-Route)}]} "Pre-route completion incorrectly accepted as post-route"
    require {![fpga::completed_status {post_route_phys_opt_design Complete!} {phys_opt_design (Post-Route)}]} "Internal Step Id incorrectly accepted as STATUS"
    close_project
    fpga::emit regression_passed true status_assertions_passed true done true
} message options]
if {$failed} {
    catch {fpga::emit regression_passed false regression_error $message done false}
    puts [dict get $options -errorinfo]
    catch {close_project}
    exit 1
}
puts "FPGA_STEP_REGRESSION_PASSED: post-route and route-only implementations, reports, and status assertions"
exit 0
