import asyncio
import json
import fitz
from paperead.layout import Block
from paperead.vision_text import enrich_paragraphs

class Backend:
    def __init__(self, result): self.result, self.calls = result, []
    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        if isinstance(self.result, Exception): raise self.result
        return json.dumps(self.result, ensure_ascii=False)

def pdf_file(tmp_path):
    path = tmp_path / 'test.pdf'
    doc = fitz.open()
    for _ in range(2):
        page = doc.new_page()
        page.insert_text((50, 70), 'The condition number k controls the convergence rate of the method.')
    doc.save(path)
    doc.close()
    return path

def block(**kw):
    args = dict(id='p', order=0, page=1, kind='para', text='The condition number k controls the convergence rate of the method.', bbox=(40, 45, 400, 85), inline_math=[{'start':21,'end':22,'latex':'k','fallback':True}])
    args.update(kw)
    return Block(**args)

def test_recognizes_complex_paragraph_and_skips_plain(tmp_path):
    b = block()
    plain = block(id='plain', inline_math=[])
    backend = Backend({'text': b.text, 'formulas': [{'source':'k', 'latex':r'\kappa'}]})
    asyncio.run(enrich_paragraphs(pdf_file(tmp_path), [b, plain], backend=backend))
    assert len(backend.calls) == 1
    assert b.inline_math[0]['latex'] == r'\kappa'
    assert b.text[b.inline_math[0]['start']:b.inline_math[0]['end']] == 'k'

def test_cross_page_images_and_failure_fallback(tmp_path):
    b = block(bboxes=[(1,40,45,400,85),(2,40,45,400,85)])
    before = (b.text, list(b.inline_math))
    backend = Backend(RuntimeError('failure'))
    asyncio.run(enrich_paragraphs(pdf_file(tmp_path), [b], backend=backend))
    assert len([x for x in backend.calls[0][0]['content'] if x['type'] == 'image_url']) == 2
    assert (b.text, b.inline_math) == before

def test_rejects_incomplete_model_result(tmp_path):
    b = block()
    before = (b.text, list(b.inline_math))
    backend = Backend({'text':'The method.', 'formulas':[]})
    asyncio.run(enrich_paragraphs(pdf_file(tmp_path), [b], backend=backend))
    assert (b.text, b.inline_math) == before
