from src.config import MAX_PARSER_CONCURRENCY, clamp_parser_concurrency


def test_clamp_parser_concurrency_bounds():
    assert MAX_PARSER_CONCURRENCY == 2
    assert clamp_parser_concurrency(1) == 1
    assert clamp_parser_concurrency(2) == 2
    assert clamp_parser_concurrency(5) == 2
    assert clamp_parser_concurrency(0) == 1
    assert clamp_parser_concurrency("2") == 2
    assert clamp_parser_concurrency(None, default=1) == 1
