"""규칙(정규식) 기반 추출.

텍스트 PDF의 텍스트층, 또는 오프라인 OCR이 읽은 글자에서
결제일·시각·금액·통화·가맹점·카드번호 등을 찾는다.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Optional

from .models import ReceiptData

# ------------------------------------------------------------------ 공통

def _lines(text: str) -> list[str]:
    out = []
    for ln in text.replace("\r", "\n").split("\n"):
        ln = re.sub(r"[ \t　]+", " ", ln).strip()
        if ln:
            out.append(ln)
    return out


def _compact(s: str) -> str:
    return re.sub(r"\s+", "", s)


# ------------------------------------------------------------------ 날짜

_EN_MONTHS = {
    m: i + 1 for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])
}

_DATE_PATTERNS = [
    # 2026-10-07, 2026.10.07, 2026/10/07, 2026. 10. 07
    re.compile(r"(?<!\d)(20\d{2})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})(?!\d)"),
    # 2026년 10월 7일
    re.compile(r"(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일"),
    # 26/10/07, 26.10.07 (두 자리 연도)
    re.compile(r"(?<![\d.\-/])(\d{2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{1,2})(?![\d.\-/])"),
]
_EN_DATE_1 = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b",
    re.I)
_EN_DATE_2 = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s+(20\d{2})\b",
    re.I)
# 20261007 처럼 붙은 날짜 (항목명이 있을 때만)
_DATE_COMPACT = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")

_DATE_LABEL_RANK = [
    (re.compile(r"승인\s*(일자|일시|날짜)|승인일"), 0),
    (re.compile(r"(결제|거래|판매|매출)\s*(일자|일시|날짜|일)|paid|payment date|date paid", re.I), 1),
    (re.compile(r"(이용|발행|사용|탑승)\s*(일자|일시|날짜)|일시|일자|날짜|date", re.I), 2),
]


def _valid_date(y: int, m: int, d: int) -> Optional[str]:
    try:
        _dt.date(y, m, d)
    except ValueError:
        return None
    return f"{y:04d}{m:02d}{d:02d}"


def _fix_ocr_month(y: int, m: int, d: int) -> tuple[int, int, int]:
    # OCR이 1을 6으로 읽는 경우(16월 → 10월) 보정
    if m > 12 and 10 <= m <= 19:
        m = 10 + (m % 10) if m % 10 <= 2 else 10
    return y, m, d


def find_dates(lines: list[str], today: Optional[_dt.date] = None) -> list[tuple[int, str, int]]:
    """(우선순위, YYYYMMDD, 줄번호) 목록."""
    today = today or _dt.date.today()
    found = []
    for i, ln in enumerate(lines):
        rank = 3
        for pat, r in _DATE_LABEL_RANK:
            if pat.search(ln):
                rank = r
                break
        cands = []
        for p in _DATE_PATTERNS:
            for m in p.finditer(ln):
                y, mo, d = (int(g) for g in m.groups())
                if y < 100:
                    y += 2000
                y, mo, d = _fix_ocr_month(y, mo, d)
                v = _valid_date(y, mo, d)
                if v:
                    cands.append(v)
        for m in _EN_DATE_1.finditer(ln):
            v = _valid_date(int(m.group(3)), _EN_MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
            if v:
                cands.append(v)
        for m in _EN_DATE_2.finditer(ln):
            v = _valid_date(int(m.group(3)), _EN_MONTHS[m.group(2)[:3].lower()], int(m.group(1)))
            if v:
                cands.append(v)
        if not cands and rank < 3:
            for m in _DATE_COMPACT.finditer(ln):
                v = _valid_date(*(int(g) for g in m.groups()))
                if v:
                    cands.append(v)
        for v in cands:
            y = int(v[:4])
            # 미래 연도·2000년 이전은 버림 (OCR 오독)
            if y < 2000 or y > today.year + 1:
                continue
            found.append((rank, v, i))
    return found


def best_date(lines, today=None):
    found = find_dates(lines, today)
    if not found:
        return "", 0.0, None
    found.sort(key=lambda t: (t[0], t[2]))
    rank, v, idx = found[0]
    conf = {0: 0.95, 1: 0.92, 2: 0.85, 3: 0.75}[rank]
    today = today or _dt.date.today()
    if _dt.datetime.strptime(v, "%Y%m%d").date() > today + _dt.timedelta(days=1):
        conf = min(conf, 0.5)
    return v, conf, idx


# ------------------------------------------------------------------ 시각

_TIME = re.compile(
    r"(오전|오후|AM|PM|am|pm)?\s*(?<!\d)([01]?\d|2[0-3])\s*[:시]\s*([0-5]\d)(?:\s*[:분]\s*([0-5]\d))?\s*(초)?\s*(AM|PM|am|pm|a\.m\.|p\.m\.)?")
_TIME_LABEL = re.compile(r"승인|결제|판매|거래|매출|일시|paid", re.I)


def best_time(lines, date_line=None):
    cands = []
    for i, ln in enumerate(lines):
        for m in _TIME.finditer(ln):
            ap = (m.group(1) or m.group(6) or "").lower().replace(".", "")
            h, mi = int(m.group(2)), int(m.group(3))
            if ap in ("오후", "pm") and h < 12:
                h += 12
            if ap in ("오전", "am") and h == 12:
                h = 0
            if h > 23:
                continue
            score = 2
            if _TIME_LABEL.search(ln):
                score = 0
            elif date_line is not None and abs(i - date_line) <= 1:
                score = 1
            cands.append((score, i, f"{h:02d}{mi:02d}"))
    if not cands:
        return "", 0.0
    cands.sort()
    score, _, v = cands[0]
    return v, {0: 0.92, 1: 0.85, 2: 0.7}[score]


# ------------------------------------------------------------------ 금액

CURRENCY_SYMBOLS = {"₩": "KRW", "$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "￥": "JPY"}
CURRENCY_CODES = ("KRW", "USD", "EUR", "JPY", "GBP", "CNY", "CHF", "CAD", "AUD", "HKD",
                  "SGD", "TWD", "THB", "VND", "PHP", "NZD", "SEK", "NOK", "DKK", "INR", "MYR", "IDR")
ZERO_DECIMAL = {"KRW", "JPY", "VND", "TWD", "IDR"}

# 원화: 세 자리 쉼표 모양만 (OCR이 '원'을 숫자로 읽는 오류 방지)
_KRW_NUM = r"-?\d{1,3}(?:\s?,\s?\d{3})+|-?\d{1,3}"
_KRW_AMOUNT = re.compile(
    r"(?<![\d,.])(-?\s?\d{1,3}(?:\s?,\s?\d{3})+)(?![\d,])\s*(원|워|윈|”|\"|₩)?|(?<![\d,.])(\d{1,3})\s*(원)")
_FOREIGN_AMOUNT = re.compile(
    r"(?P<sym>[$€£¥￥]|US\$|USD|EUR|GBP|JPY|CHF|CAD|AUD|CNY|HKD|SGD)\s?(?P<num>-?\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|-?\d+(?:\.\d{1,2})?)"
    r"|(?P<num2>-?\d{1,3}(?:,\d{3})*(?:\.\d{2})|-?\d+\.\d{2})\s?(?P<code>USD|EUR|GBP|JPY|CHF|CAD|AUD|CNY|HKD|SGD)\b")

_TOTAL_LABEL = re.compile(
    r"(총\s*)?(합\s*계|결\s*제\s*금\s*액|승\s*인\s*금\s*액|결제\s*합계|총\s*액|총\s*결제|받을\s*금액|청구\s*금액|"
    r"요금\s*총[액엑역]|이용\s*금액|결제\s*요금|카드\s*결제|매출\s*금액|거래\s*금액|판매\s*금액|금\s*액|"
    r"total fare|amount paid|grand total|total|amount due|amount charged|charged)", re.I)
_TOTAL_STRONG = re.compile(
    r"합\s*계|결\s*제\s*금\s*액|승\s*인\s*금\s*액|총\s*액|요금\s*총|받을\s*금액|청구\s*금액|total fare|amount paid|grand total|amount charged",
    re.I)
_NOT_TOTAL = re.compile(
    r"공\s*급\s*가|부\s*가\s*세|과\s*세|면\s*세|봉사료|vat|tax|sub\s*total|subtotal|할인|포인트|잔액|거스름|받은\s*금액|"
    r"통행료|물품\s*[가기]액|단가|수량|부가가치세", re.I)
_SUPPLY_LABEL = re.compile(r"공\s*급\s*가\s*액?|물품\s*[가기]액|과세\s*물품|공급\s*금액", re.I)
_VAT_LABEL = re.compile(r"부\s*가\s*세|부가가치세|vat", re.I)


def _to_num(s: str) -> float:
    s = s.replace(" ", "").replace(",", "")
    return float(s)


def krw_amounts(line: str) -> list[float]:
    out = []
    for m in _KRW_AMOUNT.finditer(line):
        if m.group(1):
            num = m.group(1)
            # 1,234 자체는 쉼표가 있어야만. '원'이 없으면 쉼표 필수(이미 패턴으로 보장)
            out.append(_to_num(num))
        elif m.group(3):
            out.append(_to_num(m.group(3)))
    return out


def detect_currency(text: str) -> tuple[str, float]:
    t = text
    if "₩" in t or re.search(r"\d\s*원", t) or "KRW" in t:
        return "KRW", 0.9
    for code in CURRENCY_CODES:
        if re.search(rf"\b{code}\b", t):
            return code, 0.9
    if "US$" in t or "$" in t:
        return "USD", 0.85
    if "€" in t:
        return "EUR", 0.9
    if "£" in t:
        return "GBP", 0.9
    if "¥" in t or "￥" in t:
        return "JPY", 0.8
    if re.search(r"[가-힣]", t):
        return "KRW", 0.8
    return "KRW", 0.5


def _labelled_amounts(lines, currency):
    """(강한항목명여부, 줄번호, 금액) — 합계 항목명이 붙은 금액."""
    out = []
    for i, ln in enumerate(lines):
        if not _TOTAL_LABEL.search(ln) or _NOT_TOTAL.search(ln):
            continue
        strong = bool(_TOTAL_STRONG.search(ln))
        vals = _amounts_in(ln, currency)
        if not vals and i + 1 < len(lines):
            # 항목명 다음 줄에 금액 (요금총액(Total Fare): ↵ 28,300원)
            nxt = lines[i + 1]
            if not _NOT_TOTAL.search(nxt):
                vals = _amounts_in(nxt, currency)
        for v in vals:
            out.append((strong, i, v))
    return out


def _amounts_in(line, currency):
    if currency == "KRW":
        return krw_amounts(line)
    vals = []
    for m in _FOREIGN_AMOUNT.finditer(line):
        num = m.group("num") or m.group("num2")
        try:
            vals.append(_to_num(num))
        except ValueError:
            pass
    if not vals:
        for m in re.finditer(r"(?<![\d.])(-?\d{1,3}(?:,\d{3})*\.\d{2}|-?\d+\.\d{2})(?![\d])", line):
            vals.append(_to_num(m.group(1)))
    return vals


def _vat_triplet(all_amounts: list[float]) -> Optional[float]:
    s = sorted(set(a for a in all_amounts if a > 0))
    best = None
    for a in s:
        for b in s:
            if b >= a:
                continue
            c = a + b
            if c in s and abs(b - round(a / 10)) <= 1:
                best = max(best or 0, c)
    return best


def best_amount(lines, currency):
    text = "\n".join(lines)
    labelled = _labelled_amounts(lines, currency)
    all_amts = []
    for ln in lines:
        all_amts.extend(_amounts_in(ln, currency))
    supply = None
    for i, ln in enumerate(lines):
        if _SUPPLY_LABEL.search(ln):
            v = _amounts_in(ln, currency)
            if not v and i + 1 < len(lines):
                v = _amounts_in(lines[i + 1], currency)
            if v:
                supply = v[0]
                break

    # 카카오T: 운행 요금 결제 + 부가서비스 요금 결제 합산
    if "부가서비스" in text and currency == "KRW":
        idx = next(i for i, ln in enumerate(lines) if "부가서비스" in ln)
        before = [v for ln in lines[:idx] if "결제" in ln or "금액" in ln for v in krw_amounts(ln)]
        after = []
        for j in range(idx, min(idx + 6, len(lines))):
            after = krw_amounts(lines[j])
            if after:
                break
        if before and after:
            return before[-1] + after[0], 0.85, supply

    # 택시 미터기: 요금총액 — 다른 …원 금액과 함께 가장 큰 값
    if re.search(r"요금\s*총|total\s*fare", text, re.I) and currency == "KRW":
        won = [v for ln in lines for v in krw_amounts(ln) if not _NOT_TOTAL.search(ln)]
        if won:
            return max(won), 0.85, supply

    if labelled:
        strong = [t for t in labelled if t[0]]
        pool = strong or labelled
        # 합계 항목이 여러 개면 가장 큰 값(최종 결제 합계)
        v = max(t[2] for t in pool)
        conf = 0.92 if strong else 0.8
        trip = _vat_triplet(all_amts)
        if trip and abs(trip - v) > 0.5 and trip > v:
            conf = 0.7
        return v, conf, supply

    trip = _vat_triplet(all_amts)
    if trip:
        return trip, 0.8, supply
    if all_amts:
        return max(all_amts), 0.55, supply
    return None, 0.0, supply


def is_cancel(text: str) -> bool:
    t = _compact(text)
    return bool(re.search(r"취소전표|승인취소|거래취소|취소영수증|매출취소|결제취소|refund|cancell?ed", t, re.I))


# ------------------------------------------------------------------ 카드번호

_CARD_FULL = re.compile(
    r"(?<![\d*])([\d*]{4})\s*[- ]\s*([\d*\"″'xX#•]{2,4})\s*[- ]\s*([\d*xX#•]{4})\s*[- ]\s*([\d*xX#•]{3,4})(?![\d*])")
_CARD_TAIL = re.compile(
    r"(?:\*{2,}|x{2,}|X{2,}|•{2,}|·{2,})[\s-]*(\d{3}[\d*]|\d{4})(?![\d])")
_CARD_EN = re.compile(r"(?:ending in|ends with|card\s*(?:no\.?|number)?\s*[:#]?\s*[x*•]+)\s*(\d{4})", re.I)
_CARD_BAD = re.compile(r"전표|사업자|가맹점\s*번호|승인\s*번호|전화|tel|fax|대표|주문|거래\s*번호|등록|차량|단말", re.I)
_CARD_LABEL = re.compile(r"카드|card|결제\s*수단|visa|master|amex", re.I)


def find_card(lines):
    """(끝 4자리, 앞 4자리, 신뢰도)."""
    for strict in (True, False):
        for ln in lines:
            if _CARD_BAD.search(ln):
                continue
            if strict and not _CARD_LABEL.search(ln):
                continue
            m = _CARD_FULL.search(ln)
            if m:
                g1, g4 = m.group(1), m.group(4)
                g4 = re.sub(r"[xX#•]", "*", g4)
                if len(g4) == 3:
                    g4 = g4 + "*"
                # 숫자·하이픈뿐인 전표번호는 제외 (가린 자리가 하나도 없으면)
                full = "".join(m.groups())
                if re.fullmatch(r"\d+", full) and not _CARD_LABEL.search(ln):
                    continue
                first = g1 if re.fullmatch(r"\d{4}", g1) and not re.fullmatch(r"\d{16}", full) else ""
                if re.fullmatch(r"\d{16}", full):
                    first = g1
                if re.fullmatch(r"[\d*]{4}", g4) and re.search(r"\d", g4):
                    return g4, first, 0.9 if strict else 0.75
            m = _CARD_EN.search(ln)
            if m:
                return m.group(1), "", 0.9
            m = _CARD_TAIL.search(ln)
            if m and (strict or _CARD_LABEL.search(ln) is None):
                return m.group(1), "", 0.85 if strict else 0.7
    # 값 모양으로 판단: 258* 처럼 숫자3+* 단독
    for ln in lines:
        if _CARD_BAD.search(ln):
            continue
        m = re.search(r"(?<![\d*])(\d{3}\*)(?![\d*])", ln)
        if m:
            return m.group(1), "", 0.6
    return "", "", 0.0


_KAKAO_CARD_ALIAS = re.compile(r"(?:결제\s*수단|카드)\s*[:：]?\s*([가-힣A-Za-z]{2,10})\s*(\d{4})(?!\d)")


def find_card_alias_first4(lines):
    """카카오T처럼 '카드 별칭 + 숫자 4자리'(나래법인 4289) → 앞 4자리."""
    for ln in lines:
        m = _KAKAO_CARD_ALIAS.search(ln)
        if m and "번호" not in m.group(1):
            return m.group(2), m.group(1)
    return "", ""


# ------------------------------------------------------------------ 기타 항목

_APPROVAL = re.compile(r"승\s*인\s*번\s*호\s*[:：]?\s*([0-9]{6,12})|approval\s*(?:no|code|#)\.?\s*[:：]?\s*([0-9A-Z]{4,12})", re.I)

CARD_COMPANIES_KO = ["신한", "KB국민", "국민", "삼성", "현대", "롯데", "하나", "우리", "비씨", "BC", "NH농협", "농협",
                     "씨티", "카카오뱅크", "케이뱅크", "IBK기업", "기업", "수협", "광주", "전북", "제주", "토스"]
CARD_COMPANIES_EN = {"visa": "Visa", "mastercard": "Mastercard", "master": "Mastercard", "amex": "Amex",
                     "american express": "Amex", "jcb": "JCB", "unionpay": "UnionPay", "discover": "Discover"}


def find_approval(lines):
    for i, ln in enumerate(lines):
        m = _APPROVAL.search(ln)
        if m:
            return (m.group(1) or m.group(2)), 0.9
    # 값 모양: 24004912/ 같은 8자리 + 슬래시
    for ln in lines:
        m = re.search(r"(?<!\d)(\d{8})/(?!\d)", ln)
        if m:
            return m.group(1), 0.6
    return "", 0.0


def find_card_company(lines):
    for ln in lines:
        if re.search(r"카드사|발급사|매입사|카드\s*종류|카드\s*명", ln):
            for c in CARD_COMPANIES_KO:
                if c in ln:
                    return _norm_company(c), 0.9
    text = "\n".join(lines)
    for c in CARD_COMPANIES_KO:
        if re.search(rf"{c}\s*(카드|체크|card)", text, re.I):
            return _norm_company(c), 0.8
    for k, v in CARD_COMPANIES_EN.items():
        if re.search(rf"\b{k}\b", text, re.I):
            return v, 0.8
    return "", 0.0


def _norm_company(c):
    return {"KB국민": "국민", "BC": "비씨", "NH농협": "농협", "IBK기업": "기업"}.get(c, c)


# 결재자·서명 칸의 사람 이름
_APPROVER_LABEL = re.compile(r"(결\s*재\s*자|결\s*제\s*자|서\s*명\s*자|서\s*명|카드\s*소지자|소\s*지\s*자|사\s*용\s*자|이용자|성명)\s*[:：]?\s*(.*)")
NAME_STOPWORDS = {"서명", "본인", "고객", "법인", "회사", "카드", "사용자", "결제자", "결재자", "소지자", "이용자",
                  "성명", "확인", "님", "귀하", "대표", "고객님", "회원", "개인", "법인카드", "서명란", "결제", "승인"}


def is_person_name(s: str) -> bool:
    s = (s or "").strip()
    if not re.fullmatch(r"[가-힣]{2,4}", s):
        return False
    if s in NAME_STOPWORDS or any(w in s for w in ("법인", "회사", "주식", "카드", "서명", "고객", "본인")):
        return False
    return True


def find_approver(lines):
    for i, ln in enumerate(lines):
        m = _APPROVER_LABEL.search(ln)
        if not m:
            continue
        rest = m.group(2).strip()
        cand = re.split(r"[\s(/,]", rest)[0] if rest else ""
        if not cand and i + 1 < len(lines):
            cand = re.split(r"[\s(/,]", lines[i + 1].strip())[0]
        cand = cand.rstrip("님")
        if is_person_name(cand):
            return cand
    return ""


# ------------------------------------------------------------------ 가맹점

_MERCHANT_LABEL = re.compile(
    r"(가\s*맹\s*점\s*명|가\s*맹\s*점|상\s*호\s*명|상\s*호|판\s*매\s*자|판매처|사용처|이용\s*가맹점|merchant|store)\s*[:：]?\s*(.*)",
    re.I)
_MERCHANT_SKIP = re.compile(r"번호|주소|전화|tel|대표|사업자|no\.?$", re.I)
_HEADER_WORDS = re.compile(
    r"^(카드\s*)?(영수증|매출\s*전표|신용\s*카드|전표|receipt|invoice|거래\s*명세서|거래내역서|고객용|가맹점용|승인|"
    r"카드\s*매출|매출전표|신용승인|현금영수증|cash receipt)", re.I)
_CAR_PLATE = re.compile(r"(?<![\d])\d{2,3}\s?[가-힣]\s?\d{4}(?!\d)")


def detect_form(text: str) -> str:
    """양식으로 정하는 가맹점 이름. 순서: 택시 이용 상세 → 개인택시 → 택시 미터기 → 카카오T 흔적."""
    c = _compact(text)
    if "택시이용상세" in c:
        return "카카오택시"
    if re.search(r"가맹점\s*[:：]?\s*개인\s*택시", text) or "개인택시" in c:
        return "개인택시"
    if re.search(r"total\s*fare", text, re.I) or ("요금총" in c and ("탑승" in c or _CAR_PLATE.search(text))):
        return "개인택시"
    if re.search(r"카카오\s*T|카카오모빌리티", text) and "택시" in c:
        return "카카오택시"
    return ""


def find_merchant(lines):
    text = "\n".join(lines)
    form = detect_form(text)
    if form:
        return form, 0.85
    for i, ln in enumerate(lines):
        m = _MERCHANT_LABEL.search(ln)
        if not m:
            continue
        if re.search(r"가\s*맹\s*점\s*번\s*호|가맹점\s*no|상호\s*번호", ln, re.I):
            # '가맹점번호'는 가맹점명이 아님. 같은 줄에 '가맹점명'이 따로 있으면 그것을 씀
            if not re.search(r"가\s*맹\s*점\s*명", ln):
                continue
        val = m.group(2).strip()
        if not val and i + 1 < len(lines):
            val = lines[i + 1].strip()
        val = re.split(r"\s{2,}|\s(?:사업자|대표|전화|tel|가맹점번호)", val, flags=re.I)[0].strip(" :：")
        if not val or re.fullmatch(r"[\d\-\s]+", val):
            continue
        if _CAR_PLATE.fullmatch(val.replace(" ", "")):
            return "개인택시", 0.8
        return val, 0.88
    # 매출전표 상호 줄: 사업자등록번호 왼쪽
    for ln in lines:
        m = re.search(r"^(.{2,30}?)\s+\d{3}-\d{2}-\d{5}", ln)
        if m and not _HEADER_WORDS.search(m.group(1)):
            return m.group(1).strip(), 0.6
    for i, ln in enumerate(lines):
        if re.search(r"\d{3}-\d{2}-\d{5}", ln) and i > 0:
            prev = lines[i - 1]
            if re.search(r"[가-힣A-Za-z]{2,}", prev) and not _HEADER_WORDS.search(prev) and not _MERCHANT_SKIP.search(prev):
                return prev.strip(), 0.55
    # 영문 영수증: 맨 위의 회사 이름 (Anthropic, PBC)
    for ln in lines[:6]:
        if _HEADER_WORDS.search(ln) or re.search(r"\d{2,}", ln) or len(ln) > 40:
            continue
        if re.search(r"[A-Za-z가-힣]{2,}", ln):
            return ln.strip(), 0.5
    return "", 0.0


# ------------------------------------------------------------------ 업종

_LODGING = re.compile(r"호텔|모텔|펜션|리조트|숙박|여기어때|야놀자|게스트\s*하우스|hotel|resort|inn\b|\d\s*박|airbnb|에어비앤비|콘도", re.I)
_FOOD = re.compile(
    r"식당|음식|카페|커피|coffee|cafe|주점|호프|치킨|피자|레스토랑|restaurant|베이커리|제과|분식|국밥|김밥|고기|갈비|횟집|"
    r"스타벅스|이디야|투썸|메가커피|빽다방|맥도날드|버거|롯데리아|테이블\s*[:：]|소주|맥주|펩시|콜라|사이다|공기밥|메뉴|"
    r"한식|중식|일식|양식|족발|보쌈|냉면|칼국수|짬뽕|짜장|초밥|라멘|돈까스|삼겹|막걸리|와인|bar\b|pub\b|bistro|diner", re.I)


def detect_category(text: str, merchant: str = "") -> str:
    t = f"{merchant}\n{text}"
    if _LODGING.search(t):
        return "숙박"
    if _FOOD.search(t):
        return "음식점"
    return "기타"


# ------------------------------------------------------------------ POS 바코드 날짜

def pos_barcode_date(lines, today=None):
    """날짜를 못 읽으면 POS 바코드 번호 앞 YYMMDD."""
    today = today or _dt.date.today()
    for ln in lines:
        m = re.search(r"(?<!\d)(\d{2})(\d{2})(\d{2})\d{6,14}(?!\d)", ln)
        if m:
            v = _valid_date(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if v and 2000 <= int(v[:4]) <= today.year:
                return v
    return ""


# ------------------------------------------------------------------ 진입점

def extract_from_text(text: str, base_conf: float = 1.0, today=None) -> ReceiptData:
    """텍스트에서 영수증 항목을 뽑는다. base_conf로 신뢰도 상한을 정한다(OCR은 0.6)."""
    lines = _lines(text)
    d = ReceiptData(raw_text=text)
    cap = lambda c: min(c, base_conf) if c else 0.0  # noqa: E731

    date, c, date_line = best_date(lines, today)
    if not date:
        date = pos_barcode_date(lines, today)
        c = 0.5 if date else 0.0
    d.set("date", date, cap(c))

    t, c = best_time(lines, date_line)
    d.set("time", t, cap(c))

    cur, c = detect_currency(text)
    d.set("currency", cur, cap(c))

    amt, c, supply = best_amount(lines, cur)
    if amt is not None and is_cancel(text) and amt > 0:
        amt = -amt
    d.set("amount", amt, cap(c))
    d.supply_amount = supply

    merchant, c = find_merchant(lines)
    d.set("merchant", merchant, cap(c))

    last4, first4, c = find_card(lines)
    d.set("card_last4", last4, cap(c))
    if first4:
        d.set("card_first4", first4, cap(c))
    else:
        alias_first, _alias = find_card_alias_first4(lines)
        if alias_first and not last4:
            d.set("card_first4", alias_first, cap(0.8))

    ap, c = find_approval(lines)
    d.set("approval_no", ap, cap(c))
    cc, c = find_card_company(lines)
    d.set("card_company", cc, cap(c))

    d.approver_name = find_approver(lines)
    d.category = detect_category(text, merchant)
    d.confidence["category"] = cap(0.8)
    return d
