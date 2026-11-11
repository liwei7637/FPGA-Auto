"""Exercise the real E2E launcher without sending a message to any real task."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fpga_auto.jobs import now, read_json, atomic_json
from fpga_auto.runner import process_identity
from verify_desktop_wakeup import launch, verify


class WakeupHarnessTests(unittest.TestCase):
    def verification_fixture(self, root, with_trigger):
        thread = '00000000-0000-4000-8000-000000000001'
        job = root / 'job'
        job.mkdir()
        evidence = root / 'verification.json'
        rollout = root / 'test.jsonl'
        event = 'test:terminal'
        atomic_json(evidence, dict(thread_id=thread, origin_turn_id='origin',
                                  origin_completed_at='2026-09-16T08:00:00+00:00',
                                  stage_started_at='2026-09-16T08:00:01+00:00', rollout=str(rollout),
                                  submission={'paths': {'job': str(job)}}))
        atomic_json(job / 'codex-received.json', dict(thread_id=thread, event_id=event))
        atomic_json(job / 'result.json', dict(job_id='test', end_time='2026-09-16T08:00:09+00:00'))
        atomic_json(job / 'notification.json', dict(status='queued', transport='desktop_app_tools'))
        entries = [dict(timestamp='2026-09-16T08:00:10+00:00', type='event_msg',
                        payload=dict(type='task_started', turn_id='resumed'))]
        if with_trigger:
            entries.append(dict(timestamp='2026-09-16T08:00:10+00:00', type='response_item', payload=dict(
                type='function_call_output', namespace='codex_app', name='send_message_to_thread',
                output='<codex_delegation><source_thread_id>' + thread + '</source_thread_id><input>' +
                       'FPGA Auto completion event ' + event + '.</input></codex_delegation>',
                internal_chat_message_metadata_passthrough={'turn_id': 'resumed'})))
        rollout.write_text(''.join(json.dumps(x) + '\n' for x in entries), encoding='utf-8')
        return evidence, thread

    def test_manual_new_turn_is_not_accepted_as_automatic_wakeup(self):
        with tempfile.TemporaryDirectory() as temporary:
            evidence, thread = self.verification_fixture(Path(temporary), False)
            with patch.dict(os.environ, {'CODEX_THREAD_ID': thread}):
                with self.assertRaisesRegex(ValueError, 'manual message is not proof'):
                    verify(evidence)

    def test_matching_desktop_event_proves_automatic_trigger(self):
        with tempfile.TemporaryDirectory() as temporary:
            evidence, thread = self.verification_fixture(Path(temporary), True)
            with patch.dict(os.environ, {'CODEX_THREAD_ID': thread}):
                report = verify(evidence)
            self.assertTrue(report['verified'])
            self.assertEqual(report['trigger_received']['turn_id'], 'resumed')

    def test_single_worker_waits_for_turn_end_then_finishes(self):
        thread = '00000000-0000-4000-8000-000000000001'
        turn = '00000000-0000-4000-8000-000000000002'
        with tempfile.TemporaryDirectory(prefix='fpga wakeup harness ') as temporary:
            root = Path(temporary)
            rollout = root / 'test.jsonl'
            rollout.write_text('\n'.join(json.dumps(x) for x in [
                {'type': 'session_meta', 'payload': {'id': thread}},
                {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': turn}}
            ]) + '\n', encoding='utf-8')
            # The worker receives an intentionally nonexistent pipe and synthetic task.
            # No production desktop endpoint is ever used by this test's sender.
            probe = dict(transport='desktop_app_tools', pipe='\\\\.\\pipe\\fpga-nonexistent-test',
                         caller_thread_id=thread, thread_id=thread, host_id='local')
            with patch.dict(os.environ, {'CODEX_THREAD_ID': thread,
                                         'CODEX_APP_TOOLS_PIPE_PATH': probe['pipe']}), \
                    patch('fpga_auto.background.probe_notification', return_value=probe):
                submission = launch(rollout, output_root=root / 'evidence', stage_delay=.1)
            self.assertEqual(submission['execution'], 'submitted')
            evidence = Path(submission['verification'])
            record = read_json(evidence)
            directory = Path(record['submission']['paths']['job'])
            self.assertFalse((directory / 'completion-event.json').exists())
            with rollout.open('a', encoding='utf-8') as sink:
                sink.write(json.dumps(dict(timestamp=now(), type='event_msg',
                                          payload=dict(type='task_complete', turn_id=turn))) + '\n')
            deadline = time.monotonic() + 15
            while process_identity(submission['worker_pid']) and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertIsNone(process_identity(submission['worker_pid']))
            self.assertEqual(read_json(directory / 'result.json')['execution'], 'completed')
            self.assertEqual(read_json(directory / 'notification.json')['status'], 'failed')
            record = read_json(evidence)
            self.assertEqual(record['status'], 'test_stage_started')
            self.assertLess(record['origin_completed_at'], record['stage_started_at'])


if __name__ == '__main__':
    unittest.main()
