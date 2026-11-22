"""데이터 모델: 추출 결과(ReceiptData)와 목록의 한 행(ReceiptItem)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# 신뢰도가 이 값보다 낮은 칸은 '확인 필요'
REVIEW_THRESHOLD = 0.8

# 비어 있으면 실패(주황)로 보는 핵심 칸
CORE_FIELDS = ("date", "amount", "merchant", "user")

FIELD_LABELS = {
    "user": "사용자",
    "date": "결제일",
    "time": "시각",
    "amount": "금액",
    "currency": "통화",
    "merchant": "가맹점",
    "card_last4": "카드끝번호",
    "card_first4": "카드앞번호",
    "approval_no": "승인번호",
    "card_company": "카드사",
    "category": "업종",
}

CATEGORIES = ("기타", "숙박", "음식점")


@dataclass
class ReceiptData:
    user: str = ""
    date: str = ""            # YYYYMMDD
    time: str = ""            # HHMM
    amount: Optional[float] = None   # 최종 결제 합계(부가세 포함). 취소는 음수
    currency: str = "KRW"
    merchant: str = ""
    card_last4: str = ""      # 4자리, 가려진 자리는 *
    card_first4: str = ""
    approval_no: str = ""
    card_company: str = ""
    category: str = "기타"     # 기타 / 숙박 / 음식점
    supply_amount: Optional[float] = None  # 영수증에 찍힌 공급가액(있으면)
    approver_name: str = ""   # 결재자·서명 칸에서 읽은 사람 이름
    user_source: str = ""     # manual / approver / mapping / receipt
    confidence: dict = field(default_factory=dict)
    raw_text: str = ""

    def conf(self, name: str) -> float:
        return float(self.confidence.get(name, 0.0))

    def set(self, name: str, value, conf: float) -> None:
        setattr(self, name, value)
        self.confidence[name] = conf

    def copy(self) -> "ReceiptData":
        import copy
        return copy.deepcopy(self)


@dataclass
class ReceiptItem:
    path: Path                 # 원본 파일
    page: Optional[int] = None  # 여러 페이지 PDF를 나눌 때 0부터
    data: ReceiptData = field(default_factory=ReceiptData)
    status: str = "대기"        # 대기 / 추출 중 / 완료 / 실패 / 적용됨
    error: str = ""
    new_name: str = ""
    manual: set = field(default_factory=set)  # 사람이 고친 칸

    @property
    def display_name(self) -> str:
        if self.page is None:
            return self.path.name
        return f"{self.path.name} (p{self.page + 1})"

    def low_conf_fields(self) -> list[str]:
        out = []
        for name in ("user", "date", "time", "amount", "merchant", "card_last4"):
            if name in self.manual:
                continue
            val = getattr(self.data, name)
            if val in ("", None):
                continue
            if self.data.conf(name) < REVIEW_THRESHOLD:
                out.append(name)
        return out

    def missing_core(self) -> list[str]:
        return [n for n in CORE_FIELDS if getattr(self.data, n) in ("", None)]

    @property
    def row_state(self) -> str:
        """'fail'(주황) / 'review'(노랑) / 'ok'."""
        if self.status == "실패" or (self.status in ("완료", "적용됨") and self.missing_core()):
            return "fail"
        if self.status in ("완료", "적용됨") and self.low_conf_fields():
            return "review"
        return "ok"
