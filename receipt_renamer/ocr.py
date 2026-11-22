"""오프라인 OCR: Windows 내장 OCR(Windows.Media.Ocr, 한국어).

인터넷·API 키 없이 동작하며 영수증이 PC 밖으로 나가지 않는다.
첫 판독에서 빠진 항목이 있으면 이미지를 보정해 다시 읽고 빈 항목만 채운다.
"""
from __future__ import annotations

import asyncio
import sys
import threading
from typing import Optional

from .models import ReceiptData
from .rules import extract_from_text

OCR_CONF = 0.6  # 오프라인 결과는 모두 확인 필요

_ns = None
_lock = threading.Lock()


def _import_winrt():
    """pywinrt(winrt-*) 또는 winsdk 중 설치된 것을 쓴다."""
    global _ns
    if _ns is not None:
        return _ns
    errors = []
    for prefix in ("winrt.windows", "winsdk.windows"):
        try:
            import importlib
            ocr = importlib.import_module(prefix + ".media.ocr")
            glob = importlib.import_module(prefix + ".globalization")
            imaging = importlib.import_module(prefix + ".graphics.imaging")
            streams = importlib.import_module(prefix + ".storage.streams")
            _ns = (ocr, glob, imaging, streams)
            return _ns
        except Exception as e:  # pragma: no cover - Windows 전용
            errors.append(f"{prefix}: {e}")
    raise RuntimeError("Windows OCR 부품을 불러오지 못했습니다. " + "; ".join(errors))


def availability() -> str:
    """사용 가능하면 '', 아니면 한글 이유."""
    if sys.platform != "win32":
        return "오프라인 OCR은 Windows 10/11에서만 쓸 수 있습니다."
    try:
        ocr, glob, _, _ = _import_winrt()
    except RuntimeError as e:
        return str(e)
    try:
        eng = ocr.OcrEngine.try_create_from_language(glob.Language("ko"))
    except Exception as e:  # pragma: no cover
        return f"Windows OCR을 시작할 수 없습니다: {e}"
    if eng is None:
        return ("Windows에 한국어 OCR 언어 팩이 없습니다. [설정 > 시간 및 언어 > 언어]에서 "
                "한국어를 추가하고 '광학 문자 인식'을 설치하세요.")
    return ""


async def _recognize_async(img):
    ocr, glob, imaging, streams = _import_winrt()
    engine = ocr.OcrEngine.try_create_from_language(glob.Language("ko"))
    if engine is None:
        raise RuntimeError(availability())
    max_dim = int(getattr(ocr.OcrEngine, "max_image_dimension", 10000) or 10000)
    if max(img.size) > max_dim:
        r = max_dim / max(img.size)
        img = img.resize((int(img.width * r), int(img.height * r)))
    rgba = img.convert("RGBA")
    writer = streams.DataWriter()
    writer.write_bytes(rgba.tobytes())
    bmp = imaging.SoftwareBitmap.create_copy_from_buffer(
        writer.detach_buffer(), imaging.BitmapPixelFormat.RGBA8, rgba.width, rgba.height)
    result = await engine.recognize_async(bmp)
    lines = []
    words = []
    for line in result.lines:
        lines.append(_join_words([w.text for w in line.words]) or line.text)
        for w in line.words:
            r = w.bounding_rect
            words.append((r.x, r.y, r.width, r.height, w.text))
    return "\n".join(lines), _rows_from_words(words)


def _join_words(ws):
    """한국어 OCR은 글자마다 띄어 주는 경우가 있어, 한 글자짜리 한글 조각은 붙인다."""
    out = ""
    prev = ""
    for w in ws:
        if out and not (len(prev) == 1 and len(w) == 1 and "가" <= prev <= "힣" and "가" <= w <= "힣"):
            out += " "
        out += w
        prev = w
    return out


def _rows_from_words(words):
    """비스듬한 사진에서도 같은 높이의 단어를 한 행으로 묶는다."""
    if not words:
        return ""
    words = sorted(words, key=lambda t: t[1] + t[3] / 2)
    rows = []
    for w in words:
        cy = w[1] + w[3] / 2
        if rows and abs(rows[-1]["cy"] - cy) < max(6, w[3] * 0.5):
            rows[-1]["w"].append(w)
            n = len(rows[-1]["w"])
            rows[-1]["cy"] = (rows[-1]["cy"] * (n - 1) + cy) / n
        else:
            rows.append({"cy": cy, "w": [w]})
    out = []
    for r in rows:
        ws = sorted(r["w"], key=lambda t: t[0])
        out.append(_join_words([t[4] for t in ws]))
    return "\n".join(out)


def recognize(img) -> tuple[str, str]:
    """(OCR 원래 줄, 행 묶음) 텍스트."""
    with _lock:  # WinRT OCR 엔진은 한 번에 하나씩
        return asyncio.run(_recognize_async(img))


# ------------------------------------------------------------------ 보정 이미지

def crop_paper(img):
    """밝은 영수증 종이만 잘라낸다(책상 배경 제거)."""
    from PIL import ImageFilter
    g = img.convert("L").filter(ImageFilter.GaussianBlur(3))
    thr = max(140, int(sum(i * c for i, c in enumerate(g.histogram())) / (g.width * g.height)) + 20)
    bw = g.point(lambda v: 255 if v > thr else 0)
    box = bw.getbbox()
    if not box:
        return img
    x0, y0, x1, y1 = box
    if (x1 - x0) * (y1 - y0) < 0.2 * img.width * img.height:
        return img
    return img.crop(box)


def high_contrast(img):
    from PIL import ImageOps, ImageEnhance
    g = ImageOps.autocontrast(img.convert("L"), cutoff=2)
    g = ImageEnhance.Contrast(g).enhance(1.8)
    return g.convert("RGB")


def upscale(img, k=2):
    from PIL import Image
    if max(img.size) * k > 8000:
        k = max(1, 8000 / max(img.size))
    return img.resize((int(img.width * k), int(img.height * k)), Image.LANCZOS)


def rotate(img, deg):
    return img.rotate(deg, expand=True, fillcolor=(255, 255, 255))


_KEY_FIELDS = ("date", "amount", "card_last4", "merchant")


def _missing(d: ReceiptData):
    return [f for f in _KEY_FIELDS if getattr(d, f) in ("", None)]


def _merge_missing(dst: ReceiptData, src: ReceiptData):
    for f in ("date", "time", "amount", "merchant", "card_last4", "card_first4", "approval_no", "card_company"):
        if getattr(dst, f) in ("", None) and getattr(src, f) not in ("", None):
            dst.set(f, getattr(src, f), src.conf(f))
            if f == "amount":
                dst.currency = src.currency
    if not dst.approver_name and src.approver_name:
        dst.approver_name = src.approver_name
    if dst.supply_amount is None:
        dst.supply_amount = src.supply_amount
    if dst.category == "기타" and src.category != "기타":
        dst.category = src.category
    dst.raw_text += "\n" + src.raw_text


def extract(img) -> ReceiptData:
    lines_text, rows_text = recognize(img)
    d = extract_from_text(lines_text, OCR_CONF)
    if _missing(d) and rows_text:
        _merge_missing(d, extract_from_text(rows_text, OCR_CONF))
    if not _missing(d):
        return d
    # 보정 재판독: ① 종이 자르기 ② 흑백·대비 ③ 2배 확대 ④ ±1~2도 회전
    base = crop_paper(img)
    steps = [base, high_contrast(base), upscale(high_contrast(base))]
    for deg in (1, -1, 2, -2):
        steps.append(rotate(high_contrast(base), deg))
    for step in steps:
        lt, rt = recognize(step)
        for t in (lt, rt):
            if t:
                _merge_missing(d, extract_from_text(t, OCR_CONF))
        if not _missing(d):
            break
    return d


def selftest() -> Optional[str]:
    reason = availability()
    if reason:
        return reason
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (400, 120), "white")
    ImageDraw.Draw(img).text((10, 40), "TOTAL 12,000", fill="black")
    try:
        recognize(upscale(img, 3))
    except Exception as e:
        return f"OCR 실행 실패: {e}"
    return None
