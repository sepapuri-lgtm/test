"""파일명 템플릿, 금액 표기, 금지 문자 제거, 중복 이름 처리."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .models import ReceiptItem
from .normalize import net_amount
from .rules import ZERO_DECIMAL

UNKNOWN = "미확인"

CURRENCY_SIGN = {"KRW": "₩", "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CNY": "CN¥",
                 "CAD": "CA$", "AUD": "A$", "HKD": "HK$", "SGD": "S$", "TWD": "NT$", "NZD": "NZ$"}

TEMPLATE_VARS = ["{결제일}", "{시각}", "{사용자}", "{가맹점}", "{카드끝번호}", "{카드사}", "{금액}", "{금액콤마}",
                 "{금액원화}", "{결제금액}", "{업종}", "{원본명}", "{순번}"]

_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def format_amount(amount: Optional[float], currency: str, style: str = "sign") -> str:
    """style: 'plain' 45000 / 'comma' 45,000 / 'sign' ₩45,000, $22.00, CHF 9.90."""
    if amount is None:
        return ""
    cur = (currency or "KRW").upper()
    neg = amount < 0
    a = abs(amount)
    if cur in ZERO_DECIMAL:
        plain, comma = f"{int(round(a))}", f"{int(round(a)):,}"
    else:
        plain, comma = f"{a:.2f}", f"{a:,.2f}"
    if style == "plain":
        s = plain
    elif style == "comma":
        s = comma
    else:
        sign = CURRENCY_SIGN.get(cur)
        s = f"{sign}{comma}" if sign else f"{cur} {comma}"
    return ("-" if neg else "") + s


def format_time_display(hhmm: str) -> str:
    """'1432' → '오후 2:32'."""
    if not hhmm or len(hhmm) != 4:
        return ""
    h, m = int(hhmm[:2]), int(hhmm[2:])
    ap = "오전" if h < 12 else "오후"
    h12 = h % 12 or 12
    return f"{ap} {h12}:{m:02d}"


def format_time_filename(hhmm: str) -> str:
    """파일명용: '오후 3시01분' (':'는 쓸 수 없음)."""
    if not hhmm or len(hhmm) != 4:
        return ""
    h, m = int(hhmm[:2]), int(hhmm[2:])
    ap = "오전" if h < 12 else "오후"
    return f"{ap} {h % 12 or 12}시{m:02d}분"


def parse_time_input(s: str) -> Optional[str]:
    """오후 2:32 / 14:32 / 1432 / 2:32 PM → '1432'. 빈 값은 ''. 알 수 없으면 None."""
    s = (s or "").strip()
    if not s:
        return ""
    ap = None
    low = s.lower()
    if "오후" in s or "pm" in low:
        ap = "pm"
    elif "오전" in s or "am" in low:
        ap = "am"
    digits = re.sub(r"[^\d:시분]", "", s)
    m = re.fullmatch(r"(\d{1,2})[:시](\d{2})분?", digits) or re.fullmatch(r"(\d{1,2})(\d{2})", digits)
    if not m:
        m2 = re.fullmatch(r"(\d{1,2})시?", digits)
        if not m2:
            return None
        h, mi = int(m2.group(1)), 0
    else:
        h, mi = int(m.group(1)), int(m.group(2))
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}{mi:02d}"


_CUR_WORDS = {"달러": "USD", "불": "USD", "원": "KRW", "엔": "JPY", "유로": "EUR", "파운드": "GBP", "위안": "CNY"}


def parse_amount_input(s: str, default_currency: str = "KRW"):
    """'$22.00' / '22 달러' / '₩45,000' / '45000' / 'CHF 9.90' / '-3,200' → (금액, 통화). 실패하면 None."""
    s = (s or "").strip()
    if not s:
        return None, default_currency
    cur = None
    for sym, code in (("US$", "USD"), ("CA$", "CAD"), ("A$", "AUD"), ("HK$", "HKD"), ("S$", "SGD"),
                      ("₩", "KRW"), ("$", "USD"), ("€", "EUR"), ("£", "GBP"), ("¥", "JPY"), ("￥", "JPY")):
        if sym in s:
            cur = code
            break
    if not cur:
        m = re.search(r"\b([A-Za-z]{3})\b", s)
        if m and m.group(1).upper() in ("KRW", "USD", "EUR", "JPY", "GBP", "CNY", "CHF", "CAD", "AUD",
                                         "HKD", "SGD", "TWD", "THB", "VND", "NZD"):
            cur = m.group(1).upper()
    if not cur:
        for w, code in _CUR_WORDS.items():
            if w in s:
                cur = code
                break
    num = re.sub(r"[^\d.\-]", "", s)
    try:
        val = float(num)
    except ValueError:
        return None
    return val, cur or default_currency


def sanitize(name: str) -> str:
    s = _FORBIDDEN.sub("", name)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s


def render(template: str, item: ReceiptItem, seq: int = 1) -> str:
    """확장자를 뺀 새 파일명."""
    d = item.data
    net = net_amount(d)
    values = {
        "결제일": d.date or UNKNOWN,
        "시각": format_time_filename(d.time) or UNKNOWN,
        "사용자": d.user or UNKNOWN,
        "가맹점": d.merchant or UNKNOWN,
        "카드끝번호": d.card_last4 or UNKNOWN,
        "카드사": d.card_company or UNKNOWN,
        "금액": format_amount(net, d.currency, "plain") or UNKNOWN,
        "금액콤마": format_amount(net, d.currency, "comma") or UNKNOWN,
        "금액원화": format_amount(net, d.currency, "sign") or UNKNOWN,
        "결제금액": format_amount(d.amount, d.currency, "sign") or UNKNOWN,
        "업종": d.category or "기타",
        "원본명": item.path.stem + (f"_p{item.page + 1}" if item.page is not None else ""),
        "순번": f"{seq:03d}",
    }

    def rep(m):
        key = m.group(1)
        return values.get(key, m.group(0))

    out = re.sub(r"\{([^{}]+)\}", rep, template)
    return sanitize(out) or UNKNOWN


def output_ext(item: ReceiptItem) -> str:
    return item.path.suffix.lower() if item.page is None else ".pdf"


def fit_length(folder: Path, stem: str, ext: str, limit: int = 255) -> str:
    """전체 경로 255자 이내로 자른다."""
    room = limit - len(str(folder)) - 1 - len(ext) - 3  # _NN 여유
    if room < 10:
        room = 10
    return stem[:room].rstrip(" ._")


def unique_path(folder: Path, stem: str, ext: str, taken: set) -> Path:
    stem = fit_length(folder, stem, ext)
    p = folder / f"{stem}{ext}"
    n = 2
    while p.exists() or str(p).lower() in taken:
        p = folder / f"{stem}_{n}{ext}"
        n += 1
    taken.add(str(p).lower())
    return p
