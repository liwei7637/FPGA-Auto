import json
import os
import struct
import unittest
from unittest.mock import patch

from fpga_auto import desktop
from fpga_auto.jobs import atomic_json, job_state, read_json
from fpga_auto import background
import test_background


class FragmentedPipe:
    def __init__(self, replies):
        self.incoming = bytearray()
        self.outgoing = bytearray()
        for reply in replies:
            data = json.dumps(reply).encode()
            self.incoming.extend(struct.pack('<I', len(data)) + data)

    def read(self, count):
        chunk = self.incoming[:min(count, 3)]
        del self.incoming[:len(chunk)]
        return bytes(chunk)

    def write(self, data):
        self.outgoing.extend(data[:7])
        return min(7, len(data))


class DesktopProtocolTests(unittest.TestCase):
    def test_absent_caller_host_is_resolved_by_desktop(self):
        client = desktop.PipeClient(None)
        response = {'success': True, 'contentItems': [{'type': 'inputText', 'text': '{}'}]}
        with patch.dict(os.environ, {}, clear=True), patch.object(client, 'rpc', return_value=response) as rpc:
            client.tool('read_thread', {'threadId': 'target'}, 'caller')
        self.assertNotIn('hostId', rpc.call_args.args[1])
        self.assertEqual(rpc.call_args.args[1]['threadId'], 'caller')

    def test_explicit_caller_host_is_preserved(self):
        client = desktop.PipeClient(None)
        response = {'success': True, 'contentItems': [{'type': 'inputText', 'text': '{}'}]}
        with patch.dict(os.environ, {'CODEX_APP_TOOLS_CALLER_HOST_ID': 'local'}, clear=True), \
                patch.object(client, 'rpc', return_value=response) as rpc:
            client.tool('read_thread', {'threadId': 'target'}, 'caller')
        self.assertEqual(rpc.call_args.args[1]['hostId'], 'local')

    def test_durable_target_is_rejected_before_sending(self):
        target = '01a0a902-a04f-7df3-b888-2ed5190d6852'
        catalog = {'tools': [
            dict(name=name, namespace='codex_app', inputSchema={'properties': props})
            for name, props in [('read_thread', {'threadId': {}}),
                                ('send_message_to_thread', {'threadId': {}, 'prompt': {}})]]}
        with patch('builtins.open'), patch.object(desktop.PipeClient, 'rpc', return_value=catalog), \
                patch.object(desktop.PipeClient, 'tool', return_value={
                    'thread': dict(id=target, kind='codex', hostId='durable')}) as tool:
            reply = desktop.bridge(dict(mode='send', pipe='\\\\.\\pipe\\test',
                caller_thread_id=target, thread_id=target, message='done'))
        self.assertFalse(reply['ok'])
        self.assertFalse(reply['uncertain'])
        self.assertIn('host=durable', reply['error'])
        self.assertEqual(tool.call_count, 1)
        self.assertNotIn('hostId', tool.call_args.args[1])

    def test_partial_reads_and_writes_preserve_unicode(self):
        pipe = FragmentedPipe([dict(jsonrpc='2.0', id=1, result={'text': '完成'})])
        client = desktop.PipeClient(pipe)
        self.assertEqual(client.rpc('tools/list', {'text': '阶段'}), {'text': '完成'})
        size = struct.unpack('<I', pipe.outgoing[:4])[0]
        self.assertEqual(size, len(pipe.outgoing) - 4)
        self.assertEqual(json.loads(pipe.outgoing[4:])['params']['text'], '阶段')

    def test_wrong_response_id_rejected(self):
        client = desktop.PipeClient(FragmentedPipe([dict(jsonrpc='2.0', id=99, result={})]))
        with self.assertRaisesRegex(ValueError, 'ID mismatch'):
            client.rpc('tools/list', {})

    def test_closed_pipe_is_not_success(self):
        with self.assertRaises(EOFError):
            desktop.PipeClient(FragmentedPipe([])).rpc('tools/list', {})

    def test_oversized_frame_rejected(self):
        pipe = FragmentedPipe([])
        pipe.incoming.extend(struct.pack('<I', desktop.MAX_FRAME + 1))
        with self.assertRaisesRegex(ValueError, 'frame limit'):
            desktop.PipeClient(pipe).rpc('tools/list', {})

    def test_missing_desktop_context_fails_without_daemon(self):
        with patch.dict(os.environ, {}, clear=True), patch('fpga_auto.desktop.subprocess.run') as run:
            with self.assertRaises(desktop.DesktopError):
                desktop.probe()
            run.assert_not_called()

    def test_pipe_error_after_send_is_uncertain(self):
        with patch('builtins.open') as opened:
            opened.return_value.__enter__.return_value = FragmentedPipe([])
            with patch.object(desktop.PipeClient, 'rpc') as rpc:
                rpc.return_value = {'tools': [
                    dict(name=name, namespace='codex_app', inputSchema={'properties': props})
                    for name, props in [('read_thread', {'threadId': {}}),
                                        ('send_message_to_thread', {'threadId': {}, 'prompt': {}})]]}
                target = '01a0a902-a04f-7df3-b888-2ed5190d6852'
                def call(client, name, arguments, caller):
                    if name == 'read_thread':
                        return {'thread': dict(id=target, kind='codex', hostId='local')}
                    client.send_attempted = True
                    raise EOFError('lost acknowledgement')
                # autospec keeps the client accessible for the mutation boundary.
                with patch.object(desktop.PipeClient, 'tool', autospec=True, side_effect=call):
                    reply = desktop.bridge(dict(mode='send', pipe='\\\\.\\pipe\\test',
                                                caller_thread_id=target, thread_id=target, message='done'))
                self.assertFalse(reply['ok'])
                self.assertTrue(reply['uncertain'])


class DesktopDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_background.BackgroundTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.job = self.fixture.notification_job()
        self.receipt_path = self.job.directory / 'notification.json'
        receipt = read_json(self.receipt_path)
        receipt.update(transport='desktop_app_tools', pipe='\\\\.\\pipe\\test',
                       caller_thread_id=self.fixture.thread_id)
        atomic_json(self.receipt_path, receipt)

    def test_desktop_delivery_uses_existing_task_once(self):
        with patch('fpga_auto.desktop.send', return_value={'threadId': self.fixture.thread_id}) as send, \
                patch('fpga_auto.background.subprocess.run') as cli:
            result = background.deliver(self.job.directory)
            background.deliver(self.job.directory)
            self.assertEqual(result['status'], 'queued')
            self.assertEqual(send.call_count, 1)
            cli.assert_not_called()
            self.assertIn('notify-ack', send.call_args.args[1])
            self.assertEqual(send.call_args.args[0]['thread_id'], self.fixture.thread_id)

    def test_uncertain_desktop_delivery_never_retries_automatically(self):
        with patch('fpga_auto.desktop.send', side_effect=desktop.DesktopError('timeout', True)) as send:
            self.assertEqual(background.deliver(self.job.directory)['status'], 'unknown')
            with self.assertRaisesRegex(Exception, 'Delivery may have succeeded'):
                background.deliver(self.job.directory)
            self.assertEqual(send.call_count, 1)

    def test_missing_pipe_preserves_build_success(self):
        with patch('fpga_auto.desktop.send', side_effect=desktop.DesktopError('app closed')):
            self.assertEqual(background.deliver(self.job.directory)['status'], 'failed')
        self.assertEqual(read_json(self.job.directory / 'result.json')['execution'], 'completed')

    def test_acknowledgement_survives_sender_receipt_race(self):
        def send(receipt, _message):
            with patch.dict(os.environ, {'CODEX_THREAD_ID': self.fixture.thread_id}):
                background.acknowledge(self.fixture.config['config_path'], self.job.id, self.job.id + ':terminal')
            return {'threadId': self.fixture.thread_id}
        with patch('fpga_auto.desktop.send', side_effect=send):
            background.deliver(self.job.directory)
        self.assertTrue(job_state(self.job.directory)['notification']['wake_verified'])

    def test_other_task_cannot_acknowledge(self):
        with patch.dict(os.environ, {'CODEX_THREAD_ID': 'different-task'}):
            with self.assertRaisesRegex(Exception, 'Only the addressed desktop task'):
                background.acknowledge(self.fixture.config['config_path'], self.job.id, self.job.id + ':terminal')


if __name__ == '__main__':
    unittest.main()
