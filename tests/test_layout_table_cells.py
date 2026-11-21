import pymupdf
from paperead.layout import _table_cell_text


def test_cell_text_uses_visual_character_order_for_shifted_punctuation():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((60, 100), '86', fontsize=10)
    page.insert_text((71, 102), '.', fontsize=6)
    page.insert_text((73, 100), '55', fontsize=10)
    assert _table_cell_text(page, (58, 88, 90, 105)) == '86.55'
