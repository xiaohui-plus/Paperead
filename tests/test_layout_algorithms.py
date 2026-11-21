import pymupdf
from paperead.layout import parse_pdf


def test_ruled_algorithm_is_extracted_with_heading_and_full_body(tmp_path):
    pdf = tmp_path / 'algorithm.pdf'
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((50, 70), 'Algorithm 3 Preference-level contrastive learning.', fontsize=10)
    for y in (58, 75, 220):
        page.draw_line((48, y), (290, y))
    for i in range(8):
        page.insert_text((55, 95 + i * 15), f'{i + 1}: Update the preference representation', fontsize=9)
    page.insert_text((320, 100), 'This paragraph is outside the algorithm.', fontsize=10)
    doc.save(pdf)
    doc.close()
    parsed = parse_pdf(pdf, figure_dir=tmp_path / 'out')
    algorithms = [b for b in parsed.blocks if b.kind == 'table' and b.caption.startswith('Algorithm 3')]
    assert len(algorithms) == 1
    assert algorithms[0].bbox[1] <= 58 and algorithms[0].bbox[3] >= 220
    assert algorithms[0].bbox[2] < 320
    assert algorithms[0].image_path
