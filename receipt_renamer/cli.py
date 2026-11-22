"""명령줄: 인자 없이 실행하면 GUI. --selftest로 부품·연결·추출 점검."""
from __future__ import annotations

import argparse
import csv
import io
import sys

from . import VERSION


def _selftest(settings) -> int:
    from . import ai, loader, ocr
    from .config import get_api_key
    from .rules import extract_from_text
    ok = True

    def line(name, err):
        nonlocal ok
        print(f"[{'OK' if not err else '실패'}] {name}" + (f" - {err}" if err else ""))
        ok = ok and not err

    print(f"영수증 파일명 변환기 v{VERSION} 자가 점검")
    for mod, label in (("PIL", "Pillow"), ("pymupdf", "PyMuPDF(PDF)"), ("openpyxl", "openpyxl(엑셀)"),
                       ("anthropic", "anthropic(AI)")):
        try:
            __import__(mod)
            line(label, "")
        except Exception as e:
            line(label, str(e))
    line("pillow-heif(HEIC)", "" if loader.HEIC_OK else "설치되지 않음")
    try:
        import tkinterdnd2  # noqa: F401
        line("tkinterdnd2(드래그 앤 드롭)", "")
    except Exception as e:
        line("tkinterdnd2(드래그 앤 드롭)", str(e))
    d = extract_from_text("가맹점명: 스타벅스 강남점\n승인일시 2026-10-07 15:01\n합계 45,000원\n카드번호 1234-****-****-5678")
    line("규칙 추출", "" if (d.date, d.amount, d.card_last4) == ("20261007", 45000, "5678") else f"결과 이상: {d}")
    reason = ocr.selftest()
    line("Windows 오프라인 OCR", reason or "")
    key = get_api_key(settings)
    if key:
        line("Claude API 연결", ai.test_connection(key, settings.get("model") or ai.MODEL))
    else:
        print("[건너뜀] Claude API 연결 - API 키 없음")
    return 0 if ok else 1


def _run_extract(paths, settings):
    from .config import load_mapping
    from .extractor import extract_item
    from .loader import collect, make_items
    res = collect(paths)
    items = make_items(res.files, settings.get("split_pdf", True))
    mapping = load_mapping()
    for it in items:
        extract_item(it, settings, mapping)
        print(f"  {it.display_name}: {it.status} {it.error}", file=sys.stderr)
    return items


def _csv(items) -> str:
    from .naming import format_amount, format_time_display
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["원본", "상태", "결제일", "시각", "사용자", "금액", "가맹점", "카드끝번호", "새 파일명", "오류"])
    for it in items:
        d = it.data
        w.writerow([it.display_name, it.status, d.date, format_time_display(d.time), d.user,
                    format_amount(d.amount, d.currency), d.merchant, d.card_last4, it.new_name, it.error])
    return out.getvalue()


def _show_message(title, text):
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(title, text)
        root.destroy()
    except Exception:
        pass


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # 창 모드 exe(콘솔 없음)에서는 출력을 모아 대화상자로 보여준다
    windowed = sys.stdout is None
    if windowed:
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
    p = argparse.ArgumentParser(prog="ReceiptRenamer", description="카드 영수증 파일명 자동 변환기")
    p.add_argument("--version", action="version", version=f"v{VERSION}")
    p.add_argument("--selftest", action="store_true", help="부품·연결·추출 점검")
    sub = p.add_subparsers(dest="cmd")
    pe = sub.add_parser("extract", help="추출 결과를 CSV로 미리보기")
    pe.add_argument("paths", nargs="+")
    pe.add_argument("--engine", choices=["ai", "offline"])
    pe.add_argument("-o", "--out", help="CSV 저장 경로")
    pa = sub.add_parser("apply", help="추출 후 이름 변경 적용 + 요약")
    pa.add_argument("paths", nargs="+")
    pa.add_argument("--engine", choices=["ai", "offline"])
    pa.add_argument("--mode", choices=["copy", "inplace"])
    pa.add_argument("--template")
    sub.add_parser("undo", help="마지막 변경 되돌리기")
    sub.add_parser("gui", help="창 열기(기본)")
    args = p.parse_args(argv)

    from .config import load_settings
    settings = load_settings()

    if args.selftest:
        if windowed:
            rc = _selftest(settings)
            _show_message("자가 점검", sys.stdout.getvalue())
            return rc
        return _selftest(settings)
    if args.cmd in (None, "gui"):
        from .gui import run
        return run()
    if getattr(args, "engine", None):
        settings["engine"] = args.engine
        if args.engine == "ai":
            settings["ai_consent"] = True  # 명령줄에서 직접 고른 경우
    if args.cmd == "extract":
        from .output import preview_names
        items = _run_extract(args.paths, settings)
        preview_names([i for i in items if i.status == "완료"], settings["template"])
        text = _csv(items)
        if args.out:
            with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
                f.write(text)
        else:
            sys.stdout.write(text)
        return 0
    if args.cmd == "apply":
        from .output import OutputSession, apply, write_summary_txt, write_summary_xlsx
        if args.mode:
            settings["output_mode"] = args.mode
        if args.template:
            settings["template"] = args.template
        items = [i for i in _run_extract(args.paths, settings) if i.status == "완료"]
        if not items:
            print("적용할 영수증이 없습니다.", file=sys.stderr)
            return 1
        session = OutputSession()
        folder, n, errors = apply(items, settings, session)
        write_summary_txt(items, folder)
        try:
            write_summary_xlsx(items, folder)
        except Exception as e:
            errors.append(str(e))
        print(f"{n}건 적용 → {folder}")
        for e in errors:
            print("  " + e, file=sys.stderr)
        return 0 if not errors else 2
    if args.cmd == "undo":
        from .output import OutputError, undo_last
        try:
            n, errors = undo_last()
        except OutputError as e:
            print(e, file=sys.stderr)
            return 1
        print(f"{n}건 되돌림")
        for e in errors:
            print("  " + e, file=sys.stderr)
        return 0
    return 0
