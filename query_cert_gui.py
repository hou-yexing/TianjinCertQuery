from __future__ import annotations

import os
import queue
import sys
import threading
from datetime import datetime
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, X, filedialog, messagebox, ttk
import tkinter as tk

import query_cert


APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


class QueryCertApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("天津证书查询工具")
        self.geometry("1180x780")
        self.minsize(1080, 700)

        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.continue_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.output_chosen = False
        self.output_path = tk.StringVar(value=str(self.default_output_path("查询结果")))

        self._configure_style()
        self._build_ui()
        self.after(150, self._poll_messages)

    def _configure_style(self) -> None:
        self.configure(bg="#eef4fb")
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.font = ("Microsoft YaHei UI", 11)
        self.title_font = ("Microsoft YaHei UI", 15, "bold")
        self.section_font = ("Microsoft YaHei UI", 12, "bold")
        style.configure(".", font=self.font)
        style.configure("App.TFrame", background="#eef4fb")
        style.configure("Card.TFrame", background="#fbfdff", relief="flat")
        style.configure("Title.TLabel", background="#eef4fb", foreground="#12315f", font=self.title_font)
        style.configure("Muted.TLabel", background="#eef4fb", foreground="#667085")
        style.configure("CardTitle.TLabel", background="#fbfdff", foreground="#12315f", font=self.section_font)
        style.configure("TLabel", background="#fbfdff", foreground="#31405a")
        style.configure("TEntry", padding=(8, 6))
        style.configure("TSpinbox", padding=(8, 6))
        style.configure("Primary.TButton", background="#2563eb", foreground="#ffffff", padding=(16, 8), font=("Microsoft YaHei UI", 11, "bold"))
        style.map("Primary.TButton", background=[("active", "#0891b2"), ("disabled", "#9fb3cf")])
        style.configure("Tool.TButton", padding=(12, 7))
        style.configure("Treeview", rowheight=30, font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, style="App.TFrame")
        outer.pack(fill=BOTH, expand=True, padx=22, pady=(18, 26))

        ttk.Label(outer, text="天津证书查询工具", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="安管人员证书与建造师信息采集，支持指定姓名优先匹配。", style="Muted.TLabel").pack(anchor="w", pady=(4, 14))

        top = self._card(outer)
        top.pack(fill=X)
        top.columnconfigure(0, weight=2)
        top.columnconfigure(1, weight=3)
        top.columnconfigure(2, weight=0)
        top.columnconfigure(3, weight=0)

        self.company_var = tk.StringVar()
        self._field(top, "单位名称", self.company_var, row=0, column=0)
        self._field(top, "输出文件", self.output_path, row=0, column=1)
        ttk.Button(top, text="选择", style="Tool.TButton", command=self.choose_output).grid(row=1, column=2, sticky="ew", padx=(12, 0))
        ttk.Button(top, text="打开目录", style="Tool.TButton", command=self.open_output_dir).grid(row=1, column=3, sticky="ew", padx=(8, 0))
        self.start_button = ttk.Button(top, text="开始查询", style="Primary.TButton", command=self.start_query)
        self.start_button.grid(row=0, column=2, columnspan=2, sticky="nsew", padx=(12, 0), pady=(0, 30))

        middle = ttk.Frame(outer, style="App.TFrame")
        middle.pack(fill=X, pady=(16, 0))
        middle.columnconfigure(0, weight=3)
        middle.columnconfigure(1, weight=2)

        cert_card = self._card(middle)
        cert_card.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self._section_title(cert_card, "安管人员证书要求").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))

        self.count_vars: dict[str, tk.IntVar] = {}
        self.name_vars: dict[str, list[tk.StringVar]] = {}
        for row, level in enumerate(("A", "B", "C"), start=1):
            self.count_vars[level] = tk.IntVar(value=0)
            self.name_vars[level] = [tk.StringVar(), tk.StringVar()]
            self._field(cert_card, f"{level}证数量", self.count_vars[level], row=row, column=0, width=10, spin=True)
            self._field(cert_card, f"{level}证姓名1", self.name_vars[level][0], row=row, column=1)
            self._field(cert_card, f"{level}证姓名2", self.name_vars[level][1], row=row, column=2)
        for idx in range(3):
            cert_card.columnconfigure(idx, weight=1)

        builder_card = self._card(middle)
        builder_card.grid(row=1, column=0, sticky="nsew", padx=(0, 12), pady=(14, 0))
        self._section_title(builder_card, "建造师查询").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        self.builder_vars = [tk.StringVar(), tk.StringVar()]
        self._field(builder_card, "建造师姓名1", self.builder_vars[0], row=1, column=0)
        self._field(builder_card, "建造师姓名2", self.builder_vars[1], row=1, column=1)
        ttk.Label(
            builder_card,
            text="按姓名查询注册类别包含“建造师”的记录，详情页单位一致后写入“建造师信息”。",
            style="TLabel",
            wraplength=360,
        ).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        builder_card.columnconfigure(0, weight=1)
        builder_card.columnconfigure(1, weight=1)

        status_card = self._card(middle)
        status_card.grid(row=0, column=1, rowspan=2, sticky="nsew")
        self.status_var = tk.StringVar(value="就绪")
        self.last_log_var = tk.StringVar(value="等待开始查询")
        self._section_title(status_card, "工作状态").pack(anchor="w", pady=(0, 12))
        status_text_area = ttk.Frame(status_card, style="Card.TFrame", height=118)
        status_text_area.pack(fill=X, pady=(0, 12))
        status_text_area.pack_propagate(False)
        ttk.Label(status_text_area, textvariable=self.status_var, style="CardTitle.TLabel", wraplength=360).pack(anchor="w", fill=X)
        ttk.Label(status_text_area, textvariable=self.last_log_var, style="TLabel", wraplength=360).pack(anchor="w", fill=X, pady=(8, 0))
        self.continue_button = ttk.Button(
            status_card,
            text="已看到结果行，继续采集",
            style="Primary.TButton",
            command=self.continue_after_verify,
            state="disabled",
        )
        self.continue_button.pack(fill=X, pady=(0, 10))
        ttk.Button(status_card, text="清空日志", style="Tool.TButton", command=lambda: self.log_text.delete("1.0", END)).pack(fill=X, pady=(0, 12))
        ttk.Label(status_card, text="运行日志", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 8))
        self.log_text = tk.Text(
            status_card,
            height=14,
            wrap="word",
            bg="#0f172a",
            fg="#dbeafe",
            insertbackground="#dbeafe",
            relief="flat",
            padx=12,
            pady=10,
            font=("Consolas", 10),
        )
        self.log_text.pack(fill=BOTH, expand=True)

        columns = ("sheet", "level", "name", "cert_no", "expires_at", "company")
        self.table = ttk.Treeview(outer, columns=columns, show="headings", height=9)
        headings = {
            "sheet": "表单",
            "level": "类别",
            "name": "姓名",
            "cert_no": "证书/证件编号",
            "expires_at": "有效期",
            "company": "单位名称",
        }
        widths = {"sheet": 130, "level": 110, "name": 100, "cert_no": 260, "expires_at": 170, "company": 280}
        for column in columns:
            self.table.heading(column, text=headings[column])
            self.table.column(column, width=widths[column], anchor="w")
        self.table.pack(fill=BOTH, expand=True, pady=(14, 0))

        self.company_var.trace_add("write", lambda *_: self._refresh_default_output())
        self.company_entry.focus()

    def _card(self, parent) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Card.TFrame", padding=18)
        return frame

    def _section_title(self, parent, text: str) -> ttk.Label:
        return ttk.Label(parent, text=text, style="CardTitle.TLabel")

    def _field(self, parent, label: str, variable, row: int, column: int, width: int | None = None, spin: bool = False) -> None:
        wrap = ttk.Frame(parent, style="Card.TFrame")
        wrap.grid(row=row, column=column, sticky="ew", padx=(0 if column == 0 else 12, 0), pady=(0, 12))
        ttk.Label(wrap, text=label).pack(anchor="w", pady=(0, 6))
        if spin:
            widget = ttk.Spinbox(wrap, textvariable=variable, from_=0, to=999, width=width or 12)
        else:
            widget = ttk.Entry(wrap, textvariable=variable, width=width)
        widget.pack(fill=X)
        if label == "单位名称":
            self.company_entry = widget

    def choose_output(self) -> None:
        filename = filedialog.asksaveasfilename(
            title="选择输出 Excel",
            defaultextension=".xlsx",
            filetypes=[("Excel 文件", "*.xlsx"), ("所有文件", "*.*")],
            initialfile=Path(self.output_path.get()).name,
        )
        if filename:
            self.output_chosen = True
            self.output_path.set(filename)

    def default_output_path(self, company: str) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return APP_DIR / "output" / f"{query_cert.sanitize_filename(company)}_{timestamp}.xlsx"

    def _refresh_default_output(self) -> None:
        if self.output_chosen:
            return
        company = self.company_var.get().strip() or "查询结果"
        self.output_path.set(str(self.default_output_path(company)))

    def open_output_dir(self) -> None:
        target = Path(self.output_path.get()).expanduser().resolve().parent
        target.mkdir(parents=True, exist_ok=True)
        os.startfile(target)

    def start_query(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("正在运行", "当前查询尚未结束。")
            return
        company = self.company_var.get().strip()
        if not company:
            messagebox.showwarning("缺少单位名称", "请先输入单位名称。")
            return

        try:
            targets = {level: int(var.get()) for level, var in self.count_vars.items()}
        except Exception:
            messagebox.showwarning("数量错误", "A/B/C 数量必须是数字。")
            return
        required_names = {
            level: [name_var.get().strip() for name_var in vars_ if name_var.get().strip()]
            for level, vars_ in self.name_vars.items()
        }
        builder_names = [var.get().strip() for var in self.builder_vars if var.get().strip()]
        if not any(value > 0 for value in targets.values()) and not builder_names:
            messagebox.showwarning("缺少查询条件", "请至少设置一种证书数量，或填写建造师姓名。")
            return

        self.table.delete(*self.table.get_children())
        self.continue_event.clear()
        self.start_button.config(state="disabled")
        self.status_var.set("正在打开浏览器...")
        task = query_cert.CompanyTask(company=company, targets=targets, required_names=required_names, builder_names=builder_names)
        if not self.output_chosen:
            self.output_path.set(str(self.default_output_path(company)))
        output = Path(self.output_path.get()).expanduser()
        self.worker = threading.Thread(target=self._run_worker, args=(task, output), daemon=True)
        self.worker.start()

    def _run_worker(self, task: query_cert.CompanyTask, output: Path) -> None:
        selected: list[dict[str, str]] = []
        builder_records: list[dict[str, str]] = []
        try:
            if query_cert.sync_playwright is None:
                raise RuntimeError("缺少 Playwright 依赖，请按 README 安装，或使用打包后的 exe。")

            with query_cert.sync_playwright() as p:
                browser = query_cert.launch_browser(p, headless=False)
                context = browser.new_context(viewport={"width": 1366, "height": 900})
                page = context.new_page()
                try:
                    if any(value > 0 for value in task.targets.values()):
                        records, screenshot = query_cert.collect_company(
                            page,
                            task,
                            max_pages=20,
                            wait_for_user=self._wait_for_user,
                            log=self._send_log,
                        )
                        selected = query_cert.select_targets(records, task.targets, task.required_names)
                        if not selected and records:
                            self._send_log("按 A/B/C 数量筛选结果为空，已改为导出本次识别到的全部列表记录。")
                            selected = records
                        queried_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        for item in selected:
                            item["queried_at"] = queried_at
                            item["screenshot"] = str(screenshot)

                    if task.builder_names:
                        builder_records = query_cert.collect_builders(
                            page,
                            task.company,
                            task.builder_names,
                            wait_for_user=self._wait_for_user,
                            log=self._send_log,
                        )
                        queried_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        for item in builder_records:
                            item["queried_at"] = queried_at

                    query_cert.export_results(selected, output, builder_records=builder_records)
                finally:
                    browser.close()

            self.messages.put(("results", {"certs": selected, "builders": builder_records}))
            self.messages.put(("done", output))
        except Exception as exc:
            self.messages.put(("error", str(exc)))

    def _wait_for_user(self) -> None:
        self.messages.put(("wait", None))
        self.continue_event.clear()
        self.continue_event.wait()
        self.messages.put(("log", "继续采集页面结果..."))

    def continue_after_verify(self) -> None:
        self.continue_button.config(state="disabled")
        self.continue_event.set()

    def _send_log(self, message: str) -> None:
        self.messages.put(("log", message))

    def _poll_messages(self) -> None:
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "log":
                    self._append_log(str(payload))
                elif kind == "wait":
                    self.status_var.set("请在浏览器中完成滑块，并等表格出现人员结果行后再点击继续采集。")
                    self.continue_button.config(state="normal")
                elif kind == "results":
                    self._show_results(payload)
                elif kind == "done":
                    self.start_button.config(state="normal")
                    self.continue_button.config(state="disabled")
                    self.status_var.set(f"完成：{payload}")
                    messagebox.showinfo("查询完成", f"结果已导出：\n{payload}")
                elif kind == "error":
                    self.start_button.config(state="normal")
                    self.continue_button.config(state="disabled")
                    self.status_var.set("查询失败")
                    self._append_log(f"错误：{payload}")
                    messagebox.showerror("查询失败", str(payload))
        except queue.Empty:
            pass
        self.after(150, self._poll_messages)

    def _append_log(self, message: str) -> None:
        if hasattr(self, "last_log_var"):
            self.last_log_var.set(message.rstrip() or "正在运行")
        self.log_text.insert(END, message.rstrip() + "\n")
        self.log_text.see(END)

    def _show_results(self, payload: object) -> None:
        data = payload if isinstance(payload, dict) else {"certs": payload, "builders": []}
        for item in data.get("certs", []):
            self.table.insert(
                "",
                END,
                values=(
                    "安管人员证书",
                    item.get("level", ""),
                    item.get("name", ""),
                    item.get("cert_no", ""),
                    item.get("expires_at", ""),
                    item.get("company", ""),
                ),
            )
        for item in data.get("builders", []):
            self.table.insert(
                "",
                END,
                values=(
                    "建造师信息",
                    item.get("register_category", ""),
                    item.get("name", ""),
                    item.get("id_no", ""),
                    f"{item.get('valid_from', '')} - {item.get('valid_to', '')}".strip(" -"),
                    item.get("company", ""),
                ),
            )


def main() -> None:
    app = QueryCertApp()
    app.mainloop()


if __name__ == "__main__":
    main()
