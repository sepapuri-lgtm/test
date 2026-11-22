"""출력: 결과 폴더 만들기, 사본/이름 변경 적용, 되돌리기, 메모장·엑셀 요약."""
from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

from .config import app_dir
from .models import ReceiptItem
from .naming import format_amount, format_time_display, output_ext, render, unique_path
from .normalize import net_amount, vat_excluded

LOG_NAME = "_변경기록.json"


class OutputError(Exception):
    pass


# ------------------------------------------------------------------ 결과 폴더

class OutputSession:
    """같은 목록에서 요약과 적용을 여러 번 하면 같은 결과 폴더를 쓴다."""

    def __init__(self):
        self.folder: Optional[Path] = None

    def reset(self):
        self.folder = None

    def ensure(self, base: Path, now: Optional[_dt.datetime] = None) -> Path:
        if self.folder and self.folder.exists():
            return self.folder
        self.folder = make_result_folder(base, now)
        return self.folder


def make_result_folder(base: Path, now: Optional[_dt.datetime] = None) -> Path:
    now = now or _dt.datetime.now()
    if not base.exists():
        raise OutputError(f"저장 위치를 찾을 수 없습니다: {base}\n드라이브가 연결되어 있는지 확인하세요.")
    name = now.strftime("%Y%m%d-%H%M") + "-변환완료"
    p = base / name
    n = 2
    while p.exists():
        p = base / f"{name}_{n}"
        n += 1
    try:
        p.mkdir(parents=True)
    except PermissionError as e:
        raise OutputError(f"저장 위치에 폴더를 만들 권한이 없습니다: {base}") from e
    return p


def default_base(items: list[ReceiptItem], settings: dict) -> Path:
    if settings.get("output_dir"):
        return Path(settings["output_dir"])
    if not items:
        return Path.home()
    parents = [i.path.parent for i in items]
    try:
        common = Path(os.path.commonpath([str(p) for p in parents]))
    except ValueError:  # 다른 드라이브
        common = parents[0]
    return common


# ------------------------------------------------------------------ 이름 계획

def plan(items: list[ReceiptItem], template: str, mode: str, folder: Optional[Path]):
    """[(item, 대상 경로)] — 실제로 바꾸지는 않는다."""
    taken: set = set()
    out = []
    for seq, it in enumerate(items, 1):
        stem = render(template, it, seq)
        ext = output_ext(it)
        if mode == "inplace" and it.page is None:
            target_dir = it.path.parent
            if stem + ext == it.path.name:
                out.append((it, it.path))
                taken.add(str(it.path).lower())
                continue
        else:
            target_dir = folder
        out.append((it, unique_path(target_dir, stem, ext, taken)))
    return out


def preview_names(items, template):
    taken: set = set()
    for seq, it in enumerate(items, 1):
        stem = render(template, it, seq)
        name = stem + output_ext(it)
        n = 2
        while name.lower() in taken:
            name = f"{stem}_{n}{output_ext(it)}"
            n += 1
        taken.add(name.lower())
        it.new_name = name


# ------------------------------------------------------------------ 파일 날짜

def _set_creation_time_windows(path: Path, ts: float) -> None:  # pragma: no cover - Windows 전용
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    handle = k32.CreateFileW(str(path), 0x100, 0x7, None, 3, 0x02000000, None)  # FILE_WRITE_ATTRIBUTES
    if handle in (-1, 0xFFFFFFFF, None):
        return
    try:
        t = int((ts + 11644473600) * 10_000_000)
        ft = wintypes.FILETIME(t & 0xFFFFFFFF, t >> 32)
        k32.SetFileTime(handle, ctypes.byref(ft), None, None)
    finally:
        k32.CloseHandle(handle)


def copy_dates(src: Path, dst: Path) -> None:
    """사본의 수정한·접근한 날짜를 원본과 같게, 만든 날짜는 원본의 수정한 날짜로."""
    st = src.stat()
    os.utime(dst, (st.st_atime, st.st_mtime))
    if sys.platform == "win32":
        _set_creation_time_windows(dst, st.st_mtime)


def _write_pdf_page(src: Path, page: int, dst: Path) -> None:
    from .loader import fitz
    with fitz.open(src) as doc, fitz.open() as out:
        out.insert_pdf(doc, from_page=page, to_page=page)
        out.save(dst)


# ------------------------------------------------------------------ 적용 / 되돌리기

def apply(items: list[ReceiptItem], settings: dict, session: OutputSession):
    """[변경 적용]. (결과 폴더, 처리 건수, 오류 목록)."""
    mode = settings.get("output_mode", "copy")
    base = default_base(items, settings)
    folder = session.ensure(base)
    planned = plan(items, settings.get("template"), mode, folder)
    log = {"created": _dt.datetime.now().isoformat(timespec="seconds"), "mode": mode, "entries": []}
    errors = []
    done = 0
    for it, target in planned:
        src = it.path
        try:
            if not src.exists():
                raise FileNotFoundError(str(src))
            if it.page is not None:
                _write_pdf_page(src, it.page, target)
                log["entries"].append({"action": "copy", "src": str(src), "dst": str(target)})
            elif mode == "inplace":
                if target != src:
                    os.rename(src, target)
                    log["entries"].append({"action": "rename", "src": str(src), "dst": str(target)})
                    it.path = target
            else:
                shutil.copyfile(src, target)  # 내용은 바이트 단위로 같음, 날짜는 변환 시각
                log["entries"].append({"action": "copy", "src": str(src), "dst": str(target)})
            if settings.get("keep_dates") and it.page is None and mode != "inplace":
                copy_dates(src, target)
            it.status = "적용됨"
            it.new_name = target.name
            done += 1
        except FileNotFoundError:
            errors.append(f"{it.display_name}: 원본 파일이 없습니다.")
        except PermissionError:
            errors.append(f"{it.display_name}: 파일이 다른 프로그램에서 열려 있거나 권한이 없습니다.")
        except OSError as e:
            errors.append(f"{it.display_name}: {e.strerror or e}")
    # 되돌리기 기록: 결과 폴더와 %APPDATA%\receipt_renamer\history 양쪽에
    log_path = folder / LOG_NAME
    _append_log(log_path, log)
    hist = app_dir() / "history"
    hist.mkdir(exist_ok=True)
    _append_log(hist / (folder.name + ".json"), log, folder=str(folder))
    return folder, done, errors


def _append_log(path: Path, batch: dict, folder: Optional[str] = None):
    data = {"folder": folder, "batches": []}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    data["batches"].append(batch)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def last_history() -> Optional[Path]:
    hist = app_dir() / "history"
    if not hist.exists():
        return None
    files = sorted(hist.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if d.get("batches"):
            return f
    return None


def undo_last(history_file: Optional[Path] = None):
    """마지막 배치를 되돌린다. (복원 건수, 오류 목록). 복사 모드에서는 만든 사본만 지운다."""
    history_file = history_file or last_history()
    if not history_file:
        raise OutputError("되돌릴 변경 기록이 없습니다.")
    data = json.loads(history_file.read_text(encoding="utf-8"))
    if not data.get("batches"):
        raise OutputError("되돌릴 변경 기록이 없습니다.")
    batch = data["batches"].pop()
    n, errors = 0, []
    for e in reversed(batch["entries"]):
        src, dst = Path(e["src"]), Path(e["dst"])
        try:
            if e["action"] == "copy":
                if dst.exists():
                    dst.unlink()
                    n += 1
            elif e["action"] == "rename":
                if src.exists():
                    errors.append(f"{src.name}: 같은 이름의 파일이 이미 있어 복원하지 않았습니다.")
                    continue
                os.rename(dst, src)
                n += 1
        except OSError as ex:
            errors.append(f"{dst.name}: {ex.strerror or ex}")
    history_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    folder = data.get("folder")
    if folder:
        log = Path(folder) / LOG_NAME
        try:
            if log.exists():
                d2 = json.loads(log.read_text(encoding="utf-8"))
                if d2.get("batches"):
                    d2["batches"].pop()
                log.write_text(json.dumps(d2, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, ValueError):
            pass
    return n, errors


# ------------------------------------------------------------------ 요약

def _totals(items):
    by_user = defaultdict(lambda: defaultdict(float))
    total = defaultdict(float)
    for it in items:
        d = it.data
        v = net_amount(d)
        if v is None:
            continue
        by_user[d.user or "미확인"][d.currency] += v
        total[d.currency] += v
    return total, by_user


def _fmt_totals(tot: dict) -> str:
    if not tot:
        return "₩0"
    order = sorted(tot, key=lambda c: (c != "KRW", c))
    return " + ".join(format_amount(tot[c], c, "sign") for c in order)


def summary_text(items: list[ReceiptItem]) -> str:
    items = [i for i in items if i.status in ("완료", "적용됨")]
    total, by_user = _totals(items)
    lines = [f"영수증 요약 ({_dt.datetime.now():%Y-%m-%d %H:%M})", ""]
    lines.append(f"전체 {len(items)}건  합계 {_fmt_totals(total)}")
    lines.append("(통화가 다르면 더하지 않습니다)")
    lines.append("")
    lines.append("[사용자별 합계]")
    for u in sorted(by_user):
        cnt = sum(1 for i in items if (i.data.user or "미확인") == u)
        lines.append(f"  {u}: {_fmt_totals(by_user[u])} ({cnt}건)")
    lines.append("")
    lines.append("[결제일순 목록]")
    star = False
    for it in sorted(items, key=lambda i: (i.data.date or "99999999", i.data.time or "")):
        d = it.data
        mark = ""
        if vat_excluded(d):
            mark = " *"
            star = True
        amt = format_amount(net_amount(d), d.currency, "sign") or "미확인"
        dt = d.date and f"{d.date[:4]}-{d.date[4:6]}-{d.date[6:]}" or "미확인"
        lines.append(f"  {dt} {format_time_display(d.time):>9}  {d.user or '미확인':<6} {amt:>14}{mark}  "
                     f"{d.merchant or '미확인'}  ({it.new_name or it.display_name})")
    if star:
        lines.append("")
        lines.append("* 숙박·음식점은 부가세를 뺀 금액(공급가액)입니다.")
    return "\n".join(lines) + "\n"


def write_summary_txt(items, folder: Path) -> Path:
    p = folder / "영수증_요약.txt"
    p.write_text(summary_text(items), encoding="utf-8-sig")
    return p


_EXCEL_FMT = {"KRW": '"₩"#,##0;-"₩"#,##0', "USD": '"$"#,##0.00;-"$"#,##0.00', "EUR": '"€"#,##0.00',
              "GBP": '"£"#,##0.00', "JPY": '"¥"#,##0'}


def _num_fmt(cur):
    return _EXCEL_FMT.get(cur, f'"{cur} "#,##0.00')


def write_summary_xlsx(items, folder: Path) -> Path:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError as e:  # pragma: no cover
        raise OutputError("엑셀 부품(openpyxl)이 설치되어 있지 않습니다.") from e
    items = [i for i in items if i.status in ("완료", "적용됨")]
    wb = Workbook()
    ws = wb.active
    ws.title = "영수증 목록"
    head = ["결제일", "시각", "사용자", "가맹점", "업종", "금액", "결제금액", "통화", "카드끝번호", "카드사", "승인번호",
            "원본 파일", "새 파일명"]
    ws.append(head)
    pink = PatternFill("solid", fgColor="F8C8DC")
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = pink
        c.alignment = Alignment(horizontal="center")
    for it in sorted(items, key=lambda i: (i.data.date or "99999999", i.data.time or "")):
        d = it.data
        date = _dt.datetime.strptime(d.date, "%Y%m%d").date() if d.date else None
        row = [date, format_time_display(d.time), d.user, d.merchant, d.category, net_amount(d), d.amount,
               d.currency, d.card_last4, d.card_company, d.approval_no, it.display_name, it.new_name]
        ws.append(row)
        r = ws.max_row
        if date:
            ws.cell(r, 1).number_format = "yyyy-mm-dd"
        ws.cell(r, 6).number_format = _num_fmt(d.currency)
        ws.cell(r, 7).number_format = _num_fmt(d.currency)
    widths = [12, 10, 10, 24, 8, 14, 14, 6, 10, 10, 12, 30, 44]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = w
    ws.freeze_panes = "A2"

    ws2 = wb.create_sheet("사용자별 합계")
    ws2.append(["사용자", "통화", "건수", "합계"])
    for c in ws2[1]:
        c.font = Font(bold=True)
        c.fill = pink
    counts = defaultdict(int)
    for it in items:
        if net_amount(it.data) is not None:
            counts[(it.data.user or "미확인", it.data.currency)] += 1
    total, by_user = _totals(items)
    for u in sorted(by_user):
        for cur in sorted(by_user[u], key=lambda c: (c != "KRW", c)):
            ws2.append([u, cur, counts[(u, cur)], by_user[u][cur]])
            ws2.cell(ws2.max_row, 4).number_format = _num_fmt(cur)
    for cur in sorted(total, key=lambda c: (c != "KRW", c)):
        ws2.append(["전체", cur, sum(v for (u, c), v in counts.items() if c == cur), total[cur]])
        ws2.cell(ws2.max_row, 1).font = Font(bold=True)
        ws2.cell(ws2.max_row, 4).number_format = _num_fmt(cur)
    for col, w in zip("ABCD", (12, 8, 8, 16)):
        ws2.column_dimensions[col].width = w
    if any(vat_excluded(i.data) for i in items):
        ws.append([])
        ws.append(["* 숙박·음식점의 '금액'은 부가세를 뺀 공급가액, '결제금액'은 부가세 포함 금액입니다."])
    p = folder / "영수증_요약.xlsx"
    try:
        wb.save(p)
    except PermissionError as e:
        raise OutputError("엑셀 파일이 열려 있어 저장할 수 없습니다. 엑셀에서 닫은 뒤 다시 시도하세요.") from e
    return p
