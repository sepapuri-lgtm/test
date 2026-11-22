"""GUI: 폴더 → 추출 → 검토 → 이름 변경. tkinter + tkinterdnd2(드래그 앤 드롭)."""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import APP_NAME, VERSION, ai, loader, ocr
from .config import (DEFAULT_TEMPLATE, get_api_key, load_mapping, load_settings, mapping_to_text,
                     parse_mapping_text, save_mapping, save_settings, upsert_mapping)
from .extractor import BatchRunner, choose_engine, finish
from .models import CATEGORIES, ReceiptItem
from .naming import (TEMPLATE_VARS, format_amount, format_time_display, parse_amount_input,
                     parse_time_input)
from .normalize import vat_excluded
from .output import (OutputError, OutputSession, apply, default_base, preview_names, undo_last,
                     write_summary_txt, write_summary_xlsx)

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_OK = True
except Exception:  # pragma: no cover
    DND_OK = False

# ------------------------------------------------------------------ 분홍 테마
PINK = "#F7D6E4"
PINK_DARK = "#D96C9B"
PINK_DEEP = "#B8457A"
PINK_LIGHT = "#FDF0F6"
YELLOW = "#FFF4B8"
ORANGE = "#FFC58A"
TEXT = "#3A2230"

COLUMNS = [
    ("status", "상태", 70), ("orig", "원본", 170), ("date", "결제일", 110), ("time", "시각", 90),
    ("user", "사용자", 70), ("amount", "금액", 160), ("merchant", "가맹점", 150), ("card", "카드끝번호", 85),
    ("new", "새 파일명", 330),
]

FORM_FIELDS = [
    ("user", "사용자"), ("date", "결제일 (YYYYMMDD)"), ("time", "시각"), ("amount", "금액"),
    ("merchant", "가맹점"), ("card_last4", "카드 끝번호"), ("category", "업종"), ("card_company", "카드사"),
    ("approval_no", "승인번호"),
]


def _open_file(p: Path):
    try:
        if sys.platform == "win32":
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except OSError:
        pass


class App:
    def __init__(self):
        self.root = TkinterDnD.Tk() if DND_OK else tk.Tk()
        self.root.title(f"{APP_NAME} v{VERSION}")
        self.root.geometry("1400x860")
        self.root.minsize(1000, 600)
        self.settings = load_settings()
        self.mapping = load_mapping()
        self.items: list[ReceiptItem] = []
        self.session = OutputSession()
        self.runner: BatchRunner | None = None
        self.queue: queue.Queue = queue.Queue()
        self.selected: ReceiptItem | None = None
        self.preview_src = None   # 원본 해상도 이미지
        self.preview_scale = 1.0
        self.preview_offset = (0, 0)
        self.zoom = float(self.settings.get("magnifier_zoom", 2.5))
        self._style()
        self._build()
        if DND_OK:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self._on_drop)
        self.root.after(100, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------ 화면 구성
    def _style(self):
        self.root.configure(bg=PINK_LIGHT)
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        font = ("Malgun Gothic", 10) if sys.platform == "win32" else ("TkDefaultFont", 10)
        self.font = font
        st.configure(".", background=PINK_LIGHT, foreground=TEXT, font=font)
        st.configure("TFrame", background=PINK_LIGHT)
        st.configure("Bar.TFrame", background=PINK)
        st.configure("TLabel", background=PINK_LIGHT)
        st.configure("Bar.TLabel", background=PINK)
        st.configure("TButton", background="#FFFFFF", bordercolor=PINK_DARK, padding=(8, 4))
        st.map("TButton", background=[("active", PINK), ("disabled", "#E6E0E3")],
               foreground=[("disabled", "#9A8F95")])
        st.configure("Accent.TButton", background=PINK_DARK, foreground="white", font=(font[0], 10, "bold"))
        st.map("Accent.TButton", background=[("active", PINK_DEEP), ("disabled", "#C9BFC4")])
        st.configure("TRadiobutton", background=PINK)
        st.configure("TCheckbutton", background=PINK)
        st.configure("Treeview", rowheight=26, fieldbackground="white", background="white")
        st.configure("Treeview.Heading", background=PINK, font=(font[0], 10, "bold"))
        st.map("Treeview", background=[("selected", PINK_DARK)], foreground=[("selected", "white")])
        st.configure("Warn.TLabel", foreground="#C05000", background=PINK_LIGHT)

    def _build(self):
        top = ttk.Frame(self.root, style="Bar.TFrame", padding=6)
        top.pack(fill="x")
        ttk.Button(top, text="📄 파일 추가", command=self.add_files).pack(side="left", padx=2)
        ttk.Button(top, text="📁 폴더 추가", command=self.add_folder).pack(side="left", padx=2)
        ttk.Button(top, text="목록 비우기", command=self.clear).pack(side="left", padx=2)
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(top, text="추출 방식", style="Bar.TLabel").pack(side="left")
        self.engine_var = tk.StringVar(value=self.settings.get("engine", "offline"))
        ttk.Radiobutton(top, text="AI (Claude)", value="ai", variable=self.engine_var,
                        command=self._engine_changed).pack(side="left", padx=2)
        ttk.Radiobutton(top, text="오프라인 OCR", value="offline", variable=self.engine_var,
                        command=self._engine_changed).pack(side="left", padx=2)
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        self.btn_extract = ttk.Button(top, text="▶ 추출 시작", style="Accent.TButton", command=self.start_extract)
        self.btn_extract.pack(side="left", padx=2)
        self.btn_stop = ttk.Button(top, text="멈추기", command=self.stop_extract, state="disabled")
        self.btn_stop.pack(side="left", padx=2)
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(top, text="✔ 변경 적용", style="Accent.TButton", command=self.apply_changes).pack(side="left", padx=2)
        ttk.Button(top, text="↶ 되돌리기", command=self.undo).pack(side="left", padx=2)

        bar2 = ttk.Frame(self.root, style="Bar.TFrame", padding=(6, 0, 6, 6))
        bar2.pack(fill="x")
        ttk.Button(bar2, text="⚙ 설정", command=self.open_settings).pack(side="right", padx=2)
        ttk.Button(bar2, text="사용자 매핑표", command=self.open_mapping).pack(side="right", padx=2)
        ttk.Separator(bar2, orient="vertical").pack(side="right", fill="y", padx=6)
        ttk.Button(bar2, text="엑셀 요약", command=self.summary_xlsx).pack(side="right", padx=2)
        ttk.Button(bar2, text="메모장 요약", command=self.summary_txt).pack(side="right", padx=2)
        ttk.Label(bar2, text="파일명 규칙", style="Bar.TLabel").pack(side="left")
        self.template_var = tk.StringVar(value=self.settings.get("template", DEFAULT_TEMPLATE))
        ent = ttk.Entry(bar2, textvariable=self.template_var, width=46)
        ent.pack(side="left", padx=4)
        self.template_var.trace_add("write", lambda *_: self._template_changed())
        var_menu = ttk.Menubutton(bar2, text="변수 넣기")
        m = tk.Menu(var_menu, tearoff=0)
        for v in TEMPLATE_VARS:
            m.add_command(label=v, command=lambda v=v: ent.insert("insert", v))
        m.add_separator()
        m.add_command(label="기본값으로", command=lambda: self.template_var.set(DEFAULT_TEMPLATE))
        var_menu["menu"] = m
        var_menu.pack(side="left", padx=2)
        bar3 = ttk.Frame(self.root, style="Bar.TFrame", padding=(6, 0, 6, 6))
        bar3.pack(fill="x")
        ttk.Label(bar3, text="출력", style="Bar.TLabel").pack(side="left")
        self.mode_var = tk.StringVar(value=self.settings.get("output_mode", "copy"))
        ttk.Radiobutton(bar3, text="별도 폴더에 복사", value="copy", variable=self.mode_var,
                        command=self._mode_changed).pack(side="left", padx=2)
        ttk.Radiobutton(bar3, text="제자리에서 이름 변경", value="inplace", variable=self.mode_var,
                        command=self._mode_changed).pack(side="left", padx=2)
        ttk.Button(bar3, text="저장 위치…", command=self.choose_output_dir).pack(side="left", padx=4)
        self.outdir_label = ttk.Label(bar3, style="Bar.TLabel")
        self.outdir_label.pack(side="left")
        self.review_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar3, text="확인 필요만 보기", variable=self.review_var,
                        command=self.refresh_table).pack(side="right")
        self._update_outdir_label()

        pane = ttk.PanedWindow(self.root, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=6, pady=6)

        left = ttk.Frame(pane)
        pane.add(left, weight=3)
        self.tree = ttk.Treeview(left, columns=[c[0] for c in COLUMNS], show="headings", selectmode="browse")
        for key, title, w in COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=w, anchor="w", stretch=key in ("new", "merchant", "orig"))
        self.tree.tag_configure("review", background=YELLOW)
        self.tree.tag_configure("fail", background=ORANGE)
        vs = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(left, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        self.drop_hint = tk.Label(left, text="여기에 영수증 파일이나 폴더를 끌어다 놓으세요\n(JPG·PNG·HEIC·WEBP·BMP·PDF)",
                                  bg="white", fg=PINK_DEEP, font=(self.font[0], 14))
        self.drop_hint.place(relx=0.5, rely=0.45, anchor="center")

        right = ttk.Frame(pane, padding=(6, 0, 0, 0))
        pane.add(right, weight=2)
        self.canvas = tk.Canvas(right, bg="white", highlightthickness=1, highlightbackground=PINK_DARK, height=260)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._draw_preview())
        self.canvas.bind("<Motion>", self._magnify)
        self.canvas.bind("<Leave>", lambda e: self.canvas.delete("mag"))
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Button-4>", lambda e: self._wheel(e, 120))
        self.canvas.bind("<Button-5>", lambda e: self._wheel(e, -120))
        self.mag_label = ttk.Label(right, text=f"돋보기: 마우스를 올리면 확대, 휠로 배율 조절 ({self.zoom:.1f}배)")
        self.mag_label.pack(anchor="w", pady=(2, 6))

        form = ttk.Frame(right)
        form.pack(fill="x")
        self.form_vars: dict[str, tk.StringVar] = {}
        self.form_warn: dict[str, ttk.Label] = {}
        for r, (key, label) in enumerate(FORM_FIELDS):
            ttk.Label(form, text=label).grid(row=r, column=0, sticky="w", pady=2)
            var = tk.StringVar()
            if key == "category":
                w = ttk.Combobox(form, textvariable=var, values=list(CATEGORIES), state="readonly", width=30)
                w.bind("<<ComboboxSelected>>", lambda e, k=key: self._commit(k))
            else:
                w = ttk.Entry(form, textvariable=var, width=32)
                w.bind("<Return>", lambda e, k=key: self._commit(k))
                w.bind("<FocusOut>", lambda e, k=key: self._commit(k))
            w.grid(row=r, column=1, sticky="ew", padx=4, pady=2)
            warn = ttk.Label(form, text="", style="Warn.TLabel", width=12)
            warn.grid(row=r, column=2, sticky="w")
            self.form_vars[key] = var
            self.form_warn[key] = warn
        form.columnconfigure(1, weight=1)
        self.new_name_label = ttk.Label(right, text="", wraplength=480, foreground=PINK_DEEP)
        self.new_name_label.pack(anchor="w", pady=(6, 0))
        self.error_label = ttk.Label(right, text="", wraplength=480, style="Warn.TLabel")
        self.error_label.pack(anchor="w")

        self.status = tk.StringVar(value="영수증을 끌어다 놓거나 [파일 추가]/[폴더 추가]를 누르세요.")
        tk.Label(self.root, textvariable=self.status, anchor="w", bg=PINK, fg=TEXT, padx=8).pack(fill="x", side="bottom")

    # ------------------------------------------------------------ 입력
    def _on_drop(self, event):
        paths = self.root.tk.splitlist(event.data)
        if not paths:
            messagebox.showwarning(APP_NAME, "끌어놓은 항목이 파일이 아닙니다.\n카카오톡·메일 첨부는 먼저 PC에 저장한 뒤 끌어오세요.")
            return
        self.add_paths(paths)

    def add_files(self):
        fs = filedialog.askopenfilenames(title="영수증 파일 선택", filetypes=[
            ("영수증", "*.jpg *.jpeg *.png *.heic *.heif *.webp *.bmp *.pdf"), ("모든 파일", "*.*")])
        if fs:
            self.add_paths(fs)

    def add_folder(self):
        d = filedialog.askdirectory(title="영수증 폴더 선택")
        if d:
            self.add_paths([d])

    def add_paths(self, paths):
        res = loader.collect(paths, existing={str(i.path) for i in self.items})
        new = loader.make_items(res.files, self.settings.get("split_pdf", True))
        self.items.extend(new)
        if new:
            self.session.reset()  # 영수증을 새로 넣으면 다음에 새 결과 폴더
        msgs = []
        if res.skipped_unsupported:
            msgs.append(f"지원하지 않는 형식 {res.skipped_unsupported}개 건너뜀")
        if res.skipped_result_dir:
            msgs.append(f"결과 폴더 안의 사본 {res.skipped_result_dir}개 건너뜀")
        if res.skipped_duplicate:
            msgs.append(f"이미 목록에 있음 {res.skipped_duplicate}개")
        if res.not_found:
            msgs.append(f"찾을 수 없음 {res.not_found}개")
        if not new:
            messagebox.showinfo(APP_NAME, "추가된 영수증이 없습니다.\n" + ("\n".join(msgs) or "지원 형식 파일이 없습니다."))
        self.status.set(f"{len(new)}건 추가 (전체 {len(self.items)}건)" + (" · " + ", ".join(msgs) if msgs else ""))
        self.refresh_table()

    def clear(self):
        if self.runner:
            return
        self.items.clear()
        self.session.reset()
        self.selected = None
        self.refresh_table()
        self._show_detail(None)

    def remove_selected(self):
        it = self._selected_item()
        if it and not self.runner:
            self.items.remove(it)
            self.refresh_table()
            self._show_detail(None)

    # ------------------------------------------------------------ 표
    def _row_values(self, it: ReceiptItem):
        d = it.data
        warn = set(it.low_conf_fields())
        w = lambda k, s: ("⚠ " + s) if (k in warn and s) else s  # noqa: E731
        amt = format_amount(d.amount, d.currency) if d.amount is not None else ""
        if vat_excluded(d):
            from .normalize import net_amount
            amt = format_amount(net_amount(d), d.currency) + " (VAT제외)"
        date = f"{d.date[:4]}-{d.date[4:6]}-{d.date[6:]}" if len(d.date) == 8 else d.date
        return (it.status, it.display_name, w("date", date), w("time", format_time_display(d.time)),
                w("user", d.user), w("amount", amt), w("merchant", d.merchant), w("card_last4", d.card_last4),
                it.new_name if it.status in ("완료", "적용됨") else "")

    def refresh_table(self):
        preview_names([i for i in self.items if i.status in ("완료", "적용됨")], self.template_var.get())
        sel = self._selected_item()
        self.tree.delete(*self.tree.get_children())
        for idx, it in enumerate(self.items):
            if self.review_var.get() and it.row_state == "ok":
                continue
            tag = it.row_state if it.row_state != "ok" else ""
            self.tree.insert("", "end", iid=str(idx), values=self._row_values(it), tags=(tag,))
        if sel and sel in self.items:
            iid = str(self.items.index(sel))
            if self.tree.exists(iid):
                self.tree.selection_set(iid)
        if self.items:
            self.drop_hint.place_forget()
        else:
            self.drop_hint.place(relx=0.5, rely=0.45, anchor="center")
        n_rev = sum(1 for i in self.items if i.row_state == "review")
        n_fail = sum(1 for i in self.items if i.row_state == "fail")
        if self.items and not self.runner:
            self.status.set(f"전체 {len(self.items)}건 · 확인 필요 {n_rev}건 · 빈 칸/실패 {n_fail}건")

    def _update_row(self, it: ReceiptItem):
        if it not in self.items:
            return
        iid = str(self.items.index(it))
        preview_names([i for i in self.items if i.status in ("완료", "적용됨")], self.template_var.get())
        if self.tree.exists(iid):
            tag = it.row_state if it.row_state != "ok" else ""
            self.tree.item(iid, values=self._row_values(it), tags=(tag,))
            for other in self.items:  # 같은 이름 _2 처리로 다른 행도 바뀔 수 있음
                oid = str(self.items.index(other))
                if other is not it and self.tree.exists(oid):
                    self.tree.set(oid, "new", other.new_name if other.status in ("완료", "적용됨") else "")
        else:
            self.refresh_table()

    def _selected_item(self):
        sel = self.tree.selection()
        if not sel:
            return None
        idx = int(sel[0])
        return self.items[idx] if idx < len(self.items) else None

    def _on_select(self, _e=None):
        it = self._selected_item()
        if it is not self.selected:
            self._show_detail(it)

    # ------------------------------------------------------------ 상세 칸
    def _show_detail(self, it):
        self.selected = it
        for k, var in self.form_vars.items():
            var.set("")
            self.form_warn[k].configure(text="")
        self.new_name_label.configure(text="")
        self.error_label.configure(text="")
        self.preview_src = None
        if not it:
            self._draw_preview()
            return
        self._fill_form(it)
        try:
            self.preview_src = loader.item_image(it)
        except Exception as e:
            self.error_label.configure(text=f"미리보기를 열 수 없습니다: {e}")
        self._draw_preview()

    def _fill_form(self, it):
        d = it.data
        vals = {
            "user": d.user, "date": d.date, "time": format_time_display(d.time),
            "amount": format_amount(d.amount, d.currency) if d.amount is not None else "",
            "merchant": d.merchant, "card_last4": d.card_last4, "category": d.category,
            "card_company": d.card_company, "approval_no": d.approval_no,
        }
        low = set(it.low_conf_fields())
        for k, v in vals.items():
            self.form_vars[k].set(v or "")
            if k in low:
                self.form_warn[k].configure(text=f"⚠ {int(d.conf(k) * 100)}%")
            elif k in ("user", "date", "amount", "merchant") and not v and it.status in ("완료", "적용됨"):
                self.form_warn[k].configure(text="⚠ 비어 있음")
            else:
                self.form_warn[k].configure(text="")
        extra = ""
        if vat_excluded(d):
            from .normalize import net_amount
            extra = f"\n파일명 금액은 부가세를 뺀 {format_amount(net_amount(d), d.currency)} (결제 {format_amount(d.amount, d.currency)})"
        self.new_name_label.configure(text=(f"새 파일명: {it.new_name}" if it.new_name else "") + extra)
        self.error_label.configure(text=it.error)

    def _commit(self, key):
        it = self.selected
        if not it or it.status == "추출 중":
            return
        d = it.data
        raw = self.form_vars[key].get().strip()
        before = getattr(d, key)
        if key == "date":
            v = "".join(ch for ch in raw if ch.isdigit())
            if v and len(v) != 8:
                messagebox.showwarning(APP_NAME, "결제일은 20261007처럼 8자리로 입력하세요.")
                return
            new = v
        elif key == "time":
            v = parse_time_input(raw)
            if v is None:
                messagebox.showwarning(APP_NAME, "시각은 오후 2:32, 14:32, 1432 처럼 입력하세요.")
                return
            new = v
        elif key == "amount":
            r = parse_amount_input(raw, d.currency)
            if r is None or (raw and r[0] is None):
                messagebox.showwarning(APP_NAME, "금액은 45,000 / $22.00 / 22 달러 처럼 입력하세요.")
                return
            new, cur = r
            if raw and cur != d.currency:
                d.currency = cur
                it.manual.add("currency")
            elif raw and new == d.amount:
                new = d.amount
        elif key == "card_last4":
            new = raw.replace("x", "*").replace("X", "*")
        else:
            new = raw
        if new == before or (key == "amount" and not raw and before is None):
            return
        setattr(d, key, new)
        d.confidence[key] = 1.0
        it.manual.add(key)
        if it.status == "실패":
            it.status = "완료"
        if key == "user":
            d.user_source = "manual"
            self._propagate_user(it)
        if key == "card_last4" and "user" not in it.manual:
            finish(it, self.mapping)
        self._update_row(it)
        self._fill_form(it)

    def _propagate_user(self, it):
        """같은 카드 끝번호의 사용자를 고치면 매핑표에 저장할지 묻고, 같은 끝번호의 다른 행도 채운다."""
        d = it.data
        last4 = d.card_last4
        if not last4 or not d.user or "*" in last4:
            return
        others = [o for o in self.items if o is not it and o.data.card_last4 == last4 and "user" not in o.manual
                  and o.data.user_source != "approver"]
        if messagebox.askyesno(APP_NAME, f"카드 끝번호 {last4}의 사용자를 '{d.user}'(으)로 매핑표에 저장할까요?"
                               + (f"\n같은 끝번호의 다른 {len(others)}건도 함께 바뀝니다." if others else "")):
            self.mapping = upsert_mapping(load_mapping(), last4, d.user)
            try:
                save_mapping(self.mapping)
            except OSError as e:
                messagebox.showerror(APP_NAME, f"매핑표를 저장하지 못했습니다: {e}")
            for o in others:
                finish(o, self.mapping)
            self.refresh_table()

    # ------------------------------------------------------------ 미리보기·돋보기
    def _draw_preview(self):
        self.canvas.delete("all")
        cw, ch = max(self.canvas.winfo_width(), 10), max(self.canvas.winfo_height(), 10)
        if self.preview_src is None:
            self.canvas.create_text(cw // 2, ch // 2, text="미리보기", fill=PINK_DARK)
            return
        from PIL import ImageTk
        img = self.preview_src
        s = min(cw / img.width, ch / img.height, 1.0)
        self.preview_scale = s
        disp = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))))
        self._tk_img = ImageTk.PhotoImage(disp)
        ox, oy = (cw - disp.width) // 2, (ch - disp.height) // 2
        self.preview_offset = (ox, oy)
        self.canvas.create_image(ox, oy, image=self._tk_img, anchor="nw")

    def _magnify(self, e):
        self.canvas.delete("mag")
        if self.preview_src is None:
            return
        from PIL import Image, ImageTk
        ox, oy = self.preview_offset
        s = self.preview_scale
        x, y = (e.x - ox) / s, (e.y - oy) / s
        img = self.preview_src
        if not (0 <= x < img.width and 0 <= y < img.height):
            return
        box = 220  # 돋보기 창 크기(화면 픽셀)
        # 원본 해상도에서 잘라 확대 (화면 크기 대비 zoom배)
        src_half = box / (2 * self.zoom * s)
        crop = img.crop((int(x - src_half), int(y - src_half), int(x + src_half), int(y + src_half)))
        crop = crop.resize((box, box), Image.LANCZOS)
        self._mag_img = ImageTk.PhotoImage(crop)
        cw = self.canvas.winfo_width()
        mx = e.x + 20 if e.x + 20 + box < cw else e.x - 20 - box
        my = max(0, min(e.y - box // 2, self.canvas.winfo_height() - box))
        self.canvas.create_rectangle(mx - 2, my - 2, mx + box + 2, my + box + 2, outline=PINK_DEEP, width=2, tags="mag")
        self.canvas.create_image(mx, my, image=self._mag_img, anchor="nw", tags="mag")

    def _wheel(self, e, delta=None):
        d = delta if delta is not None else e.delta
        self.zoom = min(8.0, max(1.5, self.zoom + (0.5 if d > 0 else -0.5)))
        self.settings["magnifier_zoom"] = self.zoom
        self.mag_label.configure(text=f"돋보기: 마우스를 올리면 확대, 휠로 배율 조절 ({self.zoom:.1f}배)")
        self._magnify(e)

    # ------------------------------------------------------------ 추출
    def _engine_changed(self):
        eng = self.engine_var.get()
        if eng == "ai":
            if not get_api_key(self.settings):
                key = simpledialog.askstring(APP_NAME, "Claude API 키를 입력하세요 (sk-ant-…)\n[설정]에서 바꿀 수 있습니다.",
                                             show="•", parent=self.root)
                if not key:
                    self.engine_var.set("offline")
                    return
                self.settings["api_key"] = key.strip()
            if not self.settings.get("ai_consent"):
                ok = messagebox.askyesno(
                    "외부 전송 동의",
                    "AI 추출을 쓰면 영수증 이미지(또는 PDF 텍스트)가 Anthropic의 Claude API로 전송됩니다.\n"
                    "회사 정책상 외부 전송이 허용되는지 확인하세요.\n\n동의하고 AI 추출을 사용할까요?\n"
                    "(동의하지 않으면 오프라인 OCR로 추출합니다)")
                if not ok:
                    self.engine_var.set("offline")
                    self.settings["engine"] = "offline"
                    save_settings(self.settings)
                    return
                self.settings["ai_consent"] = True
        else:
            reason = ocr.availability()
            if reason:
                messagebox.showwarning(APP_NAME, reason + "\n\n텍스트 PDF는 OCR 없이 읽을 수 있습니다.")
        prev = self.settings.get("engine")
        self.settings["engine"] = eng
        save_settings(self.settings)
        done = [i for i in self.items if i.status in ("완료", "실패")]
        if prev != eng and done and not self.runner:
            if messagebox.askyesno(APP_NAME, f"이미 추출한 {len(done)}건을 새 방식으로 다시 추출할까요?\n(직접 고친 칸은 유지됩니다)"):
                for i in done:
                    i.status = "대기"
                self.start_extract()

    def start_extract(self):
        if self.runner:
            return
        todo = [i for i in self.items if i.status in ("대기", "실패")]
        if not todo:
            if not self.items:
                messagebox.showinfo(APP_NAME, "먼저 영수증을 추가하세요.")
            else:
                if messagebox.askyesno(APP_NAME, "모든 영수증을 이미 추출했습니다. 전부 다시 추출할까요?"):
                    todo = [i for i in self.items if i.status != "적용됨"]
            if not todo:
                return
        self.mapping = load_mapping()  # 추출할 때마다 매핑표를 다시 읽음
        self.settings["template"] = self.template_var.get()
        engine = choose_engine(self.settings)
        if self.settings.get("engine") == "ai" and engine != "ai":
            messagebox.showinfo(APP_NAME, "API 키가 없거나 외부 전송에 동의하지 않아 오프라인 OCR로 추출합니다.")
        elif engine == "offline":
            reason = ocr.availability()
            if reason and any(i.path.suffix.lower() != ".pdf" for i in todo):
                messagebox.showwarning(APP_NAME, reason + "\n\n사진은 추출할 수 없고, 텍스트 PDF만 읽습니다.")
        self.btn_extract.configure(text="⏳ 추출 중…", state="disabled")
        self.btn_stop.configure(state="normal")
        self._total = len(todo)
        self._done = 0
        self.runner = BatchRunner(todo, dict(self.settings), self.mapping,
                                  on_update=lambda it: self.queue.put(("update", it)),
                                  on_done=lambda: self.queue.put(("done", None)))
        self.status.set(f"추출 중… 0/{self._total} ({'AI' if engine == 'ai' else '오프라인 OCR'})")
        self.runner.start()

    def stop_extract(self):
        if self.runner:
            self.runner.cancel.set()
            self.status.set("멈추는 중… (처리 중인 건은 마칩니다)")

    def _poll(self):
        try:
            while True:
                kind, it = self.queue.get_nowait()
                if kind == "update":
                    if it.status in ("완료", "실패", "대기"):
                        self._done += 1 if it.status != "대기" else 0
                        self.status.set(f"추출 중… {self._done}/{self._total}")
                    self._update_row(it)
                    if it is self.selected:
                        self._fill_form(it)
                elif kind == "done":
                    self.runner = None
                    self.btn_extract.configure(text="▶ 추출 시작", state="normal")
                    self.btn_stop.configure(state="disabled")
                    self.refresh_table()
                    errs = {i.error for i in self.items if i.status == "실패" and i.error}
                    if errs and len(errs) <= 3:
                        messagebox.showwarning(APP_NAME, "일부 영수증을 추출하지 못했습니다.\n\n" + "\n".join(errs))
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    # ------------------------------------------------------------ 출력
    def _template_changed(self):
        self.settings["template"] = self.template_var.get()
        if not self.runner:
            self.refresh_table()

    def _mode_changed(self):
        self.settings["output_mode"] = self.mode_var.get()
        save_settings(self.settings)

    def choose_output_dir(self):
        d = filedialog.askdirectory(title="저장 위치 선택 (취소하면 영수증 폴더)")
        self.settings["output_dir"] = d or ""
        save_settings(self.settings)
        self.session.reset()
        self._update_outdir_label()

    def _update_outdir_label(self):
        d = self.settings.get("output_dir")
        self.outdir_label.configure(text=f"→ {d}" if d else "→ 영수증 폴더")

    def _ready_items(self):
        return [i for i in self.items if i.status in ("완료", "적용됨")]

    def apply_changes(self):
        if self.runner:
            return
        items = [i for i in self.items if i.status == "완료"]
        if not items:
            messagebox.showinfo(APP_NAME, "적용할 영수증이 없습니다. 먼저 [추출 시작]을 누르세요.")
            return
        n_rev = sum(1 for i in items if i.row_state != "ok")
        self.settings["template"] = self.template_var.get()
        save_settings(self.settings)
        mode = self.settings.get("output_mode")
        msg = f"{len(items)}건을 " + ("별도 폴더에 새 이름으로 복사합니다." if mode == "copy"
                                     else "제자리에서 이름을 바꿉니다(원본 이름이 바뀜).")
        if n_rev:
            msg += f"\n\n확인이 필요한 행이 {n_rev}건 있습니다(노랑·주황)."
        if not messagebox.askokcancel(APP_NAME, msg + "\n\n계속할까요?"):
            return
        try:
            folder, n, errors = apply(items, self.settings, self.session)
            write_summary_txt(self._ready_items(), folder)
            try:
                write_summary_xlsx(self._ready_items(), folder)
            except OutputError as e:
                errors.append(str(e))
        except OutputError as e:
            messagebox.showerror(APP_NAME, str(e))
            return
        except OSError as e:
            messagebox.showerror(APP_NAME, f"저장하지 못했습니다: {e.strerror or e}")
            return
        self.refresh_table()
        text = f"{n}건 완료.\n결과 폴더: {folder}"
        if errors:
            text += "\n\n오류:\n" + "\n".join(errors[:10])
        if messagebox.askyesno(APP_NAME, text + "\n\n결과 폴더를 열까요?"):
            _open_file(folder)

    def undo(self):
        if not messagebox.askyesno(APP_NAME, "마지막 [변경 적용]을 되돌릴까요?\n(복사 모드는 만든 사본만 지웁니다)"):
            return
        try:
            n, errors = undo_last()
        except OutputError as e:
            messagebox.showinfo(APP_NAME, str(e))
            return
        for it in self.items:
            if it.status == "적용됨":
                it.status = "완료"
        self.refresh_table()
        messagebox.showinfo(APP_NAME, f"{n}건 되돌렸습니다." + ("\n\n" + "\n".join(errors) if errors else ""))

    def _summary_folder(self):
        items = self._ready_items()
        if not items:
            messagebox.showinfo(APP_NAME, "요약할 영수증이 없습니다. 먼저 추출하세요.")
            return None, None
        try:
            return items, self.session.ensure(default_base(items, self.settings))
        except OutputError as e:
            messagebox.showerror(APP_NAME, str(e))
            return None, None

    def summary_txt(self):
        items, folder = self._summary_folder()
        if folder:
            preview_names(items, self.template_var.get())
            _open_file(write_summary_txt(items, folder))

    def summary_xlsx(self):
        items, folder = self._summary_folder()
        if folder:
            try:
                _open_file(write_summary_xlsx(items, folder))
            except OutputError as e:
                messagebox.showerror(APP_NAME, str(e))

    # ------------------------------------------------------------ 매핑표·설정
    def open_mapping(self):
        win = tk.Toplevel(self.root)
        win.title("사용자 매핑표 (카드번호 → 이름)")
        win.configure(bg=PINK_LIGHT)
        win.geometry("460x480")
        ttk.Label(win, text="한 줄에 '카드번호,이름'. 끝 4자리: 2580,김민배   앞 4자리: 4289****,김민배\n"
                            "가린 자리는 *: 845*,홍길동   (엑셀에서 복사한 탭 구분도 됩니다)",
                  justify="left").pack(anchor="w", padx=8, pady=6)
        txt = tk.Text(win, font=self.font, height=18)
        txt.pack(fill="both", expand=True, padx=8)
        self.mapping = load_mapping()  # 창을 열 때마다 다시 읽음
        txt.insert("1.0", mapping_to_text(self.mapping))

        def save():
            entries, bad = parse_mapping_text(txt.get("1.0", "end"))
            if bad:
                messagebox.showwarning(APP_NAME, "알아볼 수 없는 줄이 있습니다. 고친 뒤 다시 저장하세요:\n\n" + "\n".join(bad[:10]),
                                       parent=win)
                return
            try:
                save_mapping(entries)
            except OSError as e:
                messagebox.showerror(APP_NAME, f"저장하지 못했습니다: {e}", parent=win)
                return
            self.mapping = entries
            for it in self.items:
                if it.status in ("완료", "적용됨"):
                    finish(it, self.mapping)
            self.refresh_table()
            win.destroy()

        b = ttk.Frame(win)
        b.pack(fill="x", padx=8, pady=8)
        ttk.Button(b, text="저장", style="Accent.TButton", command=save).pack(side="right")
        ttk.Button(b, text="취소", command=win.destroy).pack(side="right", padx=4)

    def open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("설정")
        win.configure(bg=PINK_LIGHT)
        win.resizable(False, False)
        f = ttk.Frame(win, padding=12)
        f.pack(fill="both")
        ttk.Label(f, text="Claude API 키").grid(row=0, column=0, sticky="w")
        key_var = tk.StringVar(value=self.settings.get("api_key", ""))
        ttk.Entry(f, textvariable=key_var, show="•", width=44).grid(row=0, column=1, padx=4, pady=3)
        ttk.Label(f, text="모델").grid(row=1, column=0, sticky="w")
        model_var = tk.StringVar(value=self.settings.get("model") or ai.MODEL)
        ttk.Entry(f, textvariable=model_var, width=44).grid(row=1, column=1, padx=4, pady=3)
        consent_var = tk.BooleanVar(value=bool(self.settings.get("ai_consent")))
        ttk.Checkbutton(f, text="영수증을 Claude API로 보내는 것에 동의", variable=consent_var).grid(
            row=2, column=1, sticky="w")
        keep_var = tk.BooleanVar(value=bool(self.settings.get("keep_dates")))
        ttk.Checkbutton(f, text="사본의 파일 날짜를 원본과 같게", variable=keep_var).grid(row=3, column=1, sticky="w")
        split_var = tk.BooleanVar(value=bool(self.settings.get("split_pdf", True)))
        ttk.Checkbutton(f, text="여러 페이지 PDF는 페이지마다 영수증 1건으로", variable=split_var).grid(
            row=4, column=1, sticky="w")
        info = ttk.Label(f, text=f"설정·매핑표 위치: {Path(os.environ.get('APPDATA', '~'))}\\receipt_renamer",
                         wraplength=420)
        info.grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))

        def test():
            k = key_var.get().strip() or get_api_key(self.settings)
            if not k:
                messagebox.showinfo(APP_NAME, "API 키를 입력하세요.", parent=win)
                return
            win.config(cursor="watch")
            win.update()
            err = ai.test_connection(k, model_var.get().strip() or ai.MODEL)
            win.config(cursor="")
            messagebox.showinfo(APP_NAME, err or "연결 성공!", parent=win)

        def save():
            self.settings.update(api_key=key_var.get().strip(), model=model_var.get().strip() or ai.MODEL,
                                 ai_consent=consent_var.get(), keep_dates=keep_var.get(), split_pdf=split_var.get())
            save_settings(self.settings)
            win.destroy()

        b = ttk.Frame(f)
        b.grid(row=6, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(b, text="연결 테스트", command=test).pack(side="left", padx=4)
        ttk.Button(b, text="저장", style="Accent.TButton", command=save).pack(side="left")

    def _on_close(self):
        if self.runner:
            self.runner.cancel.set()
        self.settings["template"] = self.template_var.get()
        try:
            save_settings(self.settings)
        except OSError:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def run() -> int:
    try:
        App().run()
    except tk.TclError as e:
        print(f"창을 열 수 없습니다: {e}", file=sys.stderr)
        return 1
    return 0
