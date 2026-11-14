"""Public, batch-bound content access; providers own preparation and parsing."""
from pathlib import Path

from .attachments import AttachmentContext, _material_failure, _verified_archive


def configure_content(settings, base):
    """Normalize provider registrations and local archive boundaries in place."""
    collectors = settings.setdefault('collector_configs', {})
    if not isinstance(collectors, dict) or set(collectors) - {'qq', 'wechat'} or any(not isinstance(p, str) or not p for p in collectors.values()):
        raise ValueError('attachments.collector_configs must map qq/wechat to local configuration files')
    settings['collector_configs'] = {k: str((Path(base) / p).resolve()) for k, p in collectors.items()}
    from .registry import configure_providers
    configure_providers(settings, base)
    roots = settings.setdefault('archive_roots', ['../data/assets'])
    if not isinstance(roots, list) or any(not isinstance(p, str) or not p for p in roots):
        raise ValueError('attachments.archive_roots must be an array of paths')
    settings['archive_roots'] = [str((Path(base) / p).resolve()) for p in roots]
    cache = settings.setdefault('cache_dir', '../data/processor/attachment-cache')
    if not isinstance(cache, str) or not cache:
        raise ValueError('attachments.cache_dir must be a path')
    settings['cache_dir'] = str((Path(base) / cache).resolve())
    if any(Path(root) == Path(settings['cache_dir']) or Path(root) in Path(settings['cache_dir']).parents for root in settings['archive_roots']):
        raise ValueError('Attachment cache must be outside original archives')


def create_attachment_context(events, config):
    return AttachmentContext(events, config)


def read_attachment(context, ref, *, pages=None, query=None):
    return context.read_attachment(ref, pages=pages, query=query)


def export_attachment(context, ref):
    return context.export_attachment(ref)


def export_material(attachment, config, *, event=None, attachment_index=None):
    """Revalidate a task-bound archive without parsing its document content."""
    try:
        return _verified_archive(attachment, config)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        if event is not None and (isinstance(exc, FileNotFoundError) or str(exc) == 'verified_archive_unavailable'):
            context = create_attachment_context([event], config)
            for item in context.catalog():
                if any(origin['attachment_index'] == attachment_index for origin in item['origins']):
                    return export_attachment(context, item['ref'])
        return _material_failure(attachment, exc)
