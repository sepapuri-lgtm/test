"""추출 결과 검증·정리: 가맹점 표기, 사용자 판정(매핑표), 부가세 제외 금액."""
from __future__ import annotations

import datetime as _dt
import re
from typing import Optional

from .config import MappingEntry
from .models import ReceiptData, ReceiptItem
from .rules import ZERO_DECIMAL, is_person_name

# ------------------------------------------------------------------ 가맹점

_CORP = re.compile(
    r"\(\s*주\s*\)|㈜|\(\s*유\s*\)|\(\s*사\s*\)|주식\s*회사|유한\s*회사|유한책임회사|사단법인|재단법인|"
    r",?\s*\b(inc|llc|l\.l\.c|pbc|ltd|limited|corp|corporation|co\.,?\s*ltd|co|gmbh|plc|s\.a|pte)\b\.?",
    re.I)

_MERCHANT_UNIFY = [
    (re.compile(r"^개인\s+택시"), "개인택시"),
    (re.compile(r"^개인택시\s*\[?\d{6,12}\]?$"), "개인택시"),
    (re.compile(r"^(카카오\s*T|카카오\s*모빌리티|kakao\s*t|카카오T블루|카카오\s*T\s*블루)$", re.I), "카카오택시"),
]


def clean_merchant(name: str) -> str:
    if not name:
        return ""
    s = name.strip()
    s = _CORP.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,.-_·:：")
    for pat, rep in _MERCHANT_UNIFY:
        if pat.search(s):
            s = rep
            break
    # 가맹점번호 같은 숫자만 남으면 버림
    if re.fullmatch(r"[\d\-\s]+", s):
        return ""
    return s


# ------------------------------------------------------------------ 사용자

def _match_last4(pattern: str, last4: str) -> Optional[bool]:
    """정확히 일치 True, *때문에 일치 False(근사), 불일치 None."""
    if len(pattern) != 4 or len(last4) != 4:
        return None
    exact = True
    for a, b in zip(pattern, last4):
        if a == b and a != "*":
            continue
        if a == "*" or b == "*":
            exact = False
            continue
        return None
    return exact


def lookup_user(mapping: list[MappingEntry], last4: str, first4: str = "", ocr_fuzzy: bool = False):
    """(이름, 신뢰도). 후보가 둘 이상이면 틀린 이름을 넣지 않도록 비운다."""
    if last4:
        exact = [e for e in mapping if e.kind == "last" and _match_last4(e.pattern, last4) is True]
        names = {e.name for e in exact}
        if len(names) == 1:
            return names.pop(), 1.0
        if len(names) > 1:
            return "", 0.0
        loose = [e for e in mapping if e.kind == "last" and _match_last4(e.pattern, last4) is False]
        names = {e.name for e in loose}
        if len(names) == 1:
            return names.pop(), 0.85
        if len(names) > 1:
            return "", 0.0
    if first4:
        cands = {e.name for e in mapping if e.kind == "first" and e.pattern == first4}
        if len(cands) == 1:
            return cands.pop(), 0.7
        if not cands and ocr_fuzzy:
            near = {e.name for e in mapping if e.kind == "first"
                    and sum(a != b for a, b in zip(e.pattern, first4)) == 1}
            if len(near) == 1:
                return near.pop(), 0.5
    return "", 0.0


_NOT_A_NAME = re.compile(r"법인|회사|organization|'s\b|@|주식|inc\b|llc|corp|팀$|부$|본부", re.I)


def resolve_user(item: ReceiptItem, mapping: list[MappingEntry], fuzzy: bool = False) -> None:
    """사용자 판단 우선순위: 사람이 고친 값 > 결재자 이름 > 매핑표(카드번호) > 영수증의 이름."""
    d = item.data
    if "user" in item.manual:
        d.user_source = "manual"
        d.confidence["user"] = 1.0
        return
    if d.approver_name and is_person_name(d.approver_name):
        d.set("user", d.approver_name, max(d.conf("user"), 0.85))
        d.user_source = "approver"
        return
    name, conf = lookup_user(mapping, d.card_last4, d.card_first4, fuzzy)
    if not name and d.card_last4 and d.card_first4:
        # 끝번호로 못 찾으면 앞 4자리로
        name, conf = lookup_user(mapping, "", d.card_first4, fuzzy)
    if name:
        d.set("user", name, conf)
        d.user_source = "mapping"
        return
    # 영수증에서 읽은 이름: 카드 별칭·회사명·이메일은 버림
    u = (d.user or "").strip()
    if u and (_NOT_A_NAME.search(u) or not re.search(r"[가-힣A-Za-z]", u)):
        u = ""
    if u and re.fullmatch(r"[가-힣]{2,4}", u) and not is_person_name(u):
        u = ""
    d.set("user", u, min(d.conf("user"), 0.75) if u else 0.0)
    d.user_source = "receipt" if u else ""


# ------------------------------------------------------------------ 검증

def validate(d: ReceiptData, today: Optional[_dt.date] = None) -> None:
    today = today or _dt.date.today()
    if d.date:
        try:
            dt = _dt.datetime.strptime(d.date, "%Y%m%d").date()
            if dt > today + _dt.timedelta(days=1) or dt.year < 2000:
                d.confidence["date"] = min(d.conf("date"), 0.4)
        except ValueError:
            d.set("date", "", 0.0)
    if d.time and not re.fullmatch(r"([01]\d|2[0-3])[0-5]\d", d.time):
        d.set("time", "", 0.0)
    d.currency = (d.currency or "KRW").upper()
    if d.amount is not None:
        if d.currency in ZERO_DECIMAL:
            d.amount = float(round(d.amount))
        else:
            d.amount = round(float(d.amount), 2)
        if d.amount == 0:
            d.confidence["amount"] = min(d.conf("amount"), 0.4)
    d.merchant = clean_merchant(d.merchant)
    if not d.merchant:
        d.confidence["merchant"] = 0.0
    if d.card_last4:
        c = d.card_last4.replace("x", "*").replace("X", "*")
        d.card_last4 = c if re.fullmatch(r"[\d*]{4}", c) and re.search(r"\d", c) else ""
    if d.card_first4 and not re.fullmatch(r"\d{4}", d.card_first4):
        d.card_first4 = ""
    if d.category not in ("숙박", "음식점", "기타"):
        d.category = "기타"


# ------------------------------------------------------------------ 부가세

def net_amount(d: ReceiptData) -> Optional[float]:
    """파일명·요약에 쓰는 금액. 숙박·음식점 원화 영수증은 부가세를 뺀 공급가액."""
    if d.amount is None:
        return None
    if d.currency == "KRW" and d.category in ("숙박", "음식점"):
        a = d.amount
        if d.supply_amount and 0 < abs(d.supply_amount) < abs(a):
            # 영수증의 공급가액이 결제 금액과 맞으면(부가세 ≈ 10%) 그대로
            vat = abs(a) - abs(d.supply_amount)
            if abs(vat - round(abs(a) / 11)) <= 2 or abs(vat - round(abs(d.supply_amount) / 10)) <= 2:
                return abs(d.supply_amount) * (1 if a >= 0 else -1)
        sign = 1 if a >= 0 else -1
        return sign * (abs(a) - round(abs(a) / 11))
    return d.amount


def vat_excluded(d: ReceiptData) -> bool:
    return d.amount is not None and d.currency == "KRW" and d.category in ("숙박", "음식점")
