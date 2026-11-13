import argparse
import json
import sys
import time

from . import add_result, load_config, preview, resolve_delivery, run_round, status, sync
from . import feedback_status, process_pending, request_task_action, retry_feedback


def main(argv=None):
    parser = argparse.ArgumentParser(description='Independent task delivery, local preview and reminders.')
    parser.add_argument('mode', choices=['preview', 'sync', 'once', 'run', 'status', 'complete', 'reopen', 'cancel', 'snooze', 'resolve-delivery', 'attach-result', 'feedback-status', 'retry-feedback'])
    parser.add_argument('--config', default='config/messaging.toml')
    parser.add_argument('--task-id')
    parser.add_argument('--until', help='Snooze until an ISO timestamp with timezone offset')
    parser.add_argument('--delivery-id')
    parser.add_argument('--resolution', choices=['retry', 'delivered'])
    parser.add_argument('--file')
    parser.add_argument('--note', default='')
    parser.add_argument('--request-id')
    parser.add_argument('--event-id')
    parser.add_argument('--expected-revision', type=int)
    args = parser.parse_args(argv)
    try:
        while True:
            interval = 60
            try:
                config = load_config(args.config)
                interval = config['schedule']['interval_seconds']
                if args.mode in {'complete', 'reopen', 'cancel', 'snooze'}:
                    result = request_task_action(config, args.task_id, args.mode, until=args.until,
                        event_id=args.event_id, expected_revision=args.expected_revision,
                        actor={'platform': 'local', 'user_id': 'local-user', 'source': 'cli'})
                elif args.mode == 'feedback-status':
                    result = feedback_status(config)
                elif args.mode == 'retry-feedback':
                    result = retry_feedback(config, args.request_id)
                elif args.mode == 'resolve-delivery':
                    result = resolve_delivery(config, args.delivery_id, args.resolution)
                elif args.mode == 'attach-result':
                    result = add_result(config, args.task_id, args.file, note=args.note)
                else:
                    feedback = process_pending(config) if args.mode in {'once', 'run'} else None
                    result = {'preview': preview, 'sync': sync, 'status': status}.get(args.mode, run_round)(config)
                    if feedback is not None:
                        result['feedback'] = feedback
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
