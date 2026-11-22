"""추출 흐름: 파일 → (텍스트층 | 이미지) → 엔진(AI / 오프라인) → 검증·사용자 판정."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from . import ai, loader, ocr
from .config import MappingEntry, get_api_key
from .models import ReceiptData, ReceiptItem
from .normalize import resolve_user, validate
from .rules import extract_from_text

TEXT_PDF_CONF = 0.9


def choose_engine(settings: dict) -> str:
    """'ai' 또는 'offline'. API 키가 없거나 외부 전송에 동의하지 않으면 오프라인."""
    if settings.get("engine") == "ai" and get_api_key(settings) and settings.get("ai_consent"):
        return "ai"
    return "offline"


def extract_data(item: ReceiptItem, settings: dict) -> ReceiptData:
    engine = choose_engine(settings)
    text = loader.item_text_layer(item)
    if engine == "ai":
        key = get_api_key(settings)
        model = settings.get("model") or ai.MODEL
        if text:
            return ai.extract(key, text=text, model=model)
        img = loader.shrink(loader.item_image(item))
        return ai.extract(key, image_jpeg=loader.jpeg_bytes(img), model=model)
    if text:
        return extract_from_text(text, TEXT_PDF_CONF)
    reason = ocr.availability()
    if reason:
        raise RuntimeError(reason)
    return ocr.extract(loader.item_image(item))


def finish(item: ReceiptItem, mapping: list[MappingEntry], fuzzy: bool = False) -> None:
    validate(item.data)
    resolve_user(item, mapping, fuzzy)


def extract_item(item: ReceiptItem, settings: dict, mapping: list[MappingEntry]) -> None:
    item.status = "추출 중"
    item.error = ""
    try:
        data = extract_data(item, settings)
    except FileNotFoundError:
        item.status, item.error = "실패", "파일을 찾을 수 없습니다(옮겨졌거나 지워짐)."
        return
    except ai.AIError as e:
        item.status, item.error = "실패", str(e)
        return
    except Exception as e:  # 손상된 파일 등
        item.status, item.error = "실패", friendly_error(e)
        return
    # 사람이 고친 칸은 유지
    old = item.data
    for f in item.manual:
        setattr(data, f, getattr(old, f))
        data.confidence[f] = 1.0
    item.data = data
    finish(item, mapping)
    item.status = "완료"


def friendly_error(e: Exception) -> str:
    msg = str(e)
    if isinstance(e, PermissionError):
        return "파일에 접근할 수 없습니다(다른 프로그램에서 열려 있거나 권한 없음)."
    if "cannot identify image" in msg:
        return "이미지 파일을 읽을 수 없습니다(손상되었거나 지원하지 않는 형식)."
    if isinstance(e, OSError) and getattr(e, "winerror", None) == 21:
        return "드라이브가 준비되지 않았습니다."
    return msg or e.__class__.__name__


class BatchRunner:
    """최대 4건을 동시에 처리하고, 완료된 행부터 알린다."""

    def __init__(self, items, settings, mapping, on_update: Callable[[ReceiptItem], None],
                 on_done: Callable[[], None], workers: int = 4):
        self.items = items
        self.settings = settings
        self.mapping = mapping
        self.on_update = on_update
        self.on_done = on_done
        self.workers = workers if choose_engine(settings) == "ai" else 1
        self.cancel = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _one(self, item):
        if self.cancel.is_set():
            return
        item.status = "추출 중"
        self.on_update(item)
        extract_item(item, self.settings, self.mapping)
        if self.cancel.is_set() and item.status == "추출 중":
            item.status = "대기"
        self.on_update(item)

    def _run(self):
        try:
            with ThreadPoolExecutor(max_workers=self.workers) as ex:
                list(ex.map(self._one, self.items))
        finally:
            self.on_done()
