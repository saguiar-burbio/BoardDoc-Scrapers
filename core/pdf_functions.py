import os
import re
import csv
import glob
import logging
from typing import Optional
from dateutil import parser
import fitz  # PyMuPDF
import pytesseract
from PIL import Image

# Use the established system logging named context
LOGGER = logging.getLogger("simbli_minutes")


# ═════════════════════════════════════════════════════════════════════════════
# 1. DATE EXTRACTION UTILITIES
# ═════════════════════════════════════════════════════════════════════════════

def extract_first_date_from_pdf_simbli(pdf_path: str) -> str:
    """
    Scans the first page of a Simbli PDF to find and extract the meeting date.
    Attempts digital text parsing before falling back to optical character recognition (OCR).
    """
    date_regex = (
        r'\b(?:'
        r'\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4}'
        r'|'
        r'\d{2,4}[\/\-.]\d{1,2}[\/\-.]\d{1,2}'
        r'|'
        r'(?:\d{1,2}\s+)?(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|'
        r'May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|'
        r'Nov(?:ember)?|Dec(?:ember)?)[ ,\-\.]?\s?\d{1,2}[, ]?\s?\d{2,4}'
        r'|'
        r'\d{1,2}(?:st|nd|rd|th)\s+day\s+of\s+'
        r'(?:January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+\d{4}'
        r')\b'
    )

    try:
        with fitz.open(pdf_path) as pdf:
            page = pdf[0]

            # 1. Try standard text extraction
            text = page.get_text("text").strip()

            # 2. If standard header extraction is insufficient, extract positioned text blocks
            if not text or len(text) < 20:
                blocks = page.get_text("blocks")
                blocks.sort(key=lambda b: b[1])  # Sort top to bottom by y-coordinate
                text = "\n".join(block[4] for block in blocks if block[4].strip())

            # 3. If still empty, fall back to Tesseract OCR
            if not text.strip():
                LOGGER.debug(f"OCR fallback for: {pdf_path}")
                pix = page.get_pixmap()
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                text = pytesseract.image_to_string(img)

            # 4. Search and return the first matched date expression
            match = re.search(date_regex, text)
            return match.group(0) if match else "No date found"

    except Exception as e:
        return f"Error: {str(e)}"


def extract_first_date_from_pdf(pdf_path: str) -> str:
    """
    Standard date extractor for general target documents. 
    Falls back to OCR if digital fonts are unmapped.
    """
    date_regex = (
        r'\b(?:'
        r'\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4}'                             # 05/12/2024 or 5-12-24
        r'|'
        r'\d{2,4}[\/\-.]\d{1,2}[\/\-.]\d{1,2}'                             # 2024-05-12 or 24-05-12
        r'|'
        r'(?:\d{1,2}\s+)?(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|'
        r'May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|'
        r'Nov(?:ember)?|Dec(?:ember)?)[ ,\-\.]?\s?\d{1,2}[, ]?\s?\d{2,4}'  # May 12, 2024
        r'|'
        r'\d{1,2}(?:st|nd|rd|th)\s+day\s+of\s+'
        r'(?:January|February|March|April|May|June|July|August|September|October|November|December)'
        r'\s+\d{4}'                                                        # 2nd day of May 2024
        r')\b'
    )
    try:
        with fitz.open(pdf_path) as pdf:
            text = pdf[0].get_text("text")
            if text.strip():
                match = re.search(date_regex, text)
                if match:
                    return match.group(0)

            # Standard OCR Fallback Execution
            LOGGER.debug(f"OCR fallback for: {pdf_path}")
            pix = pdf[0].get_pixmap()
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            ocr_text = pytesseract.image_to_string(img)
            match = re.search(date_regex, ocr_text)
            return match.group(0) if match else "No date found"
            
    except Exception as e:
        return f"Error: {str(e)}"


def parse_to_mmddyy(date_str: str) -> Optional[str]:
    """Parses arbitrary string date patterns securely into standardized 'MM-DD-YY' strings."""
    try:
        dt = parser.parse(date_str)
        return dt.strftime("%m-%d-%y")
    except Exception:
        return None


# ═════════════════════════════════════════════════════════════════════════════
# 2. OUTPUT & CLOUD CONFLICT RENAME ENGINES
# ═════════════════════════════════════════════════════════════════════════════

def write_to_csv(nces: str, title: str, date: str, url: str, file_name: str, CSV_OUTPUT_PATH: str) -> None:
    """Appends running crawl operations out to a legacy local tracking sheet."""
    header = ['NCES', 'Title', 'Date', 'URL', 'File Name']
    row = [nces, title, date, url, file_name]

    with open(CSV_OUTPUT_PATH, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if f.tell() == 0:
            writer.writerow(header)
        writer.writerow(row)


def rename_file(nces: str, district: str, doc_type: str, extracted_date: str, DOWNLOAD_DIR: str) -> str:
    """Generates unique filepaths within a directory avoiding namespace overlaps."""
    files = glob.glob(os.path.join(DOWNLOAD_DIR, '*.pdf'))
    base = f"{nces}_{district}_{doc_type}"
    pattern = rf'^{re.escape(base)}(\d*)_{re.escape(extracted_date)}\.pdf$'

    existing = [f for f in files if re.match(pattern, os.path.basename(f))]
    numbers = [int(re.match(pattern, os.path.basename(f)).group(1) or 0) for f in existing if re.match(pattern, os.path.basename(f))]
    next_number = max(numbers, default=-1) + 1
    suffix = "" if next_number == 0 else str(next_number)
    return os.path.join(DOWNLOAD_DIR, f"{base}{suffix}_{extracted_date}.pdf")


# ═════════════════════════════════════════════════════════════════════════════
# 3. OCR TEXT EXTRACTION LAYER (MINHASH PREPARATION)
# ═════════════════════════════════════════════════════════════════════════════

def extract_text_with_ocr(pdf_path: str, max_pages: int = 3) -> tuple[str, int]:
    """
    Extracts plain text from up to max_pages of a PDF.
    Uses native text extractions, falling back to OCR only for scans.
    """
    text_parts = []
    
    try:
        with fitz.open(pdf_path) as doc:
            actual_total_pages = len(doc)
            pages_to_read = min(actual_total_pages, max_pages)

            LOGGER.info(f"  [MinHash] Processing first {pages_to_read} of {actual_total_pages} pages.")
            for i in range(pages_to_read):
                page = doc[i]

                # 1. Try standard text extraction
                page_text = page.get_text("text").strip()

                if page_text:
                    text_parts.append(page_text)
                    LOGGER.debug(f"  [MinHash] Page {i+1}: Text extracted successfully.")
                else:
                    # 2. Fallback to OCR for this specific page
                    LOGGER.debug(f"  [MinHash] Page {i+1}: Scan detected. Running OCR...")
                    pix = page.get_pixmap(matrix=fitz.Matrix(1.0, 1.0))
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    text_parts.append(pytesseract.image_to_string(img))

                    # Cleanup references to release memory allocation frames
                    del pix
                    del img

            return "\n".join(text_parts), actual_total_pages

    except Exception as e:
        LOGGER.error(f"  ❌ MinHash text extraction failed for {pdf_path}: {e}")
        return "", 0