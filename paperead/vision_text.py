"""Selectively restore math-heavy paragraphs using their PDF images."""
from __future__ import annotations

import asyncio
import base64
import difflib
import json
import logging
import re

import fitz

from .formula import validate_latex

log = logging.getLogger(__name__)
PROMPT = '''Transcribe this paragraph exactly in reading order from the images. Preserve all words and punctuation; do not translate or explain. Return only JSON: {"text":"complete paragraph with readable formula text", "formulas":[{"source":"exact substring in text", "latex":"inline LaTeX without dollar signs"}]}. List formulas in order, including repeated occurrences. Images are consecutive parts of one paragraph.'''


def _candidate(block):
    if block.kind not in ('para', 'abstract') or len(block.text.strip()) < 30:
        return False
    math = block.inline_math or []
    return (len(math) >= 3 or any(m.get('fallback') for m in math)
            or len(re.findall(r'[∑∏∫√∂∇∞≤≥≠≈κλθεμσΩΘ→]', block.text)) >= 3
            or len(re.findall(r'[�\ufffd\ue000-\uf8ff]', block.text)) >= 2)


def _images(pdf, block):
    rects = block.bboxes or [(block.page, *block.bbox)]
    images = []
    with fitz.open(str(pdf)) as doc:
        for page_number, x0, y0, x1, y1 in rects:
            if not 1 <= int(page_number) <= len(doc):
                raise ValueError('invalid paragraph page')
            page = doc[int(page_number) - 1]
            rect = fitz.Rect(x0, y0, x1, y1) + (-3, -3, 3, 3)
            rect &= page.rect
            if rect.is_empty:
                raise ValueError('empty paragraph rectangle')
            images.append(page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect, alpha=False).tobytes('png'))
    return images


def _restore(raw, original):
    raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip(), flags=re.I)
    data = json.loads(raw)
    text, formulas = data['text'], data['formulas']
    if not isinstance(text, str) or not isinstance(formulas, list) or not formulas:
        raise ValueError('invalid response')
    # Compare word sequences, allowing mathematical notation to change but rejecting omissions.
    words = lambda s: ' '.join(re.findall(r'[^\W\d_]{2,}', s.casefold()))
    old, new = words(original), words(text)
    if not old or difflib.SequenceMatcher(None, old, new, autojunk=False).ratio() < 0.78:
        raise ValueError('transcription differs from source')
    if len(text) < len(original) * .65 or len(text) > len(original) * 1.6:
        raise ValueError('transcription length differs from source')
    spans, cursor = [], 0
    for item in formulas:
        source, latex = item['source'], item['latex']
        if not isinstance(source, str) or not source or not isinstance(latex, str) or not validate_latex(latex)[0]:
            raise ValueError('invalid formula')
        start = text.find(source, cursor)
        if start < 0:
            raise ValueError('formula source missing or out of order')
        end = start + len(source)
        # Frontend string offsets use UTF-16 code units.
        offset = lambda s: len(s.encode('utf-16-le')) // 2
        spans.append({'start': offset(text[:start]), 'end': offset(text[:end]), 'source': source, 'latex': latex, 'fallback': False})
        cursor = end
    return text, spans


async def enrich_paragraphs(pdf, blocks, *, backend, concurrency=3):
    targets = [b for b in blocks if _candidate(b)]
    stats = {'total': len(targets), 'ok': 0, 'failed': 0}
    sem = asyncio.Semaphore(concurrency)

    async def one(block):
        async with sem:
            try:
                images = await asyncio.to_thread(_images, pdf, block)
                content = [{'type': 'text', 'text': PROMPT}]
                content.extend({'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(image).decode('ascii')}} for image in images)
                raw = await backend.chat([{'role': 'user', 'content': content}], temperature=0.0, max_tokens=3000)
                text, spans = _restore(raw, block.text)
                if len(spans) < len(block.inline_math or []) // 2:
                    raise ValueError('too few formulas recognized')
                block.text, block.inline_math = text, spans
                stats['ok'] += 1
            except Exception as exc:
                log.warning('正文识图回退 %s: %s', block.id, exc)
                stats['failed'] += 1

    await asyncio.gather(*(one(b) for b in targets))
    return stats
