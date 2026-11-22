import os
from pathlib import Path

import pytest

from receipt_renamer import output
from receipt_renamer.config import DEFAULT_SETTINGS
from receipt_renamer.extractor import extract_item
from receipt_renamer.loader import collect, make_items


@pytest.fixture(autouse=True)
def appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))


def _text_pdf(path: Path, pages: list[str]):
    fitz = pytest.importorskip("pymupdf")
    doc = fitz.open()
    for text in pages:
        pg = doc.new_page()
        pg.insert_text((50, 72), text, fontsize=11)
    doc.save(path)


RECEIPT = "Acme Coffee Inc.\nDate paid October 7, 2026\nVisa ending in 8452\nAmount paid $22.00\n"


def test_end_to_end_copy_undo(tmp_path):
    src = tmp_path / "receipts"
    src.mkdir()
    pdf = src / "scan0003.pdf"
    _text_pdf(pdf, [RECEIPT])
    (src / "notes.txt").write_text("x")
    old = os.stat(pdf).st_mtime - 86400 * 30
    os.utime(pdf, (old, old))

    res = collect([src])
    assert res.skipped_unsupported == 1
    items = make_items(res.files)
    settings = dict(DEFAULT_SETTINGS, engine="offline", keep_dates=True)
    for it in items:
        extract_item(it, settings, [])
    it = items[0]
    assert it.status == "완료", it.error
    assert it.data.amount == 22.0 and it.data.currency == "USD"
    it.data.user = "김민배"

    session = output.OutputSession()
    folder, n, errors = output.apply(items, settings, session)
    assert n == 1 and not errors
    assert folder.name.endswith("-변환완료") and folder.parent == src
    out = folder / "20261007_김민배_$22.00_Acme Coffee.pdf"
    assert out.exists()
    assert out.read_bytes() == pdf.read_bytes()
    assert abs(out.stat().st_mtime - old) < 2
    txt = output.write_summary_txt(items, folder)
    assert "$22.00" in txt.read_text(encoding="utf-8-sig")
    output.write_summary_xlsx(items, folder)
    assert (folder / "영수증_요약.xlsx").exists()

    # 결과 폴더는 다시 넣어도 건너뜀
    again = collect([src])
    assert len(again.files) == 1 and again.skipped_result_dir >= 1

    n, errs = output.undo_last()
    assert n == 1 and not out.exists() and pdf.exists()


def test_inplace_and_duplicates(tmp_path):
    src = tmp_path / "r"
    src.mkdir()
    for name in ("a.pdf", "b.pdf"):
        _text_pdf(src / name, [RECEIPT])
    items = make_items(collect([src]).files)
    settings = dict(DEFAULT_SETTINGS, engine="offline", output_mode="inplace")
    for it in items:
        extract_item(it, settings, [])
    folder, n, errors = output.apply(items, settings, output.OutputSession())
    names = sorted(p.name for p in src.glob("*.pdf"))
    assert names == ["20261007_미확인_$22.00_Acme Coffee.pdf", "20261007_미확인_$22.00_Acme Coffee_2.pdf"]
    output.undo_last()
    assert sorted(p.name for p in src.glob("*.pdf")) == ["a.pdf", "b.pdf"]


def test_multipage_split(tmp_path):
    pdf = tmp_path / "multi.pdf"
    _text_pdf(pdf, [RECEIPT, RECEIPT.replace("22.00", "5.50")])
    items = make_items([pdf], split_pdf=True)
    assert [i.page for i in items] == [0, 1]
    settings = dict(DEFAULT_SETTINGS, engine="offline")
    for it in items:
        extract_item(it, settings, [])
    assert [i.data.amount for i in items] == [22.0, 5.5]
    folder, n, _ = output.apply(items, settings, output.OutputSession())
    assert n == 2 and len(list(folder.glob("*.pdf"))) == 2
