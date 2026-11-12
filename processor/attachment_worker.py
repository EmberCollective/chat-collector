"""Private isolated parser entry point. No network/model requests or macro execution."""
from __future__ import annotations

import json
import io
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import warnings
import zipfile


def detect_kind(raw, name):
    suffix = Path(name).suffix.lower()
    if raw.startswith(b'%PDF'):
        return 'pdf'
    if raw.startswith(bytes.fromhex('d0cf11e0a1b11ae1')):
        return 'xls' if suffix == '.xls' else 'ppt' if suffix == '.ppt' else 'doc'
    if raw.startswith(b'PK'):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                entries = archive.infolist()
                if len(entries) > 10000 or sum(e.file_size for e in entries) > 64 * 1024 * 1024:
                    raise ValueError('office_expansion_limit')
                names = set(archive.namelist())
                for member, kind in [('word/document.xml','docx'),('ppt/presentation.xml','pptx'),('xl/workbook.xml','xlsx')]:
                    if member in names:
                        return kind
        except zipfile.BadZipFile:
            pass
        raise ValueError('unsupported_archive_format')
    if suffix in {'.txt','.md','.csv','.json','.xml','.html','.htm','.rst','.log'}:
        return 'text'
    if raw.startswith((b'\x89PNG', b'\xff\xd8\xff', b'GIF8', b'BM', b'II*\x00', b'MM\x00*')) or raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
        return 'image'
    raise ValueError('unsupported_attachment_format')


def _image(path, limits, frame=None, destination=None):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = limits['max_pixels']
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(path) as image:
            if image.width * image.height > limits['max_pixels']:
                raise ValueError('image_pixel_limit')
            count = getattr(image, 'n_frames', 1)
            if count > limits['max_pages']:
                raise ValueError('image_frame_limit')
            if destination is not None:
                image.seek(frame or 0)
                frame_image = ImageOps.exif_transpose(image)
                frame_image.thumbnail((1600, 1600))
                frame_image.convert('RGB').save(destination, 'PNG')
            return count


def _doc_pdf(path, folder, limits):
    converted = folder / 'converted.pdf'
    if converted.is_file():
        return converted
    converter = shutil.which('soffice') or next((str(p) for p in [Path('C:/Program Files/LibreOffice/program/soffice.com'), Path('C:/Program Files/LibreOffice/program/soffice.exe')] if p.is_file()), None)
    if not converter:
        raise ValueError('legacy_doc_converter_unavailable')
    # LibreOffice still has Windows MAX_PATH-sensitive internals; hash caches can be deep.
    with tempfile.TemporaryDirectory(prefix='attachment-office-') as temporary:
        profile = Path(temporary) / 'profile'
        (profile / 'user').mkdir(parents=True)
        # A dedicated untrusted profile disables macros and automatic link updates.
        (profile / 'user/registrymodifications.xcu').write_text('''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item>
</oor:items>''', encoding='utf-8')
        local_input = Path(temporary) / ('input'+path.suffix)
        shutil.copyfile(path, local_input)
        result = subprocess.run([converter, '-env:UserInstallation=' + profile.as_uri(), '--headless', '--nologo',
            '--nodefault', '--nofirststartwizard', '--norestore', '--convert-to', 'pdf',
            '--outdir', temporary, str(local_input)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        candidate = Path(temporary) / 'input.pdf'
        if result.returncode or not candidate.is_file() or candidate.stat().st_size > limits['max_file_bytes']:
            raise ValueError('legacy_doc_conversion_failed')
        candidate.replace(converted)
    return converted


def inspect(path, kind, folder, limits):
    units, notes = [], []
    if kind == 'text':
        raw = path.read_bytes()
        if raw.startswith((b'\xff\xfe',b'\xfe\xff')):
            text = raw.decode('utf-16')
        else:
            try:
                text = raw.decode('utf-8-sig')
            except UnicodeDecodeError:
                text = raw.decode('gb18030')
        if '\x00' in text or len(text) > limits['max_text_chars']:
            raise ValueError('text_encoding_or_size_limit')
        return {'unit':'block','units':[{'index':i//10000+1,'text':text[i:i+10000],'visual':False}
                for i in range(0,len(text),10000)],'warnings':[]}
    if kind in {'pdf', 'doc', 'ppt', 'pptx', 'xls', 'xlsx'}:
        import pymupdf
        path = _doc_pdf(path, folder, limits) if kind != 'pdf' else path
        with pymupdf.open(path) as document:
            if document.needs_pass:
                raise ValueError('password_protected_pdf')
            if document.page_count > limits['max_pages']:
                raise ValueError('document_page_limit')
            total = 0
            for page in document:
                text = page.get_text('text')
                total += len(text)
                if total > limits['max_text_chars']:
                    raise ValueError('document_text_limit')
                units.append({'index': len(units)+1, 'text': text, 'visual': not text.strip() or bool(page.get_images())})
        return {'unit': 'page', 'units': units, 'warnings': notes}
    if kind == 'image':
        count = _image(path, limits)
        return {'unit': 'page', 'units': [{'index': i+1, 'text': '', 'visual': True} for i in range(count)], 'warnings': []}
    from docx import Document
    from docx.table import Table
    from docx.oxml.ns import qn
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 10000 or sum(x.file_size for x in entries) > 64 * 1024 * 1024:
            raise ValueError('docx_expansion_limit')
        if 'word/document.xml' not in archive.namelist():
            raise ValueError('unsupported_archive_not_docx')
        for name, warning in [('word/footnotes.xml', 'footnotes_not_extracted'), ('word/endnotes.xml', 'endnotes_not_extracted')]:
            if name in archive.namelist():
                notes.append(warning)
    document = Document(path)
    total = 0
    def add(text, image=None):
        nonlocal total
        total += len(text)
        if total > limits['max_text_chars'] or len(units) >= limits['max_blocks']:
            raise ValueError('document_text_or_block_limit')
        item = {'index': len(units)+1, 'text': text, 'visual': image is not None}
        if image is not None:
            image_path = folder / f"embedded-{item['index']}.bin"
            image_path.write_bytes(image)
            try:
                _image(image_path, limits)
                item['image'] = image_path.name
            except Exception:
                item['visual'] = False
                notes.append('embedded_image_unsupported')
        units.append(item)
    def table_text(table, depth=0):
        if depth > 10:
            raise ValueError('docx_table_depth_limit')
        return '\n'.join(' | '.join('\n'.join(table_text(child, depth+1) if isinstance(child, Table) else child.text
            for child in cell.iter_inner_content()) for cell in row.cells) for row in table.rows)
    parts = [document]
    # Headers/footers are additional source blocks, clearly labelled rather than omitted.
    seen = set()
    for section in document.sections:
        for part in (section.header, section.footer):
            if part.part.partname not in seen:
                seen.add(part.part.partname)
                parts.append(part)
    for part in parts:
        label = '' if part is document else '[页眉/页脚] '
        for child in part.iter_inner_content():
            text = table_text(child) if isinstance(child, Table) else child.text
            if text.strip():
                add(label + text)
            for blip in child._element.xpath('.//a:blip'):
                relation = blip.get(qn('r:embed'))
                image_part = part.part.related_parts.get(relation)
                if image_part:
                    add('', image_part.blob)
                else:
                    notes.append('external_image_not_loaded')
    return {'unit': 'block', 'units': units, 'warnings': sorted(set(notes))}


def render(path, kind, folder, limits, indices, document):
    images = {}
    if kind in {'pdf', 'doc', 'ppt', 'pptx', 'xls', 'xlsx'}:
        import pymupdf
        path = _doc_pdf(path, folder, limits) if kind != 'pdf' else path
        with pymupdf.open(path) as pdf:
            for index in indices:
                page = pdf[index-1]
                width, height = page.rect.width, page.rect.height
                if not all(math.isfinite(x) and x > 0 for x in (width, height)):
                    raise ValueError('invalid_pdf_page_dimensions')
                scale = min(1600/max(width, height), 2)
                if math.ceil(width*scale) * math.ceil(height*scale) > limits['max_pixels']:
                    raise ValueError('render_pixel_limit')
                target = folder / f'page-{index}.png'
                page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).save(target)
                images[index] = target.name
    else:
        for index in indices:
            unit = document['units'][index-1]
            source = folder / unit['image'] if kind == 'docx' else path
            if not source.resolve().is_relative_to(folder.resolve()):
                raise ValueError('cached_image_path_outside_root')
            target = folder / f'unit-{index}.png'
            _image(source, limits, index-1 if kind == 'image' else 0, target)
            images[index] = target.name
    return {'images': images}


if __name__ == '__main__':
    request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    try:
        args = (Path(request['path']), request['kind'], Path(request['cache']), request['limits'])
        result = inspect(*args) if request['operation'] == 'inspect' else render(*args, request['indices'], request['document'])
    except ValueError as exc:
        result = {'error': str(exc) if str(exc).replace('_', '').isalnum() else 'attachment_parse_failed'}
    except Exception:
        result = {'error': 'attachment_parse_failed'}
    Path(request['result']).write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
