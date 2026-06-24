import pytest

from src.matcher import SubjectMatcher


@pytest.fixture
def matcher():
    return SubjectMatcher([r".*应聘.*【BOSS直聘】", r".*应聘.*"])


def test_matches_boss_subject(matcher):
    subject = "刘烨 | 7年，应聘 AI产品经理 | 北京30-40K【BOSS直聘】"
    assert matcher.matches(subject) is True


def test_matches_generic_apply(matcher):
    assert matcher.matches("张三应聘Java") is True


def test_no_match_newsletter(matcher):
    assert matcher.matches("公司周报 2026-05") is False


def test_invalid_regex_raises():
    with pytest.raises(ValueError, match="Invalid regex"):
        SubjectMatcher([r"[unclosed"])
