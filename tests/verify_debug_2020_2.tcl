# Offline ONLY: command availability + bridge behavior with every hardware API stubbed.
set argc 1
set argv {--check}
source [file normalize [file join [file dirname [info script]] .. tcl debug_bridge.tcl]]
if {[version -short] ne "2020.2"} { error "Wrong Vivado version" }
set names {open_hw_manager connect_hw_server refresh_hw_server get_hw_targets open_hw_target close_hw_target
    get_hw_devices refresh_hw_device current_hw_device get_hw_vios get_hw_ilas get_hw_probes
    commit_hw_vio refresh_hw_vio run_hw_ila wait_on_hw_ila upload_hw_ila_data
    write_hw_ila_data reset_hw_ila set_property get_property disconnect_hw_server close_hw_manager}
foreach name $names {
    if {[llength [info commands ::$name]] != 1} { error "Missing 2020.2 command: $name" }
}
# Preserve originals in this disposable Tcl process. No original hardware API is invoked.
foreach name $names { rename ::$name ::vdbg::original_$name }
set ::mock_output "000000000000"
set ::mock_commits 0
set ::mock_arms 0
set ::mock_probe_model_valid 0
set ::mock_connect_attempts 0
set ::mock_connect_failures 2
set ::mock_connect_fatal 0
set ::mock_core_status FULL
set ::mock_sample_count 1024
set ::mock_control_depth 1024
set ::mock_stop_stuck 0
foreach name {open_hw_manager open_hw_target close_hw_target
    current_hw_device refresh_hw_vio reset_hw_ila disconnect_hw_server close_hw_manager} {
    proc ::$name {args} { return "" }
}
proc ::connect_hw_server {args} {
    incr ::mock_connect_attempts
    if {[lsearch -exact $args -allow_non_jtag] < 0} { error "GUI-compatible connection flag is missing" }
    if {$::mock_connect_fatal} { error "Invalid server configuration" }
    if {$::mock_connect_attempts <= $::mock_connect_failures} {
        error {ERROR: [Labtoolstcl 44-494] There is no active target available}
    }
    return offline-server
}
proc ::refresh_hw_server {args} {
    if {[lsearch -exact $args -force_poll] < 0} { error "Initial discovery must force a target poll" }
}
proc ::get_hw_targets {args} { return {offline-target} }
proc ::get_hw_devices {args} { return {offline-device} }
proc ::get_hw_vios {args} { return {offline-vio} }
proc ::get_hw_ilas {args} { return {offline-ila} }
proc ::refresh_hw_device {args} {
    set index [lsearch -exact $args -update_hw_probes]
    set ::mock_probe_model_valid [expr {$index < 0 || [lindex $args [expr {$index + 1}]] ne "false"}]
}
proc ::get_hw_probes {args} {
    if {!$::mock_probe_model_valid} { error "Refresh with valid LTX before using probes" }
    return [lindex $args 1]
}
proc ::get_property {property object} {
    switch -exact -- $property {
        PART { return xcku040 }
        REGISTER.EFUSE.FUSE_DNA { return 000000000000000000000001 }
        REGISTER.CONFIG_STATUS.BIT13_DONE_INTERNAL_SIGNAL_STATUS { return 1 }
        REGISTER.CONFIG_STATUS.BIT00_CRC_ERROR { return 0 }
        CELL_NAME { return [expr {$object eq "offline-vio" ? "test_vio" : "test_ila"}] }
        CORE_UUID { return [expr {$object eq "offline-vio" ? [string repeat A 32] : [string repeat B 32]}] }
        WIDTH { return 48 }
        OUTPUT_VALUE { return $::mock_output }
        INPUT_VALUE { return ffffffffffff }
        STATUS.CORE_STATUS { return $::mock_core_status }
        STATUS.SAMPLE_COUNT { return $::mock_sample_count }
        CONTROL.DATA_DEPTH { return $::mock_control_depth }
        default { return 0 }
    }
}
proc ::set_property {property value object} {
    if {$property eq "CONTROL.DATA_DEPTH"} { set ::mock_control_depth $value }
    if {$property eq "CONTROL.TRIGGER_MODE"} { error "2020.2 property is read-only" }
    if {$property eq "TRIGGER_COMPARE_VALUE"} {
        if {[regexp {^ne[0-9]} $value]} { error "2020.2 requires neq" }
        set ::mock_comparison $value
    }
    if {$property eq "OUTPUT_VALUE"} { set ::mock_output $value }
}
proc ::commit_hw_vio {args} { incr ::mock_commits }
proc ::run_hw_ila {args} { incr ::mock_arms }
proc ::wait_on_hw_ila {args} {
    set index [lsearch -exact $args -timeout]
    set minutes [lindex $args [expr {$index + 1}]]
    if {$index < 0 || $minutes <= 0 || $minutes > 0.02} { error "ILA wait must use sub-second/minute conversion" }
    return ""
}
proc ::upload_hw_ila_data {args} {
    if {!$::mock_stop_stuck} { set ::mock_core_status IDLE; set ::mock_sample_count 0 }
    return offline-samples
}
proc ::write_hw_ila_data {args} { return "" }
::vdbg::dispatch connect [dict create server offline.invalid:0]
if {$::mock_connect_attempts != 3} { error "Transient discovery did not recover without a new session" }
::vdbg::dispatch bind [dict create endpoint A target offline-target dna 000000000000000000000001 part xcku040 ltx unused.ltx]
::vdbg::dispatch bind_core [dict create alias vio kind vio cell test_vio uuid [string repeat A 32]]
::vdbg::dispatch bind_core [dict create alias ila kind ila cell test_ila uuid [string repeat B 32]]
::vdbg::dispatch vio_set [dict create endpoint A core vio port out0 value 020000000080]
::vdbg::dispatch vio_commit [dict create endpoint A core vio]
if {$::mock_output ne "020000000080" || $::mock_commits != 1} { error "Wide VIO commit failed" }
puts "VDBG_WIDE_READ [::vdbg::dispatch vio_read [dict create endpoint A core vio port out0]]"
::vdbg::dispatch ila_arm [dict create endpoint A core ila depth 1024 position 0 trigger_count 0 immediate 1]
puts "VDBG_ILA_WAIT [::vdbg::dispatch ila_wait [dict create endpoint A core ila]]"
set ::mock_core_status WAITING_FOR_TRIGGER
set ::mock_sample_count 0
set waiting [::vdbg::dispatch ila_wait [dict create endpoint A core ila]]
if {![string match {*"complete":false*} $waiting]} { error "TCL_OK after a wait timeout was treated as completed capture" }
set ::mock_core_status FULL
set ::mock_sample_count 512
set partial [::vdbg::dispatch ila_wait [dict create endpoint A core ila]]
if {![string match {*"complete":false*} $partial]} { error "Partial sample count was accepted" }
set ::mock_core_status WAITING_FOR_TRIGGER
set ::mock_stop_stuck 1
if {![catch {::vdbg::dispatch ila_stop [dict create endpoint A core ila]}]} { error "Unconfirmed ILA stop was accepted" }
set ::mock_stop_stuck 0
set stopped [::vdbg::dispatch ila_stop [dict create endpoint A core ila]]
if {![string match {*"core_status":"IDLE"*} $stopped]} { error "Stopped core state is missing" }
if {$::mock_arms != 1} { error "ILA arm command failed" }
::vdbg::dispatch ila_arm [dict create endpoint A core ila depth 1024 position 512 trigger_count 1 \
    trigger_0_port in0 trigger_0_value "ne48'hffffffffffff" immediate 0]
if {$::mock_comparison ne "neq48'hffffffffffff" || $::mock_arms != 2} { error "ILA ne/neq compatibility failed" }
if {![catch {::vdbg::dispatch unsupported [dict create]}]} { error "Arbitrary operation was accepted" }
puts "VDBG_ESCAPING [::vdbg::q {a\b\"c}]"
::vdbg::dispatch quit [dict create]
set ::mock_connect_attempts 0
set ::mock_connect_fatal 1
if {![catch {::vdbg::connect_server offline.invalid:0} reason] || $::mock_connect_attempts != 1} {
    error "Definitive connection error was retried"
}
set ::mock_connect_attempts 0
set ::mock_connect_fatal 0
set ::mock_connect_failures 99
if {![catch {::vdbg::connect_server offline.invalid:0 0} reason] || $::mock_connect_attempts != 1 ||
        ![string match {*readiness timed out*} $reason]} { error "Readiness retry did not obey its deadline" }
puts {VDBG_OFFLINE_TEST {"passed":true,"vivado_version":"2020.2","original_hardware_api_calls":0,"hardware_connected":false}}
