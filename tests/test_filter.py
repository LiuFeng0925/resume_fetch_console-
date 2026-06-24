from src.filter import AttachmentFilter


def test_allows_pdf_case_insensitive():
    f = AttachmentFilter([".pdf"])
    assert f.is_allowed("resume.PDF") is True
    assert f.is_allowed("resume.pdf") is True


def test_rejects_other_extensions():
    f = AttachmentFilter([".pdf"])
    assert f.is_allowed("notes.docx") is False


def test_no_extension_rejected():
    f = AttachmentFilter([".pdf"])
    assert f.is_allowed("README") is False
