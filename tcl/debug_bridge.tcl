# Vivado Debug MCP 1.4: explicit hardware commands over a private file mailbox.
# Sourcing with --check validates commands only. It never opens Hardware Manager.
namespace eval ::vdbg {
    variable bindings [dict create]
    variable cores [dict create]
    variable current ""
    variable manager_open 0
    variable quitting 0
}
proc ::vdbg::q {value} {
    set substitutions [list \\ \\\\ \" \\\"]
    for {set i 0} {$i < 32} {incr i} {
        lappend substitutions [format %c $i] [format {\u%04x} $i]
    }
    return "\"[string map $substitutions $value]\""
}
proc ::vdbg::object {args} {
    set fields [list]
    foreach {key value} $args { lappend fields "[q $key]:$value" }
    return "\{[join $fields ,]\}"
}
proc ::vdbg::array {values} { return "\[[join $values ,]\]" }
proc ::vdbg::required {params key} {
    if {![dict exists $params $key]} { error "Missing parameter: $key" }
    return [dict get $params $key]
}
proc ::vdbg::capabilities {} {
    set names {open_hw_manager connect_hw_server refresh_hw_server get_hw_targets open_hw_target
        get_hw_devices refresh_hw_device get_hw_vios get_hw_ilas get_hw_probes
        commit_hw_vio refresh_hw_vio run_hw_ila wait_on_hw_ila
        upload_hw_ila_data write_hw_ila_data reset_hw_ila}
    set missing [list]
    foreach name $names {
        if {[llength [info commands ::$name]] == 0} { lappend missing [q $name] }
    }
    return [object vivado_version [q [version -short]] missing_commands [array $missing] hardware_connected false]
}
proc ::vdbg::select_endpoint {endpoint {require_config 1}} {
    variable bindings
    variable current
    if {![dict exists $bindings $endpoint]} { error "Unknown bound endpoint" }
    set binding [dict get $bindings $endpoint]
    if {$current ne $endpoint} {
        if {$current ne ""} { close_hw_target [dict get $bindings $current target] }
        set current ""
        set target [dict get $binding target]
        set found 0
        foreach candidate [get_hw_targets] { if {$candidate eq $target} { set found 1 } }
        if {!$found} { error "Bound hardware target disappeared" }
        # 2020.2 -of_objects requires the resolved hw_target object, not a plain path string.
        set target [get_hw_targets $target]
        open_hw_target $target
        set devices [list]
        foreach candidate [get_hw_devices -of_objects $target] {
            if {[get_property PART $candidate] eq [dict get $binding part]} { lappend devices $candidate }
        }
        if {[llength $devices] != 1} { error "Expected exactly one matching device on target" }
        set device [lindex $devices 0]
        current_hw_device $device
        refresh_hw_device -update_hw_probes false $device
        if {[string toupper [get_property REGISTER.EFUSE.FUSE_DNA $device]] ne [dict get $binding dna]} {
            error "Physical device DNA mismatch"
        }
        if {[get_property REGISTER.CONFIG_STATUS.BIT13_DONE_INTERNAL_SIGNAL_STATUS $device] == 1} {
            set_property PROBES.FILE [dict get $binding ltx] $device
            set_property FULL_PROBES.FILE [dict get $binding ltx] $device
            refresh_hw_device $device
        }
        dict set bindings $endpoint device $device
        set current $endpoint
    }
    set device [dict get $bindings $endpoint device]
    current_hw_device $device
    # Keep the populated probe model for the complete logical operation.
    # A device refresh with -update_hw_probes false invalidates the ILA model
    # in 2020.2; it must not run between arm/wait/upload or staged VIO writes.
    if {[string toupper [get_property REGISTER.EFUSE.FUSE_DNA $device]] ne [dict get $binding dna]} {
        error "Physical device DNA changed"
    }
    if {$require_config && ([get_property REGISTER.CONFIG_STATUS.BIT13_DONE_INTERNAL_SIGNAL_STATUS $device] != 1 ||
        [get_property REGISTER.CONFIG_STATUS.BIT00_CRC_ERROR $device] != 0)} {
        error "Device identity/configuration changed; session requires revalidation"
    }
    return $device
}
proc ::vdbg::core {endpoint alias kind} {
    variable cores
    set device [select_endpoint $endpoint]
    set definition [dict get $cores $alias]
    if {[dict get $definition kind] ne $kind} { error "Wrong core kind" }
    if {$kind eq "vio"} { set candidates [get_hw_vios -of_objects $device] } else {
        set candidates [get_hw_ilas -of_objects $device]
    }
    set matches [list]
    foreach candidate $candidates {
        if {[get_property CELL_NAME $candidate] eq [dict get $definition cell]} { lappend matches $candidate }
    }
    if {[llength $matches] != 1} { error "Debug core missing or ambiguous" }
    set found [lindex $matches 0]
    if {[string toupper [get_property CORE_UUID $found]] ne [dict get $definition uuid]} {
        error "Debug core UUID mismatch"
    }
    return $found
}
proc ::vdbg::probe {core alias port} {
    variable cores
    set name "vdbg_[dict get $cores $alias uuid]_$port"
    set result [get_hw_probes -quiet $name -of_objects $core]
    if {[llength $result] != 1} { error "Physical probe alias missing: $port" }
    return $result
}
proc ::vdbg::connect_server {server {budget_ms 45000}} {
    # The 2020.2 GUI connects with -allow_non_jtag, then discovers the cable.
    # Cold/hot-plug enumeration may initially report 44-494. Retry only this
    # connection-readiness phase; no device/probe commands have run here.
    set deadline [expr {[clock milliseconds] + $budget_ms}]
    set attempt 0
    while {1} {
        incr attempt
        set code [catch {
            connect_hw_server -url $server -allow_non_jtag
            refresh_hw_server -force_poll
            get_hw_targets -quiet
        } message options]
        if {!$code && [llength $message] > 0} {
            return [object connected true connection_attempts $attempt]
        }
        if {!$code} {
            set message "No active hardware target after server connection"
            set recoverable 1
        } else {
            set recoverable [regexp {Labtoolstcl 44-494} $message]
        }
        if {!$recoverable} { return -options $options $message }
        catch {disconnect_hw_server}
        if {[clock milliseconds] >= $deadline} {
            error "Hardware target readiness timed out after $attempt attempts. Last diagnostic: $message"
        }
        puts "VDBG_CONNECT_RETRY attempt=$attempt reason=$message"
        after 500
    }
}
proc ::vdbg::dispatch {operation params} {
    variable bindings
    variable cores
    variable manager_open
    variable current
    variable quitting
    switch -exact -- $operation {
        capabilities { return [capabilities] }
        connect {
            if {$manager_open} { error "Hardware manager already open" }
            open_hw_manager
            set manager_open 1
            return [connect_server [required $params server]]
        }
        bind {
            set endpoint [required $params endpoint]
            dict set bindings $endpoint $params
            select_endpoint $endpoint 0
            return [object bound [q $endpoint]]
        }
        bind_core {
            dict set cores [required $params alias] $params
            return [object bound true]
        }
        status {
            set endpoint [required $params endpoint]
            set device [select_endpoint $endpoint 0]
            refresh_hw_device -update_hw_probes false $device
            if {[string toupper [get_property REGISTER.EFUSE.FUSE_DNA $device]] ne [dict get $bindings $endpoint dna]} {
                error "Physical device DNA changed"
            }
            if {[get_property REGISTER.CONFIG_STATUS.BIT13_DONE_INTERNAL_SIGNAL_STATUS $device] == 1} {
                refresh_hw_device $device
            }
            return [object endpoint [q $endpoint] dna [q [get_property REGISTER.EFUSE.FUSE_DNA $device]] \
                done [q [get_property REGISTER.CONFIG_STATUS.BIT13_DONE_INTERNAL_SIGNAL_STATUS $device]] \
                crc_error [q [get_property REGISTER.CONFIG_STATUS.BIT00_CRC_ERROR $device]] \
                config_status [q [get_property REGISTER.CONFIG_STATUS $device]]]
        }
        vio_read - vio_set - vio_commit {
            set endpoint [required $params endpoint]
            set alias [required $params core]
            set vio [core $endpoint $alias vio]
            if {$operation eq "vio_commit"} {
                commit_hw_vio $vio
                return [object committed true]
            }
            set port [required $params port]
            set p [probe $vio $alias $port]
            if {$operation eq "vio_set"} {
                if {![string match out* $port]} { error "Writing an input probe is prohibited" }
                set value [required $params value]
                if {![regexp {^[0-9a-fA-F]+$} $value]} { error "Expected full-width hex" }
                set_property OUTPUT_VALUE $value $p
                return [object staged true]
            }
            refresh_hw_vio $vio
            set property [expr {[string match out* $port] ? "OUTPUT_VALUE" : "INPUT_VALUE"}]
            return [object value [q [get_property $property $p]] width [get_property WIDTH $p]]
        }
        ila_arm {
            set alias [required $params core]
            set ila [core [required $params endpoint] $alias ila]
            reset_hw_ila $ila
            set_property CONTROL.DATA_DEPTH [required $params depth] $ila
            set_property CONTROL.TRIGGER_POSITION [required $params position] $ila
            # CONTROL.TRIGGER_MODE is a read-only capability in Vivado 2020.2.
            set_property CONTROL.TRIGGER_CONDITION AND $ila
            set_property CONTROL.CAPTURE_MODE ALWAYS $ila
            set_property CONTROL.WINDOW_COUNT 1 $ila
            # Comparisons are GUI properties, not reset by reset_hw_ila.
            # Each capture starts with all physical probes as don't-care.
            foreach p [get_hw_probes -of_objects $ila] {
                set width [get_property WIDTH $p]
                set_property TRIGGER_COMPARE_VALUE "eq${width}'b[string repeat x $width]" $p
            }
            for {set i 0} {$i < [required $params trigger_count]} {incr i} {
                set p [probe $ila $alias [required $params trigger_${i}_port]]
                set comparison [required $params trigger_${i}_value]
                # Preserve the public "ne" shorthand; 2020.2 spells it "neq".
                if {[regexp {^ne([0-9]+'.+)$} $comparison -> suffix]} { set comparison "neq$suffix" }
                set_property TRIGGER_COMPARE_VALUE $comparison $p
            }
            if {[required $params immediate]} { run_hw_ila -trigger_now $ila } else { run_hw_ila $ila }
            return [object armed true]
        }
        ila_wait {
            set ila [core [required $params endpoint] [required $params core] ila]
            # Vivado 2020.2 takes minutes here and accepts decimal values.
            # Keep each worker-side wait below the 5 s mailbox deadline.
            if {[catch {wait_on_hw_ila -timeout [expr {1.0 / 60.0}] $ila} message]} {
                if {[regexp -nocase {timeout|timed out|time out} $message]} { return [object complete false] }
                error $message
            }
            # A timeout can also return TCL_OK in 2020.2. Prove that this
            # buffer is full before accepting it; upload validates the rows.
            set state [get_property STATUS.CORE_STATUS $ila]
            set samples [get_property STATUS.SAMPLE_COUNT $ila]
            set expected [get_property CONTROL.DATA_DEPTH $ila]
            set complete [expr {$state eq "FULL" && $samples == $expected}]
            return [object complete [expr {$complete ? "true" : "false"}] \
                core_status [q $state] captured_samples $samples expected_samples $expected]
        }
        ila_stop {
            # Vivado 2020.2 has no stop_hw_ila command. upload_hw_ila_data
            # stops acquisition and retrieves the possibly partial buffer.
            set ila [core [required $params endpoint] [required $params core] ila]
            upload_hw_ila_data $ila
            set state [get_property STATUS.CORE_STATUS $ila]
            if {$state ne "IDLE" && $state ne "FULL"} { error "ILA stop not confirmed: $state" }
            return [object stopped true core_status [q $state] partial_buffer true capture_accepted false]
        }
        ila_upload {
            set ila [core [required $params endpoint] [required $params core] ila]
            set base [required $params output]
            foreach extension {.ila .csv} {
                if {[file exists ${base}${extension}]} { error "Capture output already exists" }
            }
            set data [upload_hw_ila_data $ila]
            write_hw_ila_data ${base}.ila $data
            write_hw_ila_data -csv_file ${base}.csv $data
            return [object ila [q ${base}.ila] csv [q ${base}.csv]]
        }
        quit {
            if {$current ne ""} { catch {close_hw_target [dict get $bindings $current target]} }
            if {$manager_open} { catch {disconnect_hw_server}; catch {close_hw_manager} }
            set quitting 1
            return [object closed true]
        }
        default { error "Unsupported debug operation: $operation" }
    }
}
proc ::vdbg::write_atomic {path data} {
    set stream [open ${path}.tmp w]
    fconfigure $stream -encoding utf-8 -translation lf
    puts $stream $data
    close $stream
    file rename ${path}.tmp $path
}
if {[version -short] ne "2020.2"} { error "Vivado Debug MCP requires exactly Vivado 2020.2" }
# Vivado 2020.2 lazily registers Labtools commands. Loading the feature does not
# open Hardware Manager or connect a server/target.
if {[llength [info commands ::open_hw_manager]] == 0 && [llength [info commands ::load_features]]} {
    load_features labtools
}
if {$argc == 1 && [lindex $argv 0] eq "--check"} {
    puts "VDBG_CAPABILITIES [::vdbg::capabilities]"
} elseif {$argc == 1} {
    set mailbox [file normalize [lindex $argv 0]]
    ::vdbg::write_atomic [file join $mailbox ready.json] [::vdbg::capabilities]
    set sequence 1
    while {!$::vdbg::quitting} {
        set request [file join $mailbox [format request-%08d.req $sequence]]
        if {![file exists $request]} { after 20; continue }
        set operation ""
        set params [dict create]
        if {[catch {
            set stream [open $request r]
            fconfigure $stream -encoding utf-8
            set wire [read $stream]
            close $stream
            if {[string length $wire] > 262144} { error "Oversized request" }
            foreach line [split [string trim $wire] \n] {
                lassign [split $line \t] key value
                if {![regexp {^[a-zA-Z0-9_]+$} $key] || ![regexp {^(?:[0-9a-fA-F]{2})*$} $value]} {
                    error "Invalid request encoding"
                }
                set decoded [encoding convertfrom utf-8 [binary format H* $value]]
                if {$key eq "operation"} { set operation $decoded } else { dict set params $key $decoded }
            }
            set result [::vdbg::dispatch $operation $params]
        } message options]} {
            set reply [::vdbg::object ok false reason [::vdbg::q vivado_debug_failed] message [::vdbg::q $message]]
        } else { set reply [::vdbg::object ok true result $result] }
        ::vdbg::write_atomic [file join $mailbox [format reply-%08d.json $sequence]] $reply
        incr sequence
    }
} else { error "Usage: debug_bridge.tcl mailbox_directory | --check" }
