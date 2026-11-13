import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import pymupdf
from docx import Document
from PIL import Image

from providers.attachments import AttachmentContext


def context(tmp_path, path, *, vision=False, settings=None, attachment=None):
    config = {'storage': {'state_file': str(tmp_path/'state.db')}, 'model': {'vision': vision},
              'attachments': {'archive_roots': [str(tmp_path/'archive')], 'cache_dir': str(tmp_path/'cache'), **(settings or {})}}
    attachment = attachment or {'name': path.name, 'archive_path': str(path),
                                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    event = {'message_key': 'message-one', 'message': {'attachments': [attachment]}}
    return AttachmentContext([event], config)


def pdf(tmp_path, texts):
    folder = tmp_path/'archive'; folder.mkdir(exist_ok=True)
    path = folder/'document.pdf'
    with pymupdf.open() as document:
        for text in texts:
            page = document.new_page()
            if text:
                page.insert_text((72, 72), text)
        document.save(path)
    return path


def test_catalog_is_index_only_until_model_explicitly_reads(tmp_path, monkeypatch):
    from providers import attachments
    path = pdf(tmp_path, ['Private document body'])
    def forbidden(*args):
        raise AssertionError('catalog must not invoke a content parser')
    monkeypatch.setattr(attachments, '_run_worker', forbidden)
    catalog = context(tmp_path, path).catalog()
    assert catalog[0]['content_status'] == 'unread'
    assert catalog[0]['total_units'] is None
    assert 'Private document body' not in json.dumps(catalog)
    assert str(path) not in json.dumps(catalog)


def test_pdf_all_pages_are_addressable_and_coverage_is_explicit(tmp_path):
    path = pdf(tmp_path, ['First notice', 'Second owner', 'Third location', 'Final deadline Friday'])
    reader = context(tmp_path, path)
    assert reader.catalog()[0]['complete_document'] is False
    first = reader.read_attachment('f1')
    assert first['details']['selected_units'] == [1, 2, 3]
    assert first['details']['remaining_units'] == [4]
    assert first['details']['complete_document'] is False
    final = reader.read_attachment('f1', pages='4')
    assert 'Final deadline Friday' in json.dumps(final)
    assert final['details']['complete_document'] is True
    assert reader.evidence['f1:p4']['origins'] == [{'message_key': 'message-one', 'attachment_index': 0}]
    assert str(path) not in json.dumps(final)
    assert 'message-one' in reader.evidence['f1:p4']['message_keys']


def test_search_finds_later_page_and_cache_is_reused(tmp_path, monkeypatch):
    from providers import attachments
    path = pdf(tmp_path, ['one', 'two', 'three', 'deadline Friday'])
    reader = context(tmp_path, path)
    found = reader.read_attachment('f1', query='deadline')
    assert found['details']['selected_units'] == [4]
    assert 'f1:p4' in reader.evidence
    def forbidden(*args):
        raise AssertionError('parsed text should be cached')
    monkeypatch.setattr(attachments, '_run_worker', forbidden)
    again = context(tmp_path, path)
    assert again.read_attachment('f1', pages='2')['details']['status'] == 'ok'


def test_docx_paragraphs_and_tables_keep_document_order(tmp_path):
    folder = tmp_path/'archive'; folder.mkdir()
    path = folder/'ordered.docx'
    doc = Document(); doc.add_paragraph('Before table')
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = 'Person'; table.cell(0, 1).text = 'Friday'
    doc.add_paragraph('After table'); doc.save(path)
    result = context(tmp_path, path).read_attachment('f1', pages='1-3')
    assert result['details']['unit'] == 'block'
    texts = [block['text'] for block in result['content'] if block['type'] == 'text'][1:]
    assert 'Before table' in texts[0]
    assert 'Person | Friday' in texts[1]
    assert 'After table' in texts[2]
    assert result['details']['complete_document'] is True


def test_inline_docx_image_is_registered_as_visual_block(tmp_path):
    folder = tmp_path/'archive'; folder.mkdir()
    image = folder/'inline.png'; Image.new('RGB', (20,20), 'blue').save(image)
    path = folder/'image.docx'
    doc = Document(); doc.add_paragraph('Before'); doc.add_picture(str(image)); doc.add_paragraph('After'); doc.save(path)
    reader = context(tmp_path, path, vision=True)
    result = reader.read_attachment('f1', pages='1-3')
    assert any(block['type'] == 'image' for block in result['content'])
    assert reader.evidence['f1:b2']['coverage'] == 'image'
    assert result['details']['complete_document'] is True


@pytest.mark.parametrize('vision', [False, True])
def test_image_without_ocr_is_visual_evidence_or_explicit_review(tmp_path, vision):
    folder = tmp_path/'archive'; folder.mkdir()
    path = folder/'image.png'; Image.new('RGB', (32, 20), 'red').save(path)
    reader = context(tmp_path, path, vision=vision)
    result = reader.read_attachment('f1')
    images = [block for block in result['content'] if block['type'] == 'image']
    assert bool(images) == vision
    assert result['details']['complete_document'] == vision
    assert bool(reader.evidence) == vision
    assert bool(result['details']['evidence']) == vision
    if vision:
        assert base64.b64decode(images[0]['data']).startswith(b'\x89PNG')
    else:
        assert result['details']['status'] == 'needs_review'
        assert 'visual_content_requires_vision_model_or_local_ocr' in result['details']['warnings']


@pytest.mark.parametrize('vision', [False, True])
def test_scanned_pdf_does_not_silently_become_no_content(tmp_path, vision):
    path = pdf(tmp_path, [''])
    result = context(tmp_path, path, vision=vision).read_attachment('f1')
    assert result['details']['status'] == ('ok' if vision else 'needs_review')
    assert bool(result['details']['evidence']) == vision
    assert result['details']['complete_document'] == vision


def test_unregistered_paths_roots_hash_and_missing_archive_are_blocked(tmp_path):
    outside = tmp_path/'private.pdf'; outside.write_bytes(b'private')
    reader = context(tmp_path, outside)
    assert reader.catalog()[0]['warnings'] == ['attachment_outside_archive_roots']
    assert reader.read_attachment(str(outside))['details']['status'] == 'needs_review'
    assert not (tmp_path/'cache').exists()
    path = pdf(tmp_path, ['visible'])
    reader = context(tmp_path, path, attachment={'archive_path': str(path), 'sha256': '0'*64})
    assert reader.catalog()[0]['warnings'] == ['attachment_archive_hash_mismatch']
    reader = context(tmp_path, path, attachment={'local_path': str(path), 'name': 'unarchived'})
    assert reader.catalog()[0]['warnings'] == ['attachment_not_archived']


def test_file_page_and_pixel_limits_return_review_without_losing_message(tmp_path):
    path = pdf(tmp_path, ['a', 'b'])
    reader = context(tmp_path, path, settings={'max_file_bytes': 5})
    assert reader.catalog()[0]['warnings'] == ['attachment_file_size_limit']
    reader = context(tmp_path, path, settings={'max_pages': 1})
    assert reader.read_attachment('f1')['details']['warnings'] == ['document_page_limit']
    image = path.parent/'big.png'; Image.new('RGB', (20,20)).save(image)
    result = context(tmp_path, image, settings={'max_pixels': 100}).read_attachment('f1')
    assert result['details']['status'] == 'needs_review'
    assert result['details']['evidence'] == []


def test_duplicate_content_keeps_all_origin_indexes(tmp_path):
    path = pdf(tmp_path, ['same'])
    config = {'storage': {'state_file': str(tmp_path/'state.db')},
              'attachments': {'archive_roots': [str(path.parent)], 'cache_dir': str(tmp_path/'cache')}}
    attachment = {'archive_path': str(path)}
    reader = AttachmentContext([{'message_key':'a','message':{'attachments':[attachment,attachment]}},
                                {'message_key':'b','message':{'attachments':[attachment]}}], config)
    assert len(reader.catalog()) == 1
    reader.read_attachment('f1')
    assert reader.evidence['f1:p1']['origins'] == [
        {'message_key':'a','attachment_index':0}, {'message_key':'a','attachment_index':1},
        {'message_key':'b','attachment_index':0}]


def test_identical_content_with_different_names_keeps_refs_and_shares_parse_cache(tmp_path, monkeypatch):
    from providers import attachments
    path = pdf(tmp_path, ['same contents'])
    config = {'storage': {'state_file': str(tmp_path/'state.db')},
              'attachments': {'archive_roots': [str(path.parent)], 'cache_dir': str(tmp_path/'cache')}}
    reader = AttachmentContext([{'message_key': 'a', 'message': {'attachments': [
        {'name': 'original.pdf', 'archive_path': str(path)},
        {'name': 'renamed.pdf', 'archive_path': str(path)}]}}], config)
    assert [(item['ref'], item['name']) for item in reader.catalog()] == [
        ('f1', 'original.pdf'), ('f2', 'renamed.pdf')]
    assert reader.read_attachment('f1')['details']['status'] == 'ok'
    monkeypatch.setattr(attachments, '_run_worker', lambda *args: pytest.fail('parse cache was not reused'))
    assert reader.read_attachment('f2')['details']['status'] == 'ok'
    assert reader.evidence['f2:p1']['origins'] == [{'message_key': 'a', 'attachment_index': 1}]
    assert reader.export_attachment('f2')['name'] == 'renamed.pdf'


def test_invalid_page_or_unknown_ref_does_not_read_other_files(tmp_path):
    path = pdf(tmp_path, ['text'])
    reader = context(tmp_path, path)
    for pages in ['0', '2', '1-999999999', '../private', '1; SELECT']:
        assert reader.read_attachment('f1', pages=pages)['details']['status'] == 'needs_review'
    assert reader.read_attachment(['bad'])['details']['status'] == 'needs_review'
    assert reader.evidence == {}


def test_legacy_doc_uses_installed_libreoffice(tmp_path):
    converter = shutil.which('soffice') or 'C:/Program Files/LibreOffice/program/soffice.com'
    if not Path(converter).is_file():
        pytest.skip('LibreOffice is not installed')
    folder = tmp_path/'archive'; folder.mkdir()
    docx = folder/'legacy.docx'; document = Document(); document.add_paragraph('Legacy deadline Friday'); document.save(docx)
    subprocess.run([converter, '-env:UserInstallation='+(tmp_path/'fixture-profile').as_uri(), '--headless',
        '--convert-to', 'doc:MS Word 97', '--outdir', str(folder), str(docx)], check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    path = folder/'legacy.doc'
    result = context(tmp_path, path).read_attachment('f1')
    assert 'Legacy deadline Friday' in json.dumps(result)
    assert result['details']['complete_document'] is True


@pytest.mark.parametrize('budget', ['time', 'memory'])
def test_worker_budget_kills_only_its_process_tree(tmp_path, monkeypatch, budget):
    from types import SimpleNamespace
    from providers import attachments
    killed = []
    class Process:
        pid = 99999
        running = True
        def poll(self): return None if self.running else -1
        def kill(self): self.running = False; killed.append('worker')
        def wait(self, timeout): return -1
    process = Process()
    child = SimpleNamespace(kill=lambda: killed.append('child'), is_running=lambda: True,
                            memory_info=lambda: SimpleNamespace(rss=2*1024*1024))
    monitored = SimpleNamespace(children=lambda recursive: [child], is_running=lambda: True,
                                memory_info=lambda: SimpleNamespace(rss=2*1024*1024))
    monkeypatch.setattr(attachments.subprocess, 'Popen', lambda *args, **kwargs: process)
    monkeypatch.setattr(attachments.psutil, 'Process', lambda pid: monitored)
    ticks = iter([0, 2] if budget == 'time' else [0, 0])
    monkeypatch.setattr(attachments.time, 'monotonic', lambda: next(ticks))
    limits = dict(attachments.DEFAULTS, timeout_seconds=1, max_memory_mb=1)
    with pytest.raises(ValueError, match='timeout' if budget == 'time' else 'memory_limit'):
        attachments._run_worker(tmp_path, {}, limits)
    assert killed == ['child', 'worker']
