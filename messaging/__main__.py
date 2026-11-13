import argparse
import json
import sys
import time

from . import add_result, load_config, preview, resolve_delivery, run_round, set_task_state, status, sync


def main(argv=None):
    parser = argparse.ArgumentParser(description='Independent task delivery, local preview and reminders.')
    parser.add_argument('mode', choices=['preview', 'sync', 'once', 'run', 'status', 'complete', 'reopen', 'snooze', 'resolve-delivery', 'attach-result'])
    parser.add_argument('--config', default='config/messaging.toml')
    parser.add_argument('--task-id')
    parser.add_argument('--until', help='Snooze until an ISO timestamp with timezone offset')
    parser.add_argument('--delivery-id')
    parser.add_argument('--resolution', choices=['retry', 'delivered'])
    parser.add_argument('--file')
    parser.add_argument('--note', default='')
    args = parser.parse_args(argv)
    try:
        while True:
            interval = 60
            try:
                config = load_config(args.config)
                interval = config['schedule']['interval_seconds']
                if args.mode in {'complete', 'reopen', 'snooze'}:
                    result = set_task_state(config, args.task_id, args.mode, until=args.until)
                elif args.mode == 'resolve-delivery':
                    result = resolve_delivery(config, args.delivery_id, args.resolution)
                elif args.mode == 'attach-result':
                    result = add_result(config, args.task_id, args.file, note=args.note)
                else:
                    result = {'preview': preview, 'sync': sync, 'status': status}.get(args.mode, run_round)(config)
                print(json.dumps(result, ensure_ascii=False), flush=True)
                code = 0
            except Exception as exc:
                print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr, flush=True)
                code = 1
            if args.mode != 'run':
                return code
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
