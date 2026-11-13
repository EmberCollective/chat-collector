"""Public, batch-bound content access; providers own preparation and parsing."""
from .attachments import AttachmentContext, _material_failure, _verified_archive


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
