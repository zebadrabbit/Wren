from wren.communication import chunking


def test_chunks_splits_text_over_the_limit():
    long_text = "x" * 5000
    parts = chunking.chunks(long_text, 2000)
    assert len(parts) == 3
    assert all(len(p) <= 2000 for p in parts)
    # nothing may be silently dropped on the way through the splitter
    assert "".join(parts) == long_text


def test_chunks_splits_on_the_last_newline_that_fits():
    # a note dump is lines; cutting mid-line is uglier than cutting between two
    first_line = "a" * 1800
    text = f"{first_line}\n" + "b" * 300
    parts = chunking.chunks(text, 2000)
    assert parts == [first_line, "b" * 300]


def test_chunks_hard_cuts_a_single_over_long_line():
    # one line longer than the whole ceiling has no newline to cut on, so the
    # splitter must fall back to a hard cut rather than emit an oversized part
    text = "x" * 4500
    parts = chunking.chunks(text, 2000)
    assert parts == ["x" * 2000, "x" * 2000, "x" * 500]


def test_chunks_at_exactly_the_limit_is_one_piece():
    parts = chunking.chunks("x" * 2000, 2000)
    assert parts == ["x" * 2000]


def test_a_leading_newline_never_produces_an_empty_chunk():
    # an empty message body is its own 400 on every surface this feeds;
    # splitting must not manufacture one
    parts = chunking.chunks("\n" + "x" * 2500, 2000)
    assert all(parts)
