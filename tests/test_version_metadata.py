import json
import re
from pathlib import Path

from common.version import project_metadata, project_version


def test_version_json_is_the_canonical_metadata():
    root = Path(__file__).resolve().parents[1]
    data = json.loads((root / 'version.json').read_text(encoding='utf-8'))
    assert project_metadata() == {'name': data['name'], 'version': data['version']}
    assert re.fullmatch(r'\d+\.\d+\.\d+', project_version())
    assert project_version() == '1.1.1'
