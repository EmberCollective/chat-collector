"""CLI. Preview prints counts only; publishing requires explicit downstream opt-in."""
import argparse,importlib.util,json
from pathlib import Path
try:from .adapter import events_from_capture,publish
except ImportError:from adapter import events_from_capture,publish

def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',required=True,type=Path);parser.add_argument('--publish',action='store_true');parser.add_argument('--allow-downstream-processing',action='store_true');args=parser.parse_args()
 try:
  config=json.loads(args.config.read_text(encoding='utf-8-sig'));base=args.config.resolve().parent
  path=lambda key:(base/config[key]).resolve()
  events=events_from_capture(path('capture_db'),config['account_id'],config['source_ids'],config['start_inclusive'],config['end_exclusive'])
  if not args.publish:print(json.dumps({'mode':'preview','events':len(events),'published':0,'content_logged':False}));return
  if not args.allow_downstream_processing:raise ValueError('explicit_downstream_processing_consent_required')
  file=path('upstream_root')/'contracts.py'
  spec=importlib.util.spec_from_file_location('chat_collector_contracts',file);contracts=importlib.util.module_from_spec(spec);spec.loader.exec_module(contracts)
  count=publish(events,contracts,path('inbox'),path('ledger'))
  print(json.dumps({'mode':'publish','published':count,'content_logged':False}))
 except Exception as error:
  code=str(error) if isinstance(error,ValueError) and str(error).replace('_','').isalnum() else 'provider_operation_failed'
  parser.exit(1,code+'\n')
if __name__=='__main__':main()
