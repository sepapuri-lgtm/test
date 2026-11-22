"""입력 파일 모으기와 이미지·PDF 읽기."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .models import ReceiptItem

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".bmp"}
PDF_EXT = {".pdf"}
SUPPORTED_EXT = IMAGE_EXT | PDF_EXT

# 결과 폴더 이름: 20261009-1530-변환완료(_2), 예전 …_변환
_RESULT_DIR = re.compile(r"(^\d{8}-\d{4}-변환완료(_\d+)?$)|(_변환$)")

AI_MAX_SIDE = 1800
PDF_DPI = 200

try:  # HEIC
    import pillow_heif  # type: ignore
    pillow_heif.register_heif_opener()
    HEIC_OK = True
except Exception:  # pragma: no cover
    HEIC_OK = False

try:
    import pymupdf as fitz  # type: ignore
    PDF_OK = True
except ImportError:  # 예전 PyMuPDF
    try:
        import fitz  # type: ignore
        PDF_OK = True
    except Exception:  # pragma: no cover
        fitz = None
        PDF_OK = False
except Exception:  # pragma: no cover
    fitz = None
    PDF_OK = False


def is_result_dir(p: Path) -> bool:
    return bool(_RESULT_DIR.search(p.name))


@dataclass
class CollectResult:
    files: list
    skipped_unsupported: int = 0
    skipped_result_dir: int = 0
    skipped_duplicate: int = 0
    not_found: int = 0


def collect(paths: Iterable, existing: set | None = None) -> CollectResult:
    """끌어놓은 파일·폴더에서 지원 형식 파일을 모은다. 하위 결과 폴더는 건너뛴다."""
    existing = {str(Path(p).resolve()).lower() for p in (existing or set())}
    res = CollectResult(files=[])
    seen = set(existing)

    def add(f: Path):
        if f.suffix.lower() not in SUPPORTED_EXT:
            res.skipped_unsupported += 1
            return
        key = str(f.resolve()).lower()
        if key in seen:
            res.skipped_duplicate += 1
            return
        seen.add(key)
        res.files.append(f)

    for raw in paths:
        p = Path(str(raw).strip().strip('"').strip("{}"))
        if not p.exists():
            res.not_found += 1
            continue
        if p.is_dir():
            if is_result_dir(p):
                res.skipped_result_dir += 1
                continue
            for f in sorted(p.rglob("*")):
                if any(is_result_dir(parent) for parent in f.relative_to(p).parents if str(parent) != "."):
                    if f.is_file():
                        res.skipped_result_dir += 1
                    continue
                if f.is_file():
                    add(f)
        else:
            if any(is_result_dir(parent) for parent in p.parents):
                res.skipped_result_dir += 1
                continue
            add(p)
    return res


def pdf_page_count(path: Path) -> int:
    if not PDF_OK:
        return 1
    try:
        with fitz.open(path) as doc:
            return doc.page_count
    except Exception:
        return 1


def make_items(files: list, split_pdf: bool = True) -> list:
    items = []
    for f in files:
        if f.suffix.lower() == ".pdf" and split_pdf:
            n = pdf_page_count(f)
            if n > 1:
                items.extend(ReceiptItem(path=f, page=i) for i in range(n))
                continue
        items.append(ReceiptItem(path=f))
    return items


# ------------------------------------------------------------------ 읽기

def pdf_text(path: Path, page: Optional[int]) -> str:
    if not PDF_OK:
        return ""
    with fitz.open(path) as doc:
        pages = [doc[page]] if page is not None else list(doc)
        return "\n".join(p.get_text("text") for p in pages).strip()


def pdf_render(path: Path, page: Optional[int], dpi: int = PDF_DPI):
    from PIL import Image
    if not PDF_OK:
        raise RuntimeError("PDF 부품(PyMuPDF)이 설치되어 있지 않습니다.")
    with fitz.open(path) as doc:
        pg = doc[page or 0]
        pix = pg.get_pixmap(dpi=dpi)
        return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")


def load_image(path: Path):
    from PIL import Image, ImageOps
    if path.suffix.lower() in (".heic", ".heif") and not HEIC_OK:
        raise RuntimeError("HEIC 부품(pillow-heif)이 설치되어 있지 않습니다.")
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)  # EXIF 회전 보정
    return img.convert("RGB")


def item_image(item: ReceiptItem):
    if item.path.suffix.lower() == ".pdf":
        return pdf_render(item.path, item.page)
    return load_image(item.path)


def item_text_layer(item: ReceiptItem) -> str:
    if item.path.suffix.lower() != ".pdf":
        return ""
    t = pdf_text(item.path, item.page)
    # 의미 있는 글자가 충분해야 텍스트 PDF로 본다
    return t if len(re.sub(r"\s", "", t)) >= 20 else ""


def shrink(img, max_side: int = AI_MAX_SIDE):
    w, h = img.size
    s = max(w, h)
    if s <= max_side:
        return img
    r = max_side / s
    from PIL import Image
    return img.resize((int(w * r), int(h * r)), Image.LANCZOS)


def jpeg_bytes(img, quality: int = 88) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()
