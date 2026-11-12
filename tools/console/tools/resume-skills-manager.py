"""Resume only the exact process paused during the terminal-flash investigation."""
import json
from pathlib import Path
import psutil

def resume(record):
    try:
        process = psutil.Process(record['pid'])
        if process.create_time() != record['created'] or process.exe() != record['exe']:
            raise SystemExit('Recorded process has changed; no action taken.')
        process.resume()
        print('Skills Manager resumed. Its background Git checks may show terminals again.')
    except psutil.NoSuchProcess:
        print('The paused process has already exited. Open Skills Manager normally if needed.')


if __name__ == '__main__':
    record_path = Path(__file__).resolve().parents[1]/'.runtime/paused-skills-manager.json'
    resume(json.loads(record_path.read_text(encoding='utf-8')))
