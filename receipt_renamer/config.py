"""설정과 사용자 매핑표(%APPDATA%\\receipt_renamer)."""
from __future__ import annotations

import csv
import io
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TEMPLATE = "{결제일}_{사용자}_{금액원화}_{가맹점}"
OLD_DEFAULT_TEMPLATES = ("{결제일}_{사용자}_{금액}원_{가맹점}",)

DEFAULT_SETTINGS = {
    "template": DEFAULT_TEMPLATE,
    "engine": "offline",          # ai / offline
    "output_mode": "copy",        # copy / inplace
    "output_dir": "",             # 비어 있으면 영수증 폴더
    "keep_dates": False,          # 사본 파일 날짜를 원본과 같게
    "ai_consent": False,          # 외부 전송 동의
    "api_key": "",
    "split_pdf": True,            # 여러 페이지 PDF를 페이지별로 나누기
    "review_only": False,
    "magnifier_zoom": 2.5,
    "model": "claude-opus-5-5",
}


def app_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        d = Path(base) / "receipt_renamer"
    else:
        d = Path.home() / ".receipt_renamer"
    d.mkdir(parents=True, exist_ok=True)
    return d


def settings_path() -> Path:
    return app_dir() / "settings.json"


def users_path() -> Path:
    return app_dir() / "users.csv"


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update(json.loads(settings_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    if s.get("template") in OLD_DEFAULT_TEMPLATES:
        s["template"] = DEFAULT_TEMPLATE
    return s


def save_settings(s: dict) -> None:
    settings_path().write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


def get_api_key(settings: dict) -> str:
    return (settings.get("api_key") or os.environ.get("ANTHROPIC_API_KEY") or "").strip()


# ---------------------------------------------------------------- 매핑표

@dataclass
class MappingEntry:
    pattern: str      # '2580', '845*' (끝 4자리) 또는 '4289' (앞 4자리)
    name: str
    kind: str         # 'last' / 'first'


_SPLIT = re.compile(r"\s*[,\t，、;]\s*|\s+")


def parse_mapping_line(line: str):
    """한 줄을 (카드번호, 이름)으로. 알아볼 수 없으면 None."""
    line = line.strip().lstrip("﻿")
    if not line or line.startswith("#"):
        return None
    parts = [p for p in _SPLIT.split(line, maxsplit=1) if p]
    if len(parts) != 2:
        raise ValueError(line)
    card, name = parts[0].strip(), parts[1].strip().strip(",")
    if card in ("카드번호", "card") and name in ("이름", "name"):
        return None
    card = card.replace("-", "").replace(" ", "")
    if re.fullmatch(r"\d{4}\*{4,12}", card):
        return MappingEntry(card[:4], name, "first")
    if re.fullmatch(r"[\d*]{4}", card):
        return MappingEntry(card, name, "last")
    if re.fullmatch(r"[\d*]{5,19}", card):
        return MappingEntry(card[-4:], name, "last")
    raise ValueError(line)


def parse_mapping_text(text: str) -> tuple[list[MappingEntry], list[str]]:
    entries, bad = [], []
    for line in text.splitlines():
        try:
            e = parse_mapping_line(line)
        except ValueError:
            bad.append(line.strip())
            continue
        if e:
            entries.append(e)
    return entries, bad


def mapping_to_text(entries: list[MappingEntry]) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    for e in entries:
        w.writerow([e.pattern + ("****" if e.kind == "first" else ""), e.name])
    return out.getvalue()


def load_mapping() -> list[MappingEntry]:
    p = users_path()
    if not p.exists():
        return []
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = p.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return []
    entries, _ = parse_mapping_text(text)
    return entries


def save_mapping(entries: list[MappingEntry]) -> None:
    users_path().write_text(mapping_to_text(entries), encoding="utf-8-sig")


def upsert_mapping(entries: list[MappingEntry], last4: str, name: str) -> list[MappingEntry]:
    out = [e for e in entries if not (e.kind == "last" and e.pattern == last4)]
    out.append(MappingEntry(last4, name, "last"))
    return out
