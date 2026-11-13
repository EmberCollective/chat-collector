"""Compatibility import; deterministic file handling lives in the provider layer."""
from providers.attachments import AttachmentContext

__all__ = ['AttachmentContext']
