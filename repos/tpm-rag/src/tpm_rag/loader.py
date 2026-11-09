from typing import List, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from llama_index.core.node_parser import SentenceSplitter
from dotenv import load_dotenv
import fitz  # PyMuPDF
import pytesseract
from PIL import Image
import io
import os
import re

# Re-exported, not defined here any more - embedding moved to embed.py so the
# serving path can import it without this module's OCR imports above. Keep
# this line: `from .loader import embed_texts` is what ingest.py and anything
# written against the old layout still does.
from .embed import EMBED_DIM, EMBED_MODEL, embed_texts  # noqa: F401



load_dotenv()

# Tesseract lives in different places on every platform. Set TESSERACT_CMD if
# it is not already on PATH:
#   Windows  C:\Program Files\Tesseract-OCR\tesseract.exe
#   macOS    /opt/homebrew/bin/tesseract
#   Linux    usually on PATH already, nothing to set
_tesseract = os.getenv("TESSERACT_CMD")
if _tesseract:
    pytesseract.pytesseract.tesseract_cmd = _tesseract

splitter = SentenceSplitter(chunk_size=1000, chunk_overlap=200)

MIN_TEXT_LENGTH = 30
OCR_ZOOM = 3.0
OCR_LANG = "tha+eng"
MAX_OCR_WORKERS = 4
OCR_DENSITY_THRESHOLD = 0.6


# Add near your other regex/constants
HEADING_PATTERNS = re.compile(
    r'^(บทที่\s*\d+|ส่วนที่\s*\d+|Chapter\s*\d+|ขั้นตอนที่\s*\d+[:\.]?\s*.{0,40}$)',
    re.IGNORECASE
)

def _extract_heading(text: str) -> Optional[str]:
    """Look for a heading-like line near the top of a page's text."""
    for line in text.split("\n")[:5]:  # headings usually appear early on a page
        stripped = line.strip()
        if HEADING_PATTERNS.match(stripped) and len(stripped) < 80:
            return stripped
    return None

def _clean_ocr_text(text: str) -> str:
    lines = text.split("\n")
    cleaned_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        real_chars = len(re.findall(r'[\u0E00-\u0E7Fa-zA-Z0-9.,!?():\-]', stripped))
        total_chars = len(stripped)

        if total_chars == 0:
            continue

        density = real_chars / total_chars

        if total_chars < 15 and density < 0.7:
            continue

        if density >= OCR_DENSITY_THRESHOLD:
            cleaned_lines.append(stripped)

    return "\n".join(cleaned_lines)


def _ocr_page(page: fitz.Page) -> str:
    matrix = fitz.Matrix(OCR_ZOOM, OCR_ZOOM)
    pix = page.get_pixmap(matrix=matrix)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    return pytesseract.image_to_string(img, lang=OCR_LANG)


def load_and_chunk_pdf(path: str) -> Tuple[List[str], List[int]]:
    doc = fitz.open(path)

    page_texts = [None] * len(doc)
    pages_needing_ocr = []

    for page_num, page in enumerate(doc):
        text = page.get_text().strip()
        if len(text) < MIN_TEXT_LENGTH:
            pages_needing_ocr.append(page_num)
        else:
            page_texts[page_num] = _clean_ocr_text(text)

    print(f"[load_and_chunk_pdf] {len(doc)} pages total, "
          f"{len(pages_needing_ocr)} need OCR")

    if pages_needing_ocr:
        with ThreadPoolExecutor(max_workers=MAX_OCR_WORKERS) as executor:
            future_to_page = {
                executor.submit(_ocr_page, doc[page_num]): page_num
                for page_num in pages_needing_ocr
            }
            done_count = 0
            for future in as_completed(future_to_page):
                page_num = future_to_page[future]
                try:
                    page_texts[page_num] = _clean_ocr_text(future.result().strip())
                except Exception as e:
                    print(f"[load_and_chunk_pdf] OCR failed on page {page_num}: {e}")
                    page_texts[page_num] = ""
                done_count += 1
                if done_count % 25 == 0 or done_count == len(pages_needing_ocr):
                    print(f"[load_and_chunk_pdf] OCR progress: "
                          f"{done_count}/{len(pages_needing_ocr)}")

    doc.close()

    chunks = []
    pages = []
    current_heading = None

    for page_num, t in enumerate(page_texts):
        if not t:
            continue

        heading = _extract_heading(t)
        if heading:
            current_heading = heading

        page_chunks = splitter.split_text(t)
        for c in page_chunks:
            if current_heading:
                chunks.append(f"[{current_heading}]\n{c}")
            else:
                chunks.append(c)
            pages.append(page_num + 1)

    return chunks, pages
