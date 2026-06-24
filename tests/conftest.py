import pytest


@pytest.fixture
def sample_subject_patterns():
    return [r".*应聘.*【BOSS直聘】", r".*应聘.*"]
