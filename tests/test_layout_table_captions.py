from paperead.layout import _is_caption_text, _is_table_caption


def test_appendix_numbered_table_caption_is_recognized():
    """附录编号 `Table A.1` / `Table B.2` 属于题注，不能当成章节标题。"""
    assert _is_table_caption('Table A.1 Summary of main mathematical notations.')
    assert _is_table_caption('Table B.2')
    assert _is_caption_text('Table A.1 Summaries of the datasets used in the appendix.')


def test_algorithm_caption_is_a_table_heading():
    """算法框标题就是表头，需与表格走同一套题注判定。"""
    assert _is_table_caption('Algorithm 1 Preference-centric knowledge diffusion.')
    assert _is_caption_text('Algorithm 2 Preference evolution and attention aggregation.')


def test_algorithm_sentence_in_body_is_not_a_caption():
    assert not _is_caption_text('Algorithm 1 shows how the preference evolves on the graph.')
    assert not _is_caption_text('Algorithm 2 presents the aggregation procedure in detail.')


def test_figure_subpanel_reference_sentence_is_not_a_caption():
    """`Fig. 2(B)-(C), we introduce ...` 是正文里的跨子图引用句。"""
    assert not _is_caption_text(
        'Fig. 2(B)-(C), we introduce a preference evolution mechanism that aggregates neighbors.'
    )
    # 真正的子图图注仍要保留
    assert _is_caption_text('Fig. 1 (a) Comparison of the learned embeddings across datasets.')