"""Canonical project metadata loaded from the repository root."""
from functools import lru_cache
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


@lru_cache
def project_metadata():
    data = json.loads((ROOT / 'version.json').read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('name'), str) or not data['name'].strip():
        raise ValueError('version.json name must be nonempty text')
    if not isinstance(data.get('version'), str) or not re.fullmatch(r'\d+\.\d+\.\d+', data['version']):
        raise ValueError('version.json version must be semantic x.y.z')
    return {'name': data['name'].strip(), 'version': data['version']}


def project_version():
    return project_metadata()['version']
