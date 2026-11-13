import argparse
import json
import sys

from . import SchedulerService, check, load_config, request_stop, resume, run_once, status


def main(argv=None):
    parser = argparse.ArgumentParser(description='Independent jobs, bounded retries and local incident records.')
    parser.add_argument('mode', choices=['run', 'check', 'status', 'resume', 'once', 'stop'])
    parser.add_argument('--config', default='config/automation.toml')
    parser.add_argument('--job', choices=['wechat', 'qq', 'processor', 'messaging', 'feedback', 'listener'])
    args = parser.parse_args(argv)
    if args.mode in {'once', 'resume'} and not args.job:
        parser.error('--job is required for once/resume')
    try:
        config = load_config(args.config)
        if args.mode == 'run':
            service = SchedulerService(config).start()
            try:
                while not service.stop_event.wait(1):
                    pass
            except KeyboardInterrupt:
                pass
            finally:
                service.stop()
            return 0
        if args.mode == 'once':
            result = run_once(config, args.job)
        elif args.mode == 'resume':
            result = resume(config, args.job)
        elif args.mode in {'status', 'stop'}:
            from . import control
            result = {'status': control.status, 'stop': control.stop}[args.mode](config)
        else:
            result = check(config)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 1 if result.get('state') in {'retry', 'paused'} else 0
    except Exception as exc:
        # SDK exceptions may contain remote URLs or credentials; operational detail stays in state.
        print(json.dumps({'status': 'failed', 'error': type(exc).__name__}), file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
