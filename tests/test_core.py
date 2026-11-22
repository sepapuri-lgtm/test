import datetime as dt
from pathlib import Path

import pytest

from receipt_renamer.config import MappingEntry, parse_mapping_line, parse_mapping_text
from receipt_renamer.models import ReceiptData, ReceiptItem
from receipt_renamer.naming import (format_amount, format_time_display, parse_amount_input, parse_time_input,
                                    render, sanitize)
from receipt_renamer.normalize import clean_merchant, lookup_user, net_amount, resolve_user, validate
from receipt_renamer.rules import extract_from_text

TODAY = dt.date(2026, 11, 22)
TPL = "{결제일}_{사용자}_{금액원화}_{가맹점}"


def item_with(data, name="IMG_4821.jpg"):
    it = ReceiptItem(path=Path(name), data=data)
    it.status = "완료"
    return it


# ------------------------------------------------------------------ 규칙 추출

STARBUCKS = """신용카드 매출전표
가맹점명: (주)스타벅스코리아 강남점
사업자번호 123-45-67890
카드번호 1234-****-****-5678
승인일시 2026-10-07 15:01:22
승인번호 30012345
공급가액 40,909
부가세 4,091
합계 45,000원
"""


def test_korean_card_receipt():
    d = extract_from_text(STARBUCKS, today=TODAY)
    assert d.date == "20261007"
    assert d.time == "1501"
    assert d.amount == 45000
    assert d.currency == "KRW"
    assert d.card_last4 == "5678"
    assert d.approval_no == "30012345"
    assert d.supply_amount == 40909
    validate(d, TODAY)
    assert d.merchant == "스타벅스코리아 강남점"
    assert d.category == "음식점"


ANTHROPIC = """Anthropic, PBC
Receipt
Date paid September 4, 2026
Visa •••• 8452
Amount paid $22.00
"""


def test_english_usd_receipt():
    d = extract_from_text(ANTHROPIC, today=TODAY)
    validate(d, TODAY)
    assert d.date == "20260904"
    assert d.currency == "USD"
    assert d.amount == 22.0
    assert d.card_last4 == "8452"
    assert d.merchant == "Anthropic"
    assert d.card_company == "Visa"


def test_masked_last_digit_and_approval_priority():
    t = "이용일자 2026.10.01\n승인일자 2026.10.02 오후 2:32\n카드 4289-0993-****-258*\n결제금액 28,300원"
    d = extract_from_text(t, today=TODAY)
    assert d.date == "20261002"
    assert d.time == "1432"
    assert d.card_last4 == "258*"
    assert d.card_first4 == "4289"
    assert d.amount == 28300


def test_vat_triplet_without_total_label():
    t = "호텔 그랜드\n145,455\n14,545\n160,000\n2026-10-05"
    d = extract_from_text(t, today=TODAY)
    assert d.amount == 160000
    assert d.category == "숙박"


def test_kakao_extra_service_sum():
    t = "택시 이용 상세\n2026.10.21 08:10\n결제 금액 8,000원\n부가서비스 요금\n블루파트너스 예약비\n결제 금액 3,000원\n결제수단 나래법인 4289"
    d = extract_from_text(t, today=TODAY)
    assert d.amount == 11000
    assert d.merchant == "카카오택시"
    assert d.card_first4 == "4289"
    assert d.card_last4 == ""


def test_taxi_meter():
    t = "36바4377\n탑승 2026-10-28 22:10\n통행료 1,500원\n요금총액(Total Fare):\n23,300원"
    d = extract_from_text(t, today=TODAY)
    assert d.merchant == "개인택시"
    assert d.amount == 23300


def test_cancel_negative_and_ocr_won():
    t = "승인취소\n2026-10-03\n합계 3,200워"
    d = extract_from_text(t, today=TODAY)
    assert d.amount == -3200


def test_future_year_and_bad_month_filtered():
    t = "2096-10-01\n2026-16-28\n합계 10,000원"
    d = extract_from_text(t, today=TODAY)
    assert d.date == "20261028"


def test_merchant_number_is_not_name():
    t = "가맹점번호 123456789\n상호: 김밥천국 역삼점\n합계 7,000원"
    d = extract_from_text(t, today=TODAY)
    assert d.merchant == "김밥천국 역삼점"


def test_approver_name():
    t = "결재자: 박지민\n합계 10,000원"
    d = extract_from_text(t, today=TODAY)
    assert d.approver_name == "박지민"
    t2 = "서명 본인\n합계 10,000원"
    assert extract_from_text(t2, today=TODAY).approver_name == ""


# ------------------------------------------------------------------ 매핑표·사용자

def test_mapping_parse_formats():
    assert parse_mapping_line("2580,김민배") == MappingEntry("2580", "김민배", "last")
    assert parse_mapping_line("4289****\t김민배") == MappingEntry("4289", "김민배", "first")
    assert parse_mapping_line("845* 홍길동") == MappingEntry("845*", "홍길동", "last")
    assert parse_mapping_line("1234，이순신") == MappingEntry("1234", "이순신", "last")
    entries, bad = parse_mapping_text("2580,김민배\n이상한줄\n\n카드번호,이름")
    assert len(entries) == 1 and bad == ["이상한줄"]


def test_lookup_rules():
    m = [MappingEntry("8451", "가", "last"), MappingEntry("8457", "나", "last"),
         MappingEntry("2580", "김민배", "last"), MappingEntry("4289", "김민배", "first")]
    assert lookup_user(m, "2580") == ("김민배", 1.0)
    assert lookup_user(m, "258*") == ("김민배", 0.85)
    assert lookup_user(m, "845*") == ("", 0.0)   # 후보 둘 → 비움
    assert lookup_user(m, "", "4289") == ("김민배", 0.7)
    assert lookup_user(m, "", "4283", ocr_fuzzy=True) == ("김민배", 0.5)


def test_user_priority():
    m = [MappingEntry("2580", "김민배", "last")]
    d = ReceiptData(user="나래법인", card_last4="2580")
    it = item_with(d)
    resolve_user(it, m)
    assert it.data.user == "김민배"
    d = ReceiptData(user="", card_last4="2580", approver_name="박지민")
    it = item_with(d)
    resolve_user(it, m)
    assert it.data.user == "박지민"
    d = ReceiptData(user="홍길동", card_last4="2580")
    it = item_with(d)
    it.manual.add("user")
    resolve_user(it, m)
    assert it.data.user == "홍길동"
    d = ReceiptData(user="Acme's Organization")
    it = item_with(d)
    resolve_user(it, [])
    assert it.data.user == ""


# ------------------------------------------------------------------ 정리·금액

@pytest.mark.parametrize("raw,clean", [
    ("(주)스타벅스코리아", "스타벅스코리아"), ("주식회사 카카오", "카카오"), ("Anthropic, PBC", "Anthropic"),
    ("Acme Inc.", "Acme"), ("개인 택시", "개인택시"), ("카카오T", "카카오택시"), ("카카오모빌리티", "카카오택시"),
    ("123-456", ""),
])
def test_clean_merchant(raw, clean):
    assert clean_merchant(raw) == clean


def test_vat_excluded():
    d = ReceiptData(amount=160000, currency="KRW", category="숙박")
    assert net_amount(d) == 145455
    d = ReceiptData(amount=188100, currency="KRW", category="음식점")
    assert net_amount(d) == 171000
    d = ReceiptData(amount=160000, currency="KRW", category="숙박", supply_amount=145455)
    assert net_amount(d) == 145455
    d = ReceiptData(amount=22.0, currency="USD", category="음식점")
    assert net_amount(d) == 22.0


def test_format_amount():
    assert format_amount(45000, "KRW") == "₩45,000"
    assert format_amount(22, "USD") == "$22.00"
    assert format_amount(9.9, "CHF") == "CHF 9.90"
    assert format_amount(-3200, "KRW") == "-₩3,200"
    assert format_amount(45000, "KRW", "plain") == "45000"
    assert format_amount(1234.5, "USD", "comma") == "1,234.50"


def test_time_parsing():
    assert parse_time_input("오후 2:32") == "1432"
    assert parse_time_input("14:32") == "1432"
    assert parse_time_input("1432") == "1432"
    assert parse_time_input("2:32 PM") == "1432"
    assert parse_time_input("오전 12:05") == "0005"
    assert parse_time_input("25:00") is None
    assert format_time_display("1432") == "오후 2:32"


def test_amount_input():
    assert parse_amount_input("$22.00") == (22.0, "USD")
    assert parse_amount_input("22 달러") == (22.0, "USD")
    assert parse_amount_input("45,000") == (45000.0, "KRW")
    assert parse_amount_input("CHF 9.90") == (9.9, "CHF")


# ------------------------------------------------------------------ 파일명

def test_render_examples():
    d = ReceiptData(date="20261007", user="홍길동", amount=45000, currency="KRW", merchant="스타벅스")
    assert render(TPL, item_with(d)) == "20261007_홍길동_₩45,000_스타벅스"
    d = ReceiptData(date="20260904", user="김민배", amount=22.0, currency="USD", merchant="Anthropic")
    assert render(TPL, item_with(d, "a.pdf")) == "20260904_김민배_$22.00_Anthropic"
    d = ReceiptData(date="20261007")
    assert render(TPL, item_with(d)) == "20261007_미확인_미확인_미확인"


def test_sanitize():
    assert sanitize('a\\b/c:d*e?f"g<h>i|j') == "abcdefghij"
    assert sanitize("₩45,000 $1") == "₩45,000 $1"
