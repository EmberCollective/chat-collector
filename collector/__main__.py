import argparse
import datetime
import importlib
import json
import logging
from pathlib import Path
import sys
import time

from .config import load_config
from .store import Store
from .assets import AttachmentArchive
from .filters import scan_filtered


class InstanceLock:
    def __init__(self, path):
        self.path = Path(str(path) + '.lock')

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open('a+b')
        if self.stream.tell() == 0:
            self.stream.write(b'0')
            self.stream.flush()
        self.stream.seek(0)
        try:
            if sys.platform == 'win32':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise RuntimeError('Another collector is using this state file') from None
        return self

    def __exit__(self, *args):
        self.stream.close()


def run_round(config, *, start=None, end=None):
    source = config['source']
    adapter = importlib.import_module('collector.adapters.' + config['platform'])
    storage = config['storage']
    with InstanceLock(storage['state_file']):
        store = Store(storage['state_file'], config['platform'], source['account_id'])
        try:
            recovered = store.export(storage['output_dir'])
            window_end = int(time.time()) if end is None else end
            previous = store.get('window_end')
            window_start = start if start is not None else (int(previous) if previous else window_end - config['schedule']['initial_lookback_seconds'])
            if window_start >= window_end:
                if start is not None:
                    raise ValueError('start must be before end')
                return {'platform': config['platform'], 'status': 'no_new_time_window', 'batches': recovered}
            refresh_interval = config['schedule'].get('refresh_interval_seconds', 0)
            if source.get('refresh', True) and start is None and end is None and previous:
                remaining = refresh_interval - (window_end - int(previous))
                if remaining > 0:
                    # Never advance the message window using a deliberately stale snapshot.
                    return {'platform': config['platform'], 'status': 'waiting_for_refresh',
                            'retry_after_seconds': remaining, 'batches': recovered}
            refresh_result = adapter.refresh(source) if source.get('refresh', True) else {'mode': 'decrypted_only'}
            messages = scan_filtered(config, window_start, window_end)
            archive = AttachmentArchive(config, config['platform'], source['account_id'])
            messages = (archive.enrich(message) for message in messages)
            result = store.ingest(messages, window_start, window_end,
                                  datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                  advance=start is None and end is None)
            result.update(platform=config['platform'], start=window_start, end=window_end,
                          intensity=archive.intensity, attachments_enabled=archive.enabled,
                          refresh=refresh_result, batches=recovered + store.export(storage['output_dir']))
            return result
        finally:
            store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local WeChat / QQ database collectors; no AI or UI.')
    parser.add_argument('mode', choices=['once', 'run'])
    parser.add_argument('--config', action='append', required=True)
    parser.add_argument('--start', type=int, help='Replay start, Unix seconds inclusive; does not move scheduled checkpoint')
    parser.add_argument('--end', type=int, help='Replay end, Unix seconds exclusive')
    args = parser.parse_args(argv)
    if (args.start is None) != (args.end is None) or (args.mode == 'run' and args.start is not None):
        parser.error('--start and --end must be used together with once')
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')
    # Retain the core's validation summary without printing every page's diagnostics.
    logging.getLogger('collector.vendor.wechat.decrypt').setLevel(logging.ERROR)
    due = {str(Path(path).resolve()): 0.0 for path in args.config}
    try:
        while True:
            failed = False
            for path in due:
                if due[path] > time.monotonic():
                    continue
                interval = 60
                try:
                    config = load_config(path)
                    interval = config['schedule']['interval_seconds']
                    result = run_round(config, start=args.start, end=args.end)
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                except Exception as exc:
                    failed = True
                    print(json.dumps({'config': path, 'status': 'failed', 'error_type': type(exc).__name__,
                                      'error': str(exc)}, ensure_ascii=False), file=sys.stderr, flush=True)
                due[path] = time.monotonic() + interval
            if args.mode == 'once':
                return 1 if failed else 0
            time.sleep(min(1.0, max(0.01, min(due.values()) - time.monotonic())))
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
