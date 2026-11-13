"""Explicit query adapters; only application registration selects executable code."""
from contextlib import contextmanager
import importlib


class _LocalSnapshot:
    def __init__(self, scope, metadata):
        from .identities import IdentityContext
        self.config = scope['config']
        self.snapshot_id = metadata['snapshot_id']
        self.adapter = importlib.import_module('providers.platforms.' + scope['platform'])
        self.identities = IdentityContext(self.config)

    def scan(self, start, end, **kwargs):
        return self.adapter.scan(self.config['source'], start, end, **kwargs)

    def enrich(self, message):
        return self.identities.enrich(message)


@contextmanager
def _local_snapshot(scope):
    from collector.service import source_snapshot
    config = scope['config']
    if (scope['platform'] != config['platform']
            or str(scope['account_id']) != str(config['source']['account_id'])):
        raise ValueError('Query scope does not match its source configuration')
    with source_snapshot(config) as metadata:
        snapshot = _LocalSnapshot(scope, metadata)
        try:
            yield snapshot
        finally:
            snapshot.identities.close()


QUERY_PROVIDERS = {'wechat': _local_snapshot, 'qq': _local_snapshot}


def register_query_provider(provider_id, snapshot_factory):
    """Register a context manager yielding snapshot_id, scan(), and enrich()."""
    if not isinstance(provider_id, str) or not provider_id or not callable(snapshot_factory):
        raise ValueError('Query provider needs a nonempty id and a snapshot factory')
    if provider_id in QUERY_PROVIDERS:
        raise ValueError('Query provider already registered: ' + provider_id)
    QUERY_PROVIDERS[provider_id] = snapshot_factory


def query_snapshot(scope):
    try:
        factory = QUERY_PROVIDERS[scope['provider_id']]
    except (KeyError, TypeError) as exc:
        raise ValueError('Query provider is not registered') from exc
    return factory(scope)
