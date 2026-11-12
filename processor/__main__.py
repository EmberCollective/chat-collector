import argparse
import json
import sys
import time

from . import load_config, preview, run_round, status


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local Markdown-guided task processor; console is optional.')
    parser.add_argument('mode', choices=['once', 'run', 'preview', 'status'])
    parser.add_argument('--config', required=True)
    args = parser.parse_args(argv)
    try:
        while True:
            interval = 60
            try:
                config = load_config(args.config)
                interval = config['schedule']['interval_seconds']
                operation = {'preview': preview, 'status': status}.get(args.mode, run_round)
                print(json.dumps(operation(config), ensure_ascii=False), flush=True)
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
