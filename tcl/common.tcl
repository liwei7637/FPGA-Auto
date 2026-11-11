# Shared, version-controlled primitives. Arguments arrive only through environment.
namespace eval fpga {
    variable data [dict create stage preflight done false]
    if {[info exists ::env(FPGA_OPERATION)] && $::env(FPGA_OPERATION) eq "write_bit"} {
        set data [dict create stage bitstream done false checked true synthesis_status passed implementation_status passed timing_complete true drc_complete true bitstream_status running]
    }
    variable opened 0
    variable project $::env(FPGA_PROJECT)
    variable synth $::env(FPGA_SYNTH)
    variable impl $::env(FPGA_IMPL)
    variable jobs $::env(FPGA_JOBS)
    variable out $::env(FPGA_JOB_DIR)
}

proc fpga::json_quote {value} {
    set result "\""
    foreach char [split $value ""] {
        scan $char %c code
        if {$char eq "\"" || $char eq "\\"} {
            append result "\\" $char
        } elseif {$code < 32} {
            append result [format {\u%04x} $code]
        } else {
            append result $char
        }
    }
    append result "\""
    return $result
}

proc fpga::emit {args} {
    variable data
    variable out
    foreach {key value} $args { dict set data $key $value }
    set entries {}
    dict for {key value} $data { lappend entries "[json_quote $key]:[json_quote $value]" }
    set path [file join $out bridge.json]
    set f [open "${path}.tmp" w]
    fconfigure $f -encoding utf-8 -translation lf
    puts $f "\{[join $entries ,]\}"
    close $f
    for {set attempt 0} {$attempt < 20} {incr attempt} {
        if {![catch {file rename -force "${path}.tmp" $path} message]} { return }
        after 25
    }
    error "Cannot publish bridge: $message"
}

proc fpga::fail {reason message} {
    return -code error -errorcode [list FPGA_AUTO $reason] $message
}

proc fpga::run_named {name} {
    foreach run [get_runs -quiet] {
        if {[get_property NAME $run] eq $name} { return $run }
    }
    fail run_missing "Run not found: $name"
}

proc fpga::check {read_only} {
    variable project
    variable opened
    variable synth
    variable impl
    variable out
    if {[info exists ::env(FPGA_OPERATION)] && $::env(FPGA_OPERATION) eq "write_bit"} {
        emit stage bitstream
    } else { emit stage check }
    if {[version -short] ne "2020.2"} { fail vivado_version_mismatch "Expected Vivado 2020.2" }
    if {$read_only} { open_project -read_only $project } else { open_project $project }
    set opened 1
    set sr [run_named $synth]
    set ir [run_named $impl]
    emit synthesis_directory [get_property DIRECTORY $sr] implementation_directory [get_property DIRECTORY $ir]
    if {![get_property IS_SYNTHESIS $sr] || ![get_property IS_IMPLEMENTATION $ir]} {
        fail run_type_mismatch "Configured run types do not match synthesis/implementation"
    }
    if {[get_property PARENT $ir] ne $synth} {
        fail run_parent_mismatch "Implementation does not depend on configured synthesis run"
    }
    set f [open [file join $out reports project_check.txt] w]
    fconfigure $f -encoding utf-8
    foreach run [list $sr $ir] {
        puts $f "RUN: $run"
        foreach property [list_property $run] {
            puts $f "$property = [get_property $property $run]"
        }
    }
    foreach fileset [get_filesets -quiet] {
        puts $f "FILESET: $fileset TYPE: [get_property FILESET_TYPE $fileset]"
    }
    foreach file [get_files -quiet] {
        set name [get_property NAME $file]
        puts $f "FILE: $name"
        if {![file exists $name]} {
            close $f
            fail source_missing "Project source missing: $name"
        }
    }
    close $f
    if {$read_only} {
        # Vivado 2020.2 can initialize BD automation rules while reporting IP
        # status. That initialization attempts to save the project and fails
        # for a read-only open (Project 1-228 / Ip 78-90). IP status is only a
        # diagnostic here; keep read-only operations free of that mutation.
        # Source/run checks above and implemented Timing/DRC gates still apply.
        set note [open [file join $out reports ip_status_not_queried.txt] w]
        puts $note "IP status was not queried in read-only mode; BD rule initialization may write project metadata."
        close $note
    } else {
        report_ip_status -file [file join $out reports ip_status.rpt]
    }
    emit checked true vivado_version [version -short] synth_run $synth impl_run $impl
}

proc fpga::require_ip_ready {} {
    variable synth
    foreach ip [get_ips -quiet] {
        if {[get_property IS_LOCKED $ip]} {
            fail ip_locked "IP is locked; CLI will not upgrade or regenerate it: $ip"
        }
    }
    # Any auxiliary synthesis run must already be usable. launch_runs can otherwise
    # implicitly build IP dependencies, which is outside V1's allowed operations.
    foreach run [get_runs -quiet] {
        if {[get_property IS_SYNTHESIS $run] && [get_property NAME $run] ne $synth} {
            set fileset [get_filesets -quiet [get_property SRCSET $run]]
            if {[llength $fileset] != 1} {
                fail ip_dependency_unresolved "Cannot identify source fileset for $run"
            }
            # Unselected top-level strategies are not IP dependencies.
            set kind [get_property FILESET_TYPE $fileset]
            if {$kind eq "DesignSrcs"} { continue }
            if {$kind ne "BlockSrcs"} {
                fail ip_dependency_unresolved "Unknown auxiliary synthesis fileset type: $kind"
            }
            if {[get_property STATUS $run] ne "synth_design Complete!" || [get_property NEEDS_REFRESH $run]} {
                fail ip_result_missing "Auxiliary synthesis result is missing/stale: $run; automatic IP synthesis is disabled"
            }
        }
    }
    foreach ip [get_ips -quiet] {
        set xci [get_property IP_FILE $ip]
        set files [get_files -quiet $xci]
        if {[llength $files] && [get_property GENERATE_SYNTH_CHECKPOINT $files]} {
            if {![file exists "[file rootname $xci].dcp"]} {
                fail ip_result_missing "IP checkpoint missing: $ip"
            }
        }
    }
}

proc fpga::synth {} {
    variable synth
    variable jobs
    variable out
    emit stage synthesis
    require_ip_ready
    set run [run_named $synth]
    reset_run $run
    emit synthesis_status running
    launch_runs $run -jobs $jobs
    wait_on_run $run
    set status [get_property STATUS $run]
    emit synthesis_run_status $status
    if {$status ne "synth_design Complete!" || [get_property NEEDS_REFRESH $run]} {
        emit synthesis_status failed
        fail synthesis_failed "Synthesis did not complete: $status"
    }
    emit synthesis_status passed
    open_run $run
    report_utilization -file [file join $out reports synthesis_utilization.rpt]
    close_design
    emit stage post_synth
}

proc fpga::require_synthesis {} {
    variable synth
    set run [run_named $synth]
    set status [get_property STATUS $run]
    emit synthesis_run_status $status
    if {$status ne "synth_design Complete!"} {
        fail synthesis_result_missing "Successful synthesis is required; found: $status"
    }
    if {[get_property NEEDS_REFRESH $run]} {
        fail synthesis_result_stale "Synthesis is out of date; run synth explicitly"
    }
    if {![llength [glob -nocomplain -directory [get_property DIRECTORY $run] *.dcp]]} {
        fail synthesis_result_missing "Synthesis checkpoint is missing"
    }
    emit synthesis_status passed
}

proc fpga::impl {} {
    variable impl
    variable jobs
    variable out
    emit stage implementation_preflight
    require_synthesis
    require_ip_ready
    set run [run_named $impl]
    set last_step route_design
    if {[get_property STEPS.POST_ROUTE_PHYS_OPT_DESIGN.IS_ENABLED $run]} {
        # launch_runs and STATUS use the display name, not the XPR Step Id.
        set last_step {phys_opt_design (Post-Route)}
    }
    reset_run $run
    emit stage implementation implementation_status running
    launch_runs $run -to_step $last_step -jobs $jobs
    wait_on_run $run
    set status [get_property STATUS $run]
    emit implementation_run_status $status
    if {![completed_status $status $last_step] || [get_property NEEDS_REFRESH $run]} {
        emit implementation_status failed
        fail implementation_failed "Implementation did not complete: $status"
    }
    emit implementation_status passed stage post_route
    open_run $run
    report_utilization -file [file join $out reports implementation_utilization.rpt]
    report_route_status -file [file join $out reports route_status.rpt]
    close_design
}

proc fpga::completed_status {status step} {
    # Vivado reports a completed route with timing violations using a distinct
    # completion string. That is design acceptance failure, not execution failure.
    return [expr {$status eq "$step Complete!" || $status eq "$step Complete, Failed Timing!"}]
}

proc fpga::open_implementation {} {
    variable impl
    require_synthesis
    set run [run_named $impl]
    set status [get_property STATUS $run]
    emit implementation_run_status $status
    set complete 0
    foreach step [list route_design {phys_opt_design (Post-Route)} write_bitstream] {
        if {[completed_status $status $step]} { set complete 1 }
    }
    if {!$complete} {
        fail implementation_result_missing "Completed routed implementation required; found: $status"
    }
    if {[get_property NEEDS_REFRESH $run]} {
        fail implementation_result_stale "Implementation result is out of date"
    }
    if {[catch {open_run $run} message]} {
        fail implementation_result_unreadable $message
    }
    emit implementation_status passed
}

proc fpga::timing {} {
    variable out
    emit stage timing
    open_implementation
    report_timing_summary -delay_type min_max -file [file join $out reports timing_summary.rpt]
    close_design
    emit timing_complete true stage post_route
}

proc fpga::drc {} {
    variable out
    emit stage drc
    open_implementation
    if {[info exists ::env(FPGA_BITSTREAM_REQUESTED)] && $::env(FPGA_BITSTREAM_REQUESTED) eq "1"} {
        report_drc -ruledeck bitstream_checks -file [file join $out reports drc.rpt]
    } else { report_drc -file [file join $out reports drc.rpt] }
    close_design
    emit drc_complete true stage post_route
}

proc fpga::build {} {
    synth
    impl
    timing
    drc
}

proc fpga::bit {} {
    # Generate fresh reports; Python applies the same acceptance policy as build.
    timing
    drc
}

proc fpga::write_bit {} {
    emit stage bitstream bitstream_status running
    open_implementation
    set output $::env(FPGA_BITSTREAM_OUTPUT)
    set temporary "[file rootname $output].partial.bit"
    if {[file exists $output] || [file exists $temporary]} {
        fail bitstream_output_exists "Refusing to reuse existing bitstream output"
    }
    write_bitstream $temporary
    if {![file isfile $temporary] || [file size $temporary] == 0} {
        fail bitstream_output_missing "Vivado did not produce a nonempty bitstream"
    }
    file rename $temporary $output
    close_design
    emit bitstream_status passed bitstream_path $output
}

proc fpga::main {command} {
    variable opened
    variable data
    set failed [catch {
        check [expr {$command in {check timing drc bit write_bit}}]
        if {$command ne "check"} { $command }
        close_project
        set opened 0
        emit done true
    } message options]
    if {$failed} {
        set reason tcl_error
        if {[dict exists $options -errorcode] && [lindex [dict get $options -errorcode] 0] eq "FPGA_AUTO"} {
            set reason [lindex [dict get $options -errorcode] 1]
        }
        set stage [dict get $data stage]
        if {$stage eq "synthesis" && [dict exists $data synthesis_status] && [dict get $data synthesis_status] eq "running"} {
            dict set data synthesis_status failed
            if {$reason eq "tcl_error"} { set reason synthesis_failed }
        }
        if {$stage eq "implementation" && [dict exists $data implementation_status] && [dict get $data implementation_status] eq "running"} {
            dict set data implementation_status failed
            if {$reason eq "tcl_error"} { set reason implementation_failed }
        }
        if {$stage eq "bitstream"} {
            dict set data bitstream_status failed
            if {$reason eq "tcl_error"} { set reason bitstream_failed }
        }
        catch {emit reason $reason message $message done false}
        # Some Vivado batch sessions have no Tcl stderr channel. Never let error
        # logging prevent publication of the actual failure reason.
        if {[catch {puts stderr [dict get $options -errorinfo]}]} {
            catch {puts [dict get $options -errorinfo]}
        }
        if {$opened} { catch {close_project} }
        exit 1
    }
    exit 0
}
