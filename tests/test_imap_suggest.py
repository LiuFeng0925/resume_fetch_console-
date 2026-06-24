from src.imap_suggest import suggest_imap


def test_suggest_known_provider():
    result = suggest_imap("user@163.com")
    assert result is not None
    assert result["host"] == "imap.163.com"
    assert result["source"] == "known"


def test_suggest_same_domain():
    existing = [{"username": "a@brgroup.com", "imap_host": "imap.brgroup.com", "imap_port": 993, "imap_ssl": True}]
    result = suggest_imap("b@brgroup.com", existing)
    assert result is not None
    assert result["host"] == "imap.brgroup.com"
    assert result["source"] == "domain"


def test_suggest_guess_unknown_domain():
    result = suggest_imap("user@bairong.com")
    assert result is not None
    assert result["host"] == "imap.bairong.com"
    assert result["source"] == "guess"


def test_suggest_invalid_email():
    assert suggest_imap("not-an-email") is None
