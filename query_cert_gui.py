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
        self.title("天津安管人员证书查询")
        self.geometry("940x680")
        self.minsize(860, 620)

        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.continue_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.output_chosen = False
        self.output_path = tk.StringVar(value=str(self.default_output_path("查询结果")))

        self._build_ui()
        self.after(150, self._poll_messages)

    def _build_ui(self) -> None:
        pad = {"padx": 12, "pady": 8}

        form = ttk.Frame(self)
        form.pack(fill=X, **pad)

        ttk.Label(form, text="公司名称").grid(row=0, column=0, sticky="w")
        self.company_var = tk.StringVar()
        self.company_entry = ttk.Entry(form, textvariable=self.company_var)
        self.company_entry.grid(row=0, column=1, columnspan=7, sticky="ew", padx=(8, 0))
        self.company_entry.focus()

        ttk.Label(form, text="A证数量").grid(row=1, column=0, sticky="w")
        ttk.Label(form, text="B证数量").grid(row=1, column=2, sticky="w")
        ttk.Label(form, text="C证数量").grid(row=1, column=4, sticky="w")
        self.a_var = tk.IntVar(value=0)
        self.b_var = tk.IntVar(value=0)
        self.c_var = tk.IntVar(value=0)
        ttk.Spinbox(form, textvariable=self.a_var, from_=0, to=999, width=8).grid(row=1, column=1, sticky="w", padx=(8, 18))
        ttk.Spinbox(form, textvariable=self.b_var, from_=0, to=999, width=8).grid(row=1, column=3, sticky="w", padx=(8, 18))
        ttk.Spinbox(form, textvariable=self.c_var, from_=0, to=999, width=8).grid(row=1, column=5, sticky="w", padx=(8, 18))

        ttk.Label(form, text="输出文件").grid(row=2, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.output_path).grid(row=2, column=1, columnspan=5, sticky="ew", padx=(8, 8))
        ttk.Button(form, text="选择", command=self.choose_output).grid(row=2, column=6, sticky="ew")
        ttk.Button(form, text="打开目录", command=self.open_output_dir).grid(row=2, column=7, sticky="ew", padx=(8, 0))

        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)
        form.columnconfigure(5, weight=1)

        actions = ttk.Frame(self)
        actions.pack(fill=X, padx=12, pady=(0, 8))
        self.start_button = ttk.Button(actions, text="开始查询", command=self.start_query)
        self.start_button.pack(side=LEFT)
        self.continue_button = ttk.Button(actions, text="已看到结果行，继续采集", command=self.continue_after_verify, state="disabled")
        self.continue_button.pack(side=LEFT, padx=(10, 0))
        ttk.Button(actions, text="清空日志", command=lambda: self.log_text.delete("1.0", END)).pack(side=RIGHT)

        columns = ("company", "level", "name", "cert_no", "expires_at")
        self.table = ttk.Treeview(self, columns=columns, show="headings", height=12)
        headings = {
            "company": "公司名称",
            "level": "类别",
            "name": "姓名",
            "cert_no": "证书编号",
            "expires_at": "有效期至",
        }
        widths = {"company": 220, "level": 70, "name": 100, "cert_no": 230, "expires_at": 130}
        for column in columns:
            self.table.heading(column, text=headings[column])
            self.table.column(column, width=widths[column], anchor="w")
        self.table.pack(fill=BOTH, expand=True, padx=12, pady=(0, 8))

        log_frame = ttk.LabelFrame(self, text="运行日志")
        log_frame.pack(fill=BOTH, expand=False, padx=12, pady=(0, 12))
        self.log_text = tk.Text(log_frame, height=9, wrap="word")
        self.log_text.pack(fill=BOTH, expand=True)

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self, textvariable=self.status_var, anchor="w").pack(fill=X, padx=12, pady=(0, 8))

    def choose_output(self) -> None:
        filename = filedialog.asksaveasfilename(
            title="选择输出 Excel",
            defaultextension=".xlsx",
            filetypes=[("Excel 文件", "*.xlsx"), ("CSV 文件", "*.csv"), ("所有文件", "*.*")],
            initialfile=Path(self.output_path.get()).name,
        )
        if filename:
            self.output_chosen = True
            self.output_path.set(filename)

    def default_output_path(self, company: str) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return APP_DIR / "output" / f"{query_cert.sanitize_filename(company)}_{timestamp}.xlsx"

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
            messagebox.showwarning("缺少公司名称", "请先输入公司名称。")
            return

        try:
            targets = {"A": int(self.a_var.get()), "B": int(self.b_var.get()), "C": int(self.c_var.get())}
        except Exception:
            messagebox.showwarning("数量错误", "A/B/C 数量必须是数字。")
            return
        if not any(value > 0 for value in targets.values()):
            messagebox.showwarning("数量错误", "请至少设置一种证书数量。")
            return

        self.table.delete(*self.table.get_children())
        self.continue_event.clear()
        self.start_button.config(state="disabled")
        self.status_var.set("正在打开浏览器...")
        task = query_cert.CompanyTask(company=company, targets=targets)
        if not self.output_chosen:
            self.output_path.set(str(self.default_output_path(company)))
        output = Path(self.output_path.get()).expanduser()
        self.worker = threading.Thread(target=self._run_worker, args=(task, output), daemon=True)
        self.worker.start()

    def _run_worker(self, task: query_cert.CompanyTask, output: Path) -> None:
        selected: list[dict[str, str]] = []
        try:
            if query_cert.sync_playwright is None:
                raise RuntimeError("缺少 Playwright 依赖，请按 README 安装，或使用打包后的 exe。")

            with query_cert.sync_playwright() as p:
                browser = query_cert.launch_browser(p, headless=False)
                context = browser.new_context(viewport={"width": 1366, "height": 900})
                page = context.new_page()
                try:
                    records, screenshot = query_cert.collect_company(
                        page,
                        task,
                        max_pages=20,
                        wait_for_user=self._wait_for_user,
                        log=self._send_log,
                    )
                    selected = query_cert.select_targets(records, task.targets)
                    if not selected and records:
                        self._send_log("按 A/B/C 数量筛选结果为空，已改为导出本次识别到的全部列表记录。")
                        selected = records
                    queried_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    for item in selected:
                        item["queried_at"] = queried_at
                        item["screenshot"] = str(screenshot)
                    query_cert.export_results(selected, output)
                finally:
                    browser.close()

            self.messages.put(("results", selected))
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
        self.log_text.insert(END, message.rstrip() + "\n")
        self.log_text.see(END)

    def _show_results(self, records: list[dict[str, str]]) -> None:
        for item in records:
            self.table.insert(
                "",
                END,
                values=(
                    item.get("company", ""),
                    item.get("level", ""),
                    item.get("name", ""),
                    item.get("cert_no", ""),
                    item.get("expires_at", ""),
                ),
            )


def main() -> None:
    app = QueryCertApp()
    app.mainloop()


if __name__ == "__main__":
    main()
