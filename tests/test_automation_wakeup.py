import json
import socket
import threading
import time

from automation import SchedulerService, load_config, status
from automation.core import _connect
from automation.wakeup import WakeupListener, _path, notify


def configuration(tmp_path):
    path = tmp_path / 'automation.toml'
    path.write_text('''enabled=true
[storage]
state_file="automation.db"
[scheduler]
control_interval_seconds=60
[jobs.feedback]
enabled=true
interval_seconds=3600
[jobs.messaging]
enabled=true
interval_seconds=3600
''')
    return load_config(path)


def test_wakeup_authentication_scope_and_missing_listener(tmp_path):
    path = tmp_path / 'messaging.toml'
    received = threading.Event()
    assert not notify(path, 'feedback')
    listener = WakeupListener({str(path): {'feedback'}}, lambda name: received.set()).start()
    try:
        endpoint = json.loads(_path(path).read_text())
        assert not notify(path, 'processor')
        assert not notify(path, 'messaging')
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(json.dumps({'token': 'wrong', 'job': 'feedback'}).encode(), ('127.0.0.1', endpoint['port']))
        assert not received.wait(.1)
        assert notify(path, 'feedback')
        assert received.wait(1)
    finally:
        listener.close()
    assert not _path(path).exists()


def test_signals_do_not_bypass_disabled_retry_or_paused_jobs(tmp_path):
    config = configuration(tmp_path)
    service = SchedulerService(config, clock=lambda: 100)
    with _connect(config, write=True) as conn:
        conn.execute("UPDATE jobs SET state='retry',next_run=150 WHERE name='feedback'")
        conn.execute("UPDATE jobs SET state='paused',next_run=0 WHERE name='messaging'")
    service.request_wakeup('feedback')
    service.request_wakeup('messaging')
    service.request_wakeup('processor')
    jobs = {row['name']: row for row in status(config)['jobs']}
    assert jobs['feedback']['state'] == 'retry' and jobs['feedback']['next_run'] == 150
    assert jobs['messaging']['state'] == 'paused'
    assert jobs['processor']['wake_seq'] == 0
    config['jobs']['feedback']['enabled'] = False
    service.request_wakeup('feedback')
    assert {r['name']: r for r in status(config)['jobs']}['feedback']['wake_seq'] == 1


def test_real_scheduler_wakes_under_one_second_and_keeps_signal_during_work(tmp_path):
    config = configuration(tmp_path)
    first, release, followup = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def work():
        calls.append(time.monotonic())
        if len(calls) == 1:
            first.set()
            assert release.wait(3)
        else:
            followup.set()
        return {'applied': 1}
    with _connect(config, write=True) as conn:
        conn.execute('UPDATE jobs SET next_run=?', (time.time() + 3600,))
    service = SchedulerService(config, callbacks={'feedback': work, 'messaging': lambda: {}}).start()
    path = config['jobs']['feedback']['config']
    try:
        started = time.monotonic()
        assert notify(path, 'feedback') and first.wait(1)
        assert calls[0] - started < 1
        assert notify(path, 'feedback')
        # Wait for receipt of the second hint before releasing the first job.
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if next(r for r in status(config)['jobs'] if r['name'] == 'feedback')['wake_seq'] == 2:
                break
            time.sleep(.01)
        else:
            raise AssertionError('Second wakeup was not recorded')
        release.set()
        assert followup.wait(1)
        assert len(calls) == 2
    finally:
        release.set()
        service.stop()


def test_feedback_commit_precedes_wakeup(tmp_path, monkeypatch):
    from messaging import load_config as load_messaging
    from messaging.feedback import enqueue_feedback, feedback_status
    import automation.wakeup
    path = tmp_path / 'messaging.toml'
    path.write_text('[feedback]\nenabled=true\nstate_file="feedback.db"\n')
    config = load_messaging(path)
    observed = []
    monkeypatch.setattr(automation.wakeup, 'notify', lambda p, j: observed.append((j, feedback_status(config)['counts'])))
    enqueue_feedback(config, 'local', 'e1', 't1', 'complete', {'platform': 'local', 'user_id': 'u1', 'source': 'test'})
    assert observed == [('feedback', {'queued': 1})]
