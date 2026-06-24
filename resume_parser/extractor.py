"""文件 → 纯文本。电子版 PDF 抽文字层；扫描件/图片走本地 OCR。"""
from pathlib import Path
import re
import threading
import fitz  # PyMuPDF

PDF_EXT = {".pdf"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif"}
SUPPORTED_EXT = PDF_EXT | IMAGE_EXT

# 文字层字符数低于此阈值，视为扫描件，转走 OCR
MIN_TEXT_CHARS = 20
# 文字层中有效内容（中文/常见符号）的最少占比，低于则视为加密/乱码 PDF，转走 OCR
MIN_CONTENT_RATIO = 0.05

_ocr_engine = None
_ocr_lock = threading.Lock()


class UnsupportedFileError(Exception):
    pass


# 匹配中文、日文、韩文字符和常见标点
_CONTENT_CHAR_RE = re.compile(r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af\u3000-\u303f\uff00-\uffef]')


def _has_meaningful_text(text: str) -> bool:
    """判断文字层是否包含有意义的内容（而非加密 token 或纯英文乱码）。"""
    if not text:
        return False
    total = len(text)
    if total < MIN_TEXT_CHARS:
        return False
    content_chars = len(_CONTENT_CHAR_RE.findall(text))
    return content_chars / total >= MIN_CONTENT_RATIO


def _get_ocr():
    global _ocr_engine
    if _ocr_engine is None:
        with _ocr_lock:
            if _ocr_engine is None:  # double-checked locking
                from rapidocr_onnxruntime import RapidOCR
                _ocr_engine = RapidOCR()
    return _ocr_engine


def _ocr_images(images: list[bytes]) -> str:
    """对一组 PNG 字节做 OCR，拼接所有识别文本。"""
    import numpy as np
    import cv2
    engine = _get_ocr()
    texts = []
    for img_bytes in images:
        arr = np.frombuffer(img_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            continue
        result, _ = engine(img)
        if result:
            texts.extend(line[1] for line in result)
    return "\n".join(texts)


def _render_pdf_to_pngs(doc) -> list[bytes]:
    pngs = []
    for page in doc:
        pix = page.get_pixmap(dpi=200)
        pngs.append(pix.tobytes("png"))
    return pngs


def extract_text_via_ocr(path) -> tuple[str, bool]:
    """强制走 OCR（PDF 渲染成图后识别；图片直接识别）。"""
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXT:
        raise UnsupportedFileError(f"不支持的文件类型: {ext}")

    if ext in IMAGE_EXT:
        return _ocr_images([path.read_bytes()]).strip(), True

    doc = fitz.open(path)
    try:
        pngs = _render_pdf_to_pngs(doc)
    finally:
        doc.close()
    return _ocr_images(pngs).strip(), True


def extract_text(path) -> tuple[str, bool]:
    """返回 (纯文本, 是否用了OCR)。"""
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXT:
        raise UnsupportedFileError(f"不支持的文件类型: {ext}")

    if ext in IMAGE_EXT:
        return _ocr_images([path.read_bytes()]), True

    # PDF：先抽文字层
    doc = fitz.open(path)
    try:
        text = "\n".join(page.get_text() for page in doc).strip()
        if len(text) >= MIN_TEXT_CHARS and _has_meaningful_text(text):
            return text, False
        # 文字层过少或为加密 token → 渲染成图走 OCR
        pngs = _render_pdf_to_pngs(doc)
    finally:
        doc.close()
    return _ocr_images(pngs), True


def is_pdf(path) -> bool:
    return Path(path).suffix.lower() in PDF_EXT
