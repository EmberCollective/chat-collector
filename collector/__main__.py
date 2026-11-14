import argparse
import json
import logging
from pathlib import Path
import sys
import time

from .config import load_config
from application.collection import run_round  # Legacy composition entry point.


from common.locking import InstanceLock  # Legacy import path.


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
