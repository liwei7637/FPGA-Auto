"""Offline debug tests: independent historical evidence + model and process boundaries."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fpga_auto.jobs import Job, read_json
from fpga_auto.runner import process_identity
from vivado_debug.analysis import analyze_ila_csv, analyze_udp, check_pair, make_snapshot, read_snapshots, throughput
from vivado_debug.backend import VivadoBackend
from vivado_debug.common import DebugError, FileLock, integer
from vivado_debug.common import atomic_json, sharing_retry
from vivado_debug.ltx import describe, normalize, validate_profile
from vivado_debug.mock import MockBackend
from vivado_debug.profile import load_machine, load_profile, mock_machine
from vivado_debug.service import DebugService
from vivado_debug.sessions import SessionClient, notification_context
from vivado_debug.exports import export_mac_pcap

FIXTURES = Path(__file__).resolve().parents[1] / "tests/fixtures/debug"


class DebugDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vivado-debug-data-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_uint64_and_wide_values_preserve_precision(self):
        self.assertEqual(integer("18446744073709551615"), (1 << 64) - 1)
        self.assertEqual(integer("0xffffffffffffffff"), (1 << 64) - 1)

    def test_transient_windows_record_sharing_does_not_fail_job_query(self):
        with patch("vivado_debug.sessions.job_state", side_effect=[PermissionError("sharing"), {"state": "running", "result": None}]) as query:
            directory = self.root / ".fpga/jobs/example"
            directory.mkdir(parents=True)
            value = SessionClient(self.root).job_get("example")
        self.assertEqual(value["state"], "running")
        self.assertEqual(query.call_count, 2)

    def test_reject_float_bool_negative_overflow_and_code(self):
        for value in [True, 1.0, -1, "-1", str(1 << 64), "1; exec calc", "0x", "NaN"]:
            with self.subTest(value=value), self.assertRaises(DebugError):
                integer(value)

    def test_offline_profile_matches_real_2020_2_layout(self):
        layout = describe(FIXTURES / "real_layout.ltx")
        validate_profile(layout, load_profile())
        self.assertEqual(len(layout["cores"]), 6)
        vio = next(c for c in layout["cores"] if c["kind"] == "vio")
        self.assertEqual(vio["ports"]["in1"], 256)
        self.assertEqual(vio["ports"]["out0"], 48)

    def test_derived_ltx_preserves_source_uuid_and_port_coverage(self):
        source = FIXTURES / "real_layout.ltx"
        original = source.read_bytes()
        output = self.root / "normalized.ltx"
        result = normalize(source, output, load_profile())
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(describe(source)["cores"], describe(output)["cores"])
        self.assertNotEqual(result["original_sha256"], result["derived_sha256"])
        with self.assertRaises(DebugError):
            normalize(source, output, load_profile())

    def test_ltx_uuid_or_width_mismatch_rejected(self):
        profile = load_profile()
        profile["cores"]["traffic"]["ports"]["out0"] = 32
        with self.assertRaises(DebugError):
            validate_profile(describe(FIXTURES / "real_layout.ltx"), profile)
        profile = load_profile()
        profile["cores"]["traffic"]["uuid"] = "0" * 32
        with self.assertRaises(DebugError):
            validate_profile(describe(FIXTURES / "real_layout.ltx"), profile)

    def test_ltx_missing_bit_and_overlap_rejected(self):
        base = {"ltx_root": {"ltx_data": [{"debug_cores": [{"name": "core", "type": "VIO_V2", "uuid": "a" * 32,
                "pins": [{"direction": "IN", "portIndex": 0, "leftIndex": 1, "rightIndex": 2}]}]}]}}
        path = self.root / "invalid.ltx"
        path.write_text(json.dumps(base))
        with self.assertRaises(DebugError):
            describe(path)
        pin = base["ltx_root"]["ltx_data"][0]["debug_cores"][0]["pins"][0]
        pin.update(leftIndex=0, rightIndex=1)
        base["ltx_root"]["ltx_data"][0]["debug_cores"][0]["pins"].append(copy.deepcopy(pin))
        path.write_text(json.dumps(base))
        with self.assertRaises(DebugError):
            describe(path)

    def test_hardware_requires_machine_binding(self):
        with self.assertRaises(DebugError):
            load_machine(hardware=True)

    def test_machine_rejects_ambiguous_targets_and_duplicate_dna(self):
        data = mock_machine()
        data["server"] = "localhost:3121"
        path = self.root / "machine.json"
        for change in ("wildcard", "duplicate"):
            x = copy.deepcopy(data)
            if change == "wildcard":
                x["endpoints"]["A"]["target"] = "*"
            else:
                x["endpoints"]["B"]["dna"] = x["endpoints"]["A"]["dna"]
            path.write_text(json.dumps(x))
            with self.assertRaises(DebugError):
                load_machine(path, hardware=True)

    def test_replay_real_controlled_rate_pass(self):
        x = analyze_udp(FIXTURES / "gap80.tsv", begin_tag="stream_begin", end_tag="stream_end")
        self.assertEqual(x["verdict"], "PASS")
        self.assertEqual(x["source_kind"], "historical_replay")
        self.assertFalse(x["board_result_update"])
        for rate in x["throughput"].values():
            self.assertAlmostEqual(rate["udp_payload_gbps"], 8.47194, places=4)

    def test_replay_real_full_rate_tail_loss_fail(self):
        x = analyze_udp(FIXTURES / "gap0.tsv")
        self.assertEqual(x["verdict"], "FAIL")
        self.assertTrue(any("rx_packets" in r for r in x["reasons"]))

    def test_incomplete_or_duplicate_pages_are_inconclusive(self):
        lines = (FIXTURES / "gap80.tsv").read_text().splitlines()
        for data in ("\n".join(lines[1:]), "\n".join(lines + [lines[0]])):
            path = self.root / "broken.tsv"
            path.write_text(data)
            self.assertEqual(analyze_udp(path)["verdict"], "INCONCLUSIVE")

    def test_raw_page_slot_disagreement_is_inconclusive(self):
        lines = (FIXTURES / "gap80.tsv").read_text().splitlines()
        row = lines[0].split("\t")
        row[3] = str(int(row[3]) + 1)
        lines[0] = "\t".join(row)
        path = self.root / "broken.tsv"
        path.write_text("\n".join(lines))
        self.assertEqual(analyze_udp(path)["verdict"], "INCONCLUSIVE")

    def test_snapshot_valid_required(self):
        with self.assertRaises(DebugError):
            make_snapshot([0] * 32, 4)

    def test_repeated_snapshot_or_reset_invalidates_throughput(self):
        groups = read_snapshots(FIXTURES / "gap80.tsv")
        begin, end = groups["stream_begin"]["0"], groups["stream_end"]["0"]
        with self.assertRaises(DebugError):
            throughput(begin, begin)
        broken = copy.deepcopy(end)
        broken["values"][16] = "0"
        with self.assertRaises(DebugError):
            throughput(begin, broken)

    def test_ready_drop_invalidates_throughput(self):
        groups = read_snapshots(FIXTURES / "gap80.tsv")
        begin, end = groups["stream_begin"]["0"], copy.deepcopy(groups["stream_end"]["0"])
        end["values"][19] = str(int(begin["values"][19]) + 1)
        with self.assertRaises(DebugError):
            throughput(begin, end)

    def test_constant_ila_window_is_not_board_pass(self):
        path = self.root / "constant.csv"
        path.write_text("Sample,signal\nRadix,HEX\n0,1\n1,1\n")
        x = analyze_ila_csv(path)
        self.assertEqual(x["verdict"], "INCONCLUSIVE")
        self.assertEqual(x["columns"]["signal"]["changes"], 0)

    def test_truncated_ila_csv_rejected(self):
        path = self.root / "truncated.csv"
        path.write_text("Sample,signal\n0\n")
        with self.assertRaises(DebugError):
            analyze_ila_csv(path)

    def test_hardware_disabled_before_any_process(self):
        with patch("vivado_debug.backend.subprocess.Popen") as popen:
            with self.assertRaises(DebugError):
                VivadoBackend(self.root, {}, {}, "absent.ltx")
            popen.assert_not_called()
        with patch("vivado_debug.sessions.subprocess.Popen") as popen:
            with self.assertRaises(DebugError):
                SessionClient(self.root).open(backend="vivado")
            popen.assert_not_called()

    def test_resource_lock_is_exclusive_and_recovers(self):
        path = self.root / "board.lock"
        with FileLock(path):
            with self.assertRaises(DebugError), FileLock(path):
                pass
        with FileLock(path):
            self.assertTrue(path.is_file())

    def test_offline_client_cannot_control_preexisting_hardware_session(self):
        client = SessionClient(self.root)
        sid = "vdbg-existing-hardware"
        directory = client.sessions / sid
        directory.mkdir(parents=True)
        atomic_json(directory / "state.json", {"session_id": sid, "state": "ready", "source_kind": "hardware", "pid": None})
        with self.assertRaises(DebugError) as error:
            client.submit(sid, "hw_status")
        self.assertEqual(error.exception.reason, "hardware_disabled")
        self.assertFalse((self.root / ".fpga/jobs").exists())

    def test_explicit_receiver_not_stale_server_startup_chat(self):
        receiver = "01a1aed2-f68e-74e1-a756-3e4401383f9f"
        with patch.dict(os.environ, {"CODEX_APP_TOOLS_PIPE_PATH": "\\\\.\\pipe\\test", "CODEX_THREAD_ID": "stale"}), \
                patch("vivado_debug.sessions.desktop.request", return_value={"hostId": "local"}) as request:
            x = notification_context(receiver)
        self.assertEqual(x["thread_id"], receiver)
        self.assertEqual(request.call_args.args[0]["caller_thread_id"], receiver)

    def test_pcap_export_preserves_bytes_and_rejects_overwrite(self):
        path = self.root / "mac.csv"
        control = (3 << 14) | (0xff << 16) | (1 << 12)
        path.write_text(f"sample,probe0,probe2,probe6\nRadix,HEX,HEX,HEX\n"
                        f"0,0807060504030201,{control:x},1\n1,100f0e0d0c0b0a09,{control | (1 << 13):x},2\n")
        output = self.root / "mac.pcap"
        result = export_mac_pcap(path, output, starts_at_frame_boundary=True)
        self.assertEqual(output.read_bytes()[-16:], bytes(range(1, 17)))
        self.assertFalse(result["summary"]["board_result_update"])
        with self.assertRaises(DebugError):
            export_mac_pcap(path, output, starts_at_frame_boundary=True)


class DebugModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vivado-debug-model-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile, self.machine = load_profile(), mock_machine()
        self.backend = MockBackend(self.root, self.profile, self.machine)
        self.service = DebugService(self.backend, self.profile, self.machine)
        self.job = Job(self.root / "debug.json", "debug_test")
        self.service.job = self.job

    def test_batch_validation_occurs_before_writes(self):
        with patch.object(self.backend, "apply") as apply:
            with self.assertRaises(DebugError):
                self.service.apply("A", "traffic", {"out0": "1", "out8": "2"})
            apply.assert_not_called()

    def test_live_revalidation_failure_prevents_writes_but_allows_close(self):
        with patch.object(self.backend, "begin_operation", create=True,
                          side_effect=DebugError("device_lost", "Device changed")) as begin, \
                patch.object(self.backend, "apply") as apply:
            with self.assertRaises(DebugError):
                self.service.execute("vio_apply", {"endpoint": "A", "core": "traffic", "writes": {"out14": "1"}}, self.job)
            apply.assert_not_called()
            result = self.service.execute("close", {}, self.job)
            self.assertTrue(result["closed"])
            begin.assert_called_once()

    def test_snapshot_revalidates_once_before_complete_page_transaction(self):
        with patch.object(self.backend, "begin_operation", create=True) as begin:
            result = self.service.execute("snapshot", {"endpoint": "A"}, self.job)
            self.assertEqual(len(result["values"]), 32)
            begin.assert_called_once()

    def test_48_and_64_bit_readback(self):
        self.service.apply("A", "traffic", {"out0": "0xfffffffffffe", "out11": "18446744073709551615"})
        self.assertEqual(self.service.read("A", "traffic", "out0")["decimal"], "281474976710654")
        self.assertEqual(self.service.read("A", "traffic", "out11")["decimal"], "18446744073709551615")

    def test_no_replay_after_uncertain_commit(self):
        self.backend.scenario = "write_timeout"
        with self.assertRaises(DebugError):
            self.service.apply("A", "traffic", {"out0": "1"})
        with patch.object(self.backend, "apply") as apply:
            with self.assertRaises(DebugError):
                self.service.apply("A", "traffic", {"out0": "1"})
            apply.assert_not_called()

    def test_input_probe_write_rejected(self):
        with self.assertRaises(DebugError):
            self.service.apply("A", "traffic", {"in0": "0"})

    def test_timeout_does_not_claim_stop_without_backend_confirmation(self):
        self.backend.scenario = "ila_timeout"
        with patch.object(self.backend, "stop_ila", return_value={"stopped": False}):
            with self.assertRaises(DebugError) as error:
                self.service.capture({"endpoint": "A", "core": "status", "timeout": .01})
        self.assertEqual(error.exception.reason, "capture_timeout")
        self.assertTrue(self.service.last_cleanup["ila"].startswith("unconfirmed:"))

    def test_trigger_and_depth_validation_before_arm(self):
        with patch.object(self.backend, "arm") as arm:
            for params in ({"depth": 17}, {"triggers": {"in0": "eq1'b1"}, "immediate": False}):
                with self.assertRaises(DebugError):
                    self.service.capture({"endpoint": "A", "core": "status", **params})
            arm.assert_not_called()

    def test_failed_staging_poison_prevents_later_commit(self):
        backend = object.__new__(VivadoBackend)
        backend.profile = self.profile
        backend.poisoned = False
        with patch.object(backend, "call", side_effect=DebugError("probe_invalid", "partial stage failed")) as call:
            with self.assertRaises(DebugError) as error:
                backend.apply("A", "traffic", {"out0": 1, "out1": 2})
        self.assertTrue(backend.poisoned)
        self.assertTrue(error.exception.uncertain)
        self.assertEqual(call.call_count, 1)

    def test_generic_profile_is_independent_of_udp_plugin(self):
        profile = {"cores": {"generic": {"kind": "vio", "cell": "generic", "ports": {"out0": 96, "in0": 1}}}}
        backend = MockBackend(self.root, profile, self.machine)
        service = DebugService(backend, profile, self.machine)
        service.job = self.job
        value = str((1 << 95) + 5)
        service.apply("A", "generic", {"out0": value})
        self.assertEqual(service.read("A", "generic", "out0")["decimal"], value)
        self.assertEqual(service.read("A", "generic", "in0")["decimal"], "0")


class DebugWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vivado-debug-worker-")
        self.root = Path(self.temp.name)
        self.client = SessionClient(self.root)
        self.ids = []

    def tearDown(self):
        for sid in self.ids:
            try:
                state = self.client.get(sid)
                if state.get("job_id"):
                    self.client.cancel(state["job_id"])
                    self.client.wait(state["job_id"], 15)
                if self.client.get(sid)["state"] in ("ready", "busy"):
                    submitted = self.client.submit(sid, "close")
                    self.client.wait(submitted["job_id"], 15)
                until = time.monotonic() + 5
                while process_identity(state.get("pid")) and time.monotonic() < until:
                    time.sleep(0.03)
            except Exception:
                pass
        self.temp.cleanup()

    def open(self, scenario="normal"):
        state = self.client.open(mock_scenario=scenario, idle_seconds=20)
        self.assertEqual(state["state"], "ready")
        self.ids.append(state["session_id"])
        return state["session_id"]

    def run_job(self, sid, operation, params):
        submitted = self.client.submit(sid, operation, params, timeout=15)
        return self.client.wait(submitted["job_id"], 20)

    def test_fixed_and_continuous_duplex_are_simulated(self):
        sid = self.open()
        for params in ({"packet_limit": "100", "timeout": 8},
                       {"packet_limit": "0", "duration_seconds": .08, "timeout": 8}):
            result = self.run_job(sid, "recipe", params)
            self.assertEqual(result["execution"], "completed", result)
            self.assertEqual(result["debug"]["verdict"], "PASS")
            self.assertEqual(result["debug"]["source_kind"], "simulated")
            event = read_json(Path(result["paths"]["job"]) / "completion-event.json")
            self.assertEqual(event["event_id"], result["job_id"] + ":terminal")

    def test_tail_loss_fails_without_sequence_gap(self):
        result = self.run_job(self.open("drop_tail"), "recipe", {"packet_limit": "100", "timeout": 8})
        self.assertEqual(result["execution"], "completed")
        self.assertEqual(result["debug"]["verdict"], "FAIL")
        self.assertEqual(result["debug"]["totals"]["A"]["sequence_gaps"], "0")

    def test_ila_capture_retains_raw_and_csv(self):
        result = self.run_job(self.open(), "ila_capture", {"endpoint": "A", "core": "status", "depth": 16})
        self.assertEqual(result["execution"], "completed", result)
        self.assertTrue(result["debug"]["capture_complete"])
        self.assertEqual(result["debug"]["analysis"]["rows"], 16)
        self.assertTrue(Path(result["debug"]["files"]["ila"]["path"]).is_file())

    def test_cancel_capture_and_busy_rejection(self):
        sid = self.open("ila_timeout")
        submitted = self.client.submit(sid, "ila_capture", {"endpoint": "A", "core": "status", "timeout": 10})
        with self.assertRaises(DebugError):
            self.client.submit(sid, "hw_status")
        self.client.cancel(submitted["job_id"])
        result = self.client.wait(submitted["job_id"], 10)
        self.assertEqual(result["reason"], "cancelled")
        self.assertEqual(self.client.job_get(submitted["job_id"])["state"], "cancelled")

    def test_not_ready_timeout_stops_without_false_pass(self):
        result = self.run_job(self.open("not_ready"), "recipe", {"packet_limit": "100", "timeout": .15})
        self.assertEqual(result["execution"], "failed")
        self.assertEqual(result["debug"]["verdict"], "INCONCLUSIVE")

    def test_recipe_cancellation_confirms_source_stop(self):
        sid = self.open()
        job = self.client.submit(sid, "recipe", {"packet_limit": "0", "duration_seconds": 5, "timeout": 10})
        path = Path(job["paths"]["job"]) / "snapshots.tsv"
        until = time.monotonic() + 5
        while not path.exists() and time.monotonic() < until:
            time.sleep(.02)
        self.assertTrue(path.exists())
        self.client.cancel(job["job_id"])
        result = self.client.wait(job["job_id"], 10)
        self.assertEqual(result["reason"], "cancelled")
        self.assertEqual(set(result["debug"]["cleanup"].values()), {"tx_idle_confirmed"})

    def test_stop_unconfirmed_is_preserved_as_invalid(self):
        result = self.run_job(self.open("stop_stuck"), "recipe", {
            "packet_limit": "0", "duration_seconds": .03, "timeout": 12})
        self.assertEqual(result["execution"], "failed")
        self.assertEqual(result["reason"], "stop_unconfirmed")
        self.assertEqual(result["debug"]["verdict"], "INCONCLUSIVE")
        self.assertTrue(any("unconfirmed" in x for x in result["debug"]["cleanup"].values()))

    def test_session_id_path_traversal_rejected(self):
        with self.assertRaises(DebugError):
            self.client.get("../other")

    def test_submitter_restart_can_read_existing_session(self):
        sid = self.open()
        restarted = SessionClient(self.root)
        self.assertEqual(restarted.get(sid)["state"], "ready")
        result = self.run_job(sid, "hw_status", {})
        self.assertEqual(result["debug"]["source_kind"], "simulated")


if __name__ == "__main__":
    unittest.main()
