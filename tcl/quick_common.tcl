# MIG-derived pre-route screen. Native placement-only run preserves project hooks.
namespace eval fpga {}
proc fpga::quick_reports {directory prefix paths} {
    report_timing_summary -delay_type max -report_unconstrained -file [file join $directory [format "%s_timing.rpt" $prefix]]
    report_timing -delay_type max -max_paths $paths -path_type full_clock_expanded -file [file join $directory [format "%s_paths.rpt" $prefix]]
    if {$prefix eq "post_synth"} {
        report_high_fanout_nets -load_types -max_nets 100 -file [file join $directory [format "%s_fanout.rpt" $prefix]]
    } else {
        report_high_fanout_nets -clock_regions -max_nets 100 -file [file join $directory [format "%s_fanout.rpt" $prefix]]
    }
}
proc fpga::quick_native {} {
    variable synth
    variable impl
    variable out
    variable jobs
    set template [run_named $impl]
    set properties {}
    foreach property [list_property $template] {
        if {[regexp {^STEPS\.[^.]+\.(ARGS\.|TCL\.|IS_ENABLED$)} $property]} {
            lappend properties $property [get_property $property $template]
        }
    }
    set flow [get_property FLOW $template]
    set strategy [get_property STRATEGY $template]
    set constraints [get_property CONSTRSET $template]
    set part [get_property PART $template]
    set name "fpga_auto_quick_[file tail $out]"
    if {[llength [get_runs -quiet $name]]} { fail quick_run_exists "Refusing to reuse quick run: $name" }
    set directory [file join $out reports quick_check]
    if {[file exists $directory]} { fail quick_output_exists "Quick evidence directory must be new: $directory" }
    synth
    emit stage quick_physical_check
    file mkdir $directory
    open_run $synth
    quick_reports $directory post_synth 30
    close_design
    create_run $name -parent_run $synth -flow $flow -strategy $strategy -constrset $constraints -part $part
    set run [run_named $name]
    if {[llength $properties]} { set_property -dict $properties $run }
    set_property STEPS.PLACE_DESIGN.ARGS.DIRECTIVE RuntimeOptimized $run
    emit stage quick_placement quick_physical_complete true quick_run $name quick_directory [get_property DIRECTORY $run] quick_status running
    launch_runs $run -to_step place_design -jobs $jobs
    wait_on_run $run
    set log [file join [get_property DIRECTORY $run] runme.log]
    if {[file isfile $log]} { file copy $log [file join $out reports quick_run.log] }
    set status [get_property STATUS $run]
    emit quick_run_status $status
    if {$status ne "place_design Complete!" || [get_property NEEDS_REFRESH $run]} {
        emit quick_status failed
        fail quick_placement_failed "Placement-only run did not complete: $status"
    }
    open_run $name
    emit stage quick_reports quick_placement_complete true quick_status passed
    quick_reports $directory post_place 50
    report_design_analysis -congestion -file [file join $directory post_place_congestion.rpt]
    check_timing -verbose -file [file join $directory post_place_check_timing.rpt]
    report_cdc -details -file [file join $directory post_place_cdc.rpt]
    write_checkpoint [file join $directory post_place.dcp]
    close_design
    emit quick_complete true quick_scope pre_route_screening stage post_place_screening
}
proc fpga::quick {} {
    set original [current_run -implementation]
    set code [catch {quick_native} message options]
    set restore_code [catch {current_run -implementation $original} restore_message]
    if {$code} { return -options $options $message }
    if {$restore_code} { fail quick_restore_failed $restore_message }
}
