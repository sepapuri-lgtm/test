"""AI(Claude) 추출: 영수증 이미지 또는 PDF 텍스트를 보내 JSON으로 받는다."""
from __future__ import annotations

import base64
import json
from typing import Optional

from .models import ReceiptData

MODEL = "claude-opus-5-5"

SCHEMA = {
    "type": "object",
    "properties": {
        "user": {"type": "string", "description": "영수증에 적힌 실제 사용자(사람) 이름. 없으면 빈 문자열"},
        "approver_name": {"type": "string",
                          "description": "결재자·결제자·서명(자)·카드 소지자·사용자 칸의 사람 이름(손글씨 서명 포함). 없으면 빈 문자열"},
        "date": {"type": "string", "description": "결제일 YYYYMMDD. 없으면 빈 문자열"},
        "time": {"type": "string", "description": "승인 시각 HHMM(24시간). 없으면 빈 문자열"},
        "amount": {"type": ["number", "null"], "description": "최종 결제 합계(부가세 포함). 취소는 음수"},
        "currency": {"type": "string", "description": "ISO 통화 코드 (KRW, USD, EUR, JPY, GBP ...)"},
        "supply_amount": {"type": ["number", "null"], "description": "영수증에 찍힌 공급가액. 없으면 null"},
        "merchant": {"type": "string", "description": "가맹점 이름"},
        "category": {"type": "string", "enum": ["숙박", "음식점", "기타"]},
        "card_last4": {"type": "string", "description": "카드번호 끝 4자리. 가려진 자리는 *. 없으면 빈 문자열"},
        "card_first4": {"type": "string", "description": "카드번호 앞 4자리. 없으면 빈 문자열"},
        "approval_no": {"type": "string"},
        "card_company": {"type": "string"},
        "confidence": {
            "type": "object",
            "properties": {k: {"type": "number"} for k in
                           ("user", "date", "time", "amount", "merchant", "card_last4")},
            "required": ["user", "date", "time", "amount", "merchant", "card_last4"],
            "additionalProperties": False,
        },
    },
    "required": ["user", "approver_name", "date", "time", "amount", "currency", "supply_amount", "merchant",
                 "category", "card_last4", "card_first4", "approval_no", "card_company", "confidence"],
    "additionalProperties": False,
}

SYSTEM = """당신은 카드 영수증에서 경비 정산 정보를 뽑는 도구입니다. 주어진 영수증 하나에서 아래 규칙대로 값을 채워 JSON으로만 답하세요.

- date: 승인일자를 우선하고, 없으면 결제·거래·판매일, 그다음 이용일자·발행일시. YYYYMMDD.
- time: 승인(결제·판매) 시각, 24시간 HHMM.
- amount: 최종 결제 합계. 공급가액·부가세가 아닌 합계. 취소 영수증은 음수. 원화·엔화는 정수, 다른 통화는 소수 둘째 자리까지.
  카카오T처럼 운행 요금 결제와 부가서비스 요금 결제(예약비 등)가 따로 있으면 두 결제 금액을 더한 값.
  택시 미터기 영수증은 요금총액(Total Fare).
- currency: 결제 통화 그대로(원화 환산 금지). 기본 KRW.
- merchant: 가맹점 이름. (주)·주식회사·Inc.·LLC·PBC 같은 법인 표기는 빼고 지점명은 유지.
  카카오T '택시 이용 상세' 화면은 "카카오택시", 개인택시 카드단말기·택시 미터기 영수증(가맹점명이 차량번호)은 "개인택시". 가맹점번호는 이름이 아님.
- category: 숙박(호텔·모텔·펜션·리조트·숙박 앱) / 음식점(식당·카페·주점 등) / 기타.
- card_last4: 카드번호 끝 4자리. 가려진 자리는 *로(예: 845*). 전체 카드번호는 절대 쓰지 마세요.
- card_first4: 카드번호 앞 4자리가 보이면. 카드번호 없이 '카드 별칭 + 숫자 4자리'(예: 나래법인 4289)만 있으면 그 4자리.
- user: 영수증에 적힌 사람 이름만. 카드 별칭·회사명·청구처(…'s Organization)·이메일은 이름이 아니므로 빈 문자열.
- approver_name: 결재자·결제자·서명·카드 소지자 칸의 사람 이름(한글 2~4자 등). '서명', '본인', '고객' 같은 안내 글자나 회사명은 제외.
- card_company: 카드사(신한, 국민, 삼성, Visa, Mastercard ...).
- confidence: 각 값에 대한 확신(0~1). 흐리거나 추측한 값은 낮게.
- 값을 찾을 수 없으면 빈 문자열 또는 null."""


class AIError(Exception):
    """사용자에게 보여줄 한글 메시지를 담는다."""


def _client(api_key: str):
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover
        raise AIError("AI 부품(anthropic)이 설치되어 있지 않습니다.") from e
    return anthropic.Anthropic(api_key=api_key, timeout=120.0, max_retries=2)


def _content_for(image_jpeg: Optional[bytes], text: Optional[str]):
    if text:
        return [{"type": "text", "text": "다음은 영수증 PDF에서 뽑은 텍스트입니다.\n\n" + text[:20000]}]
    data = base64.standard_b64encode(image_jpeg).decode("ascii")
    return [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
        {"type": "text", "text": "이 영수증의 값을 뽑아 주세요."},
    ]


def _call(client, model: str, content):
    import anthropic
    kwargs = dict(
        model=model,
        max_tokens=4000,
        system=SYSTEM,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
    )
    try:
        # 거절(refusal) 시 서버측 대체 모델로 다시 시도
        return client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    except anthropic.BadRequestError as e:
        if "fallback" not in str(e).lower():
            raise
        return client.messages.create(**kwargs)


def _korean_error(e: Exception) -> str:
    import anthropic
    if isinstance(e, anthropic.AuthenticationError):
        return "API 키가 올바르지 않습니다. [설정]에서 API 키를 확인하세요."
    if isinstance(e, anthropic.PermissionDeniedError):
        msg = str(e).lower()
        if "credit" in msg or "billing" in msg:
            return "API 크레딧이 부족합니다. Anthropic 콘솔에서 결제 정보를 확인하세요."
        return "이 API 키로는 모델을 쓸 수 없습니다(403). 회사 네트워크·키 권한을 확인하세요."
    if isinstance(e, anthropic.RateLimitError):
        return "요청이 너무 많습니다. 잠시 후 다시 시도하세요."
    if isinstance(e, anthropic.BadRequestError):
        msg = str(e).lower()
        if "credit" in msg or "balance" in msg:
            return "API 크레딧이 부족합니다. Anthropic 콘솔에서 결제 정보를 확인하세요."
        return f"요청 오류: {getattr(e, 'message', e)}"
    if isinstance(e, anthropic.APIStatusError):
        if e.status_code >= 500:
            return f"AI 서버 오류({e.status_code}). 잠시 후 다시 시도하세요."
        return f"AI 오류({e.status_code}): {getattr(e, 'message', e)}"
    if isinstance(e, anthropic.APIConnectionError):
        return "인터넷에 연결할 수 없습니다. 네트워크를 확인하거나 오프라인 OCR을 쓰세요."
    return f"AI 추출 오류: {e}"


def extract(api_key: str, image_jpeg: Optional[bytes] = None, text: Optional[str] = None,
            model: str = MODEL) -> ReceiptData:
    import anthropic
    client = _client(api_key)
    try:
        resp = _call(client, model, _content_for(image_jpeg, text))
    except anthropic.APIError as e:
        raise AIError(_korean_error(e)) from e
    if resp.stop_reason == "refusal":
        raise AIError("AI가 이 영수증 처리를 거절했습니다. 오프라인 OCR로 다시 시도하세요.")
    if resp.stop_reason == "max_tokens":
        raise AIError("AI 응답이 잘렸습니다. 다시 시도하세요.")
    raw = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")
    try:
        j = json.loads(raw)
    except ValueError as e:
        raise AIError("AI 응답을 읽을 수 없습니다.") from e
    return from_json(j)


def from_json(j: dict) -> ReceiptData:
    d = ReceiptData()
    conf = j.get("confidence") or {}
    for k in ("user", "date", "time", "merchant", "card_last4", "card_first4", "approval_no", "card_company"):
        v = str(j.get(k) or "").strip()
        setattr(d, k, v)
    d.date = "".join(ch for ch in d.date if ch.isdigit())[:8]
    d.time = "".join(ch for ch in d.time if ch.isdigit())[:4]
    a = j.get("amount")
    d.amount = float(a) if isinstance(a, (int, float)) else None
    s = j.get("supply_amount")
    d.supply_amount = float(s) if isinstance(s, (int, float)) else None
    d.currency = (j.get("currency") or "KRW").upper()
    d.category = j.get("category") or "기타"
    d.approver_name = str(j.get("approver_name") or "").strip()
    for k in ("user", "date", "time", "amount", "merchant", "card_last4"):
        try:
            d.confidence[k] = max(0.0, min(1.0, float(conf.get(k, 0.9))))
        except (TypeError, ValueError):
            d.confidence[k] = 0.9
    d.confidence["currency"] = d.confidence.get("amount", 0.9)
    d.confidence["category"] = 0.9
    return d


def test_connection(api_key: str, model: str = MODEL) -> str:
    """연결 테스트. 성공하면 '' , 실패하면 한글 메시지."""
    import anthropic
    try:
        client = _client(api_key)
        client.messages.create(model=model, max_tokens=16, output_config={"effort": "low"},
                               messages=[{"role": "user", "content": "ping"}])
        return ""
    except anthropic.APIError as e:
        return _korean_error(e)
    except AIError as e:
        return str(e)
