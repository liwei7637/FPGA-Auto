"""Opt-in real desktop wakeup test; simulated build, actual Python delivery.

Run --launch --rollout <current task's jsonl>, then END the model turn.
The normal detached worker waits locally for that exact turn to end, runs a fixture,
and sends ONE terminal event. It never calls a model while waiting.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import uuid
from datetime import datetime
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fpga_auto import background
from fpga_auto.jobs import atomic_json, now, read_json
from fpga_auto.portable import ensure_config
from test_discovery import write_xpr


def launch(rollout, *, output_root=None, stage_delay=8, bitstream=False):
    thread = os.environ['CODEX_THREAD_ID']
    turn = None
    with rollout.open(encoding='utf-8') as source:
        first = json.loads(source.readline())
        if first['payload']['id'] != thread:
            raise ValueError('Rollout must belong to this exact desktop task')
        for line in source:
            item = json.loads(line)
            payload = item.get('payload', {})
            if item.get('type') == 'event_msg':
                if payload.get('type') == 'task_started':
                    turn = payload['turn_id']
                elif payload.get('type') in ('task_complete', 'turn_aborted') and payload.get('turn_id') == turn:
                    turn = None
        offset = source.tell()
    if not turn:
        raise ValueError('Launch from an active model turn, then end that turn')
    base = output_root or Path(__file__).resolve().parents[2] / '.fpga' / 'wakeup-verification'
    root = Path(base) / uuid.uuid4().hex[:12]
    root.mkdir(parents=True)
    evidence = root / 'verification.json'
    atomic_json(evidence, dict(status='waiting_for_origin_turn_end', thread_id=thread, origin_turn_id=turn,
                              rollout=str(rollout), offset=offset, created_at=now(), simulated_build=True))
    project = write_xpr(root, 'fixture.xpr')
    config, _ = ensure_config(root, root / 'fpga.json', str(project))
    atomic_json(root / 'control.json', {'delay': stage_delay, 'wakeup_verification': str(evidence)})
    fixture = Path(__file__).parent / ('fake_bit_vivado.py' if bitstream else 'fake_vivado.py')
    executable = root / 'vivado.bat'
    executable.write_text('@echo off\nchcp 65001 >nul\n"{}" -B -X utf8 "{}" %*\n'.format(sys.executable, fixture), encoding='utf-8')
    # Launch exactly as a real stage: one worker breaks away from the terminal job.
    # A second breakaway from an already detached verifier can hit the app's outer
    # non-breakaway Job Object (WinError 5); never weaken that process boundary.
    submission = background.start(root, config, 'build' if bitstream else 'synth', executable, 960, thread,
                                  verification_path=evidence, bitstream=bitstream)
    record = read_json(evidence)
    record['submission'] = submission
    if submission['execution'] != 'submitted':
        record.update(status='failed', error=submission.get('message'))
    atomic_json(evidence, record)
    if submission['execution'] != 'submitted':
        raise RuntimeError(str(submission))
    return dict(execution='submitted', verification=str(evidence), worker_pid=submission['worker_pid'],
                worker_ready=submission['worker_ready'], job_id=submission['job_id'],
                origin_turn_id=turn, agent_action='end_turn', simulated_build=True)


def wait_for_turn_end(record):
    deadline = time.monotonic() + 900
    pending = ''
    with open(record['rollout'], encoding='utf-8') as source:
        source.seek(record['offset'])
        while time.monotonic() < deadline:
            pending += source.read()
            lines = pending.split('\n')
            pending = lines.pop()
            for line in lines:
                item = json.loads(line)
                payload = item.get('payload', {})
                if item.get('type') == 'event_msg' and payload.get('turn_id') == record['origin_turn_id']:
                    if payload.get('type') == 'turn_aborted':
                        raise RuntimeError('Origin turn was stopped; test will not send a continuation')
                    if payload.get('type') == 'task_complete':
                        return item['timestamp']
            time.sleep(.2)
    raise TimeoutError('Origin turn did not complete within 15 minutes')


def begin_test_stage(evidence):
    """Called only by the simulated Vivado fixture inside the normal worker tree."""
    record = read_json(evidence)
    try:
        record['origin_completed_at'] = wait_for_turn_end(record)
        time.sleep(1)
        # Re-read after waiting: the launcher has since persisted the submission.
        completed = record['origin_completed_at']
        record = read_json(evidence)
        record.update(status='test_stage_started', origin_completed_at=completed, stage_started_at=now())
        atomic_json(evidence, record)
    except Exception as exc:
        record.update(status='failed', error=str(exc), finished_at=now())
        atomic_json(evidence, record)
        raise


def verify(evidence):
    record = read_json(evidence)
    directory = Path(record['submission']['paths']['job'])
    ack = read_json(directory / 'codex-received.json')
    result = read_json(directory / 'result.json')
    notice = read_json(directory / 'notification.json')
    if ack['thread_id'] != record['thread_id'] or os.environ.get('CODEX_THREAD_ID') != record['thread_id']:
        raise ValueError('Verification must run in the original addressed task')
    if ack['event_id'] != result['job_id'] + ':terminal' or notice['status'] != 'queued':
        raise ValueError('Event or delivery acknowledgement mismatch')
    parse_time = lambda value: datetime.fromisoformat(value.replace('Z', '+00:00'))
    origin_end = parse_time(record['origin_completed_at'])
    stage_start = parse_time(record['stage_started_at'])
    stage_end = parse_time(result['end_time'])
    if not origin_end < stage_start <= stage_end:
        raise ValueError('Test stage did not run after the origin turn ended')
    resumed = []
    during_stage = []
    triggers = []
    with open(record['rollout'], encoding='utf-8') as source:
        for line in source:
            item = json.loads(line)
            payload = item.get('payload', {})
            if item.get('type') == 'event_msg' and payload.get('type') == 'task_started':
                stamp = parse_time(item['timestamp'])
                if origin_end < stamp <= stage_end:
                    during_stage.append(payload['turn_id'])
                elif stamp > stage_end:
                    resumed.append(dict(turn_id=payload['turn_id'], started_at=item['timestamp']))
            if (item.get('type') == 'response_item' and payload.get('type') == 'function_call_output' and
                    payload.get('namespace') == 'codex_app' and payload.get('name') == 'send_message_to_thread'):
                try:
                    delegation = ET.fromstring(payload.get('output', ''))
                except ET.ParseError:
                    continue
                if (delegation.findtext('source_thread_id') == record['thread_id'] and
                        (delegation.findtext('input') or '').startswith('FPGA Auto completion event ' + ack['event_id'] + '.')):
                    triggers.append(dict(timestamp=item['timestamp'],
                                         turn_id=payload.get('internal_chat_message_metadata_passthrough', {}).get('turn_id')))
    if during_stage or not resumed or resumed[0]['turn_id'] == record['origin_turn_id']:
        raise ValueError('No clean idle interval followed by a new model turn: ' + str(during_stage))
    trigger = next((entry for entry in triggers if entry['turn_id'] == resumed[0]['turn_id']), None)
    if not trigger:
        raise ValueError('New turn has no matching desktop completion event; a manual message is not proof of wakeup')
    report = dict(verified=True, simulated_build=True, thread_id=record['thread_id'],
                  origin_turn_id=record['origin_turn_id'], origin_completed_at=record['origin_completed_at'],
                  stage_started_at=record['stage_started_at'], stage_completed_at=result['end_time'],
                  resumed_turn=resumed[0], model_turns_during_stage=len(during_stage),
                  event_id=ack['event_id'], receiver_ack=ack, transport=notice['transport'])
    report.update(trigger='codex_app.send_message_to_thread', trigger_received=trigger)
    atomic_json(evidence.parent / 'verified.json', report)
    record.update(status='verified', verified_at=now())
    atomic_json(evidence, record)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--launch', action='store_true')
    parser.add_argument('--rollout', type=Path)
    parser.add_argument('--verify', type=Path)
    parser.add_argument('--bit', action='store_true', help='Exercise build --bit using a synthetic bitstream fixture')
    args = parser.parse_args()
    if args.verify:
        print(json.dumps(verify(args.verify), ensure_ascii=False, indent=2))
    elif args.launch and args.rollout:
        print(json.dumps(launch(args.rollout, bitstream=args.bit), ensure_ascii=False, indent=2))
    else:
        parser.error('Use --launch --rollout PATH')
