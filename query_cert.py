from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Iterable

RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
LOCAL_DEPS = RESOURCE_DIR / ".deps"
LOCAL_BROWSERS = RESOURCE_DIR / ".playwright-browsers"
if LOCAL_DEPS.exists():
    sys.path.insert(0, str(LOCAL_DEPS))
if LOCAL_BROWSERS.exists():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(LOCAL_BROWSERS))

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - user-facing dependency check
    sync_playwright = None
    PlaywrightTimeoutError = Exception


TARGET_URL = "https://zfcxjs.tj.gov.cn/ggfw_70/xxcx/agryxx/"
BUILDER_URL = "https://zfcxjs.tj.gov.cn/ggfw_70/xxcx/zyryxx/"
OUTPUT_DIR = Path("output")
SCREENSHOT_DIR = OUTPUT_DIR / "screenshots"
DEBUG_DIR = OUTPUT_DIR / "debug"

FIELD_ALIASES = {
    "name": ("姓名", "人员姓名"),
    "cert_no": ("证书编号", "证书号", "证号", "编号"),
    "expires_at": ("有效期至", "有效期", "到期日期", "有效截止日期"),
    "level": ("证书类别", "类别", "类型", "人员类别", "证书类型"),
    "company": ("企业名称", "公司名称", "单位名称", "聘用企业"),
}

DATE_PATTERN = re.compile(r"(20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)")
DETAIL_LABELS = (
    "姓名",
    "性别",
    "出生日期",
    "身份证号",
    "企业名称",
    "岗位类型",
    "岗位工种",
    "技术职称或等级",
    "证书编号",
    "有效期至",
)

BUILDER_LABEL_ALIASES = {
    "name": ("姓名",),
    "gender": ("性别",),
    "register_category": ("注册类别",),
    "id_no": ("证件编号", "身份证号", "证书编号"),
    "seal_no": ("执业印章号",),
    "valid_from": ("注册证书有效期开始", "有效期开始", "注册有效期开始"),
    "valid_to": ("注册证书有效期结束", "有效期结束", "注册有效期结束", "有效期至"),
    "major": ("注册专业",),
    "company": ("注册证书所在单位名称", "注册单位", "聘用企业", "企业名称", "单位名称"),
}

BUILDER_EXPORT_HEADERS = [
    "姓名",
    "性别",
    "注册类别",
    "证件编号",
    "执业印章号",
    "注册证书有效期开始",
    "注册证书有效期结束",
    "注册专业",
    "注册证书所在单位名称",
    "查询时间",
]


@dataclass(frozen=True)
class CompanyTask:
    company: str
    targets: dict[str, int]
    required_names: dict[str, list[str]] = field(default_factory=dict)
    builder_names: list[str] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="查询天津住建委安管人员证书，人工完成滑块后自动摘录 A/B/C 证信息。"
    )
    single = parser.add_argument_group("单家公司模式")
    single.add_argument("--company", help="公司名称")
    single.add_argument("--a", type=int, default=0, help="需要摘录的 A 证数量")
    single.add_argument("--b", type=int, default=0, help="需要摘录的 B 证数量")
    single.add_argument("--c", type=int, default=0, help="需要摘录的 C 证数量")

    batch = parser.add_argument_group("批量模式")
    batch.add_argument(
        "--input",
        type=Path,
        help="CSV 文件，表头为 company,A,B,C 或 公司名称,A,B,C",
    )

    parser.add_argument("--output", type=Path, help="输出文件，默认自动生成")
    parser.add_argument("--max-pages", type=int, default=20, help="每家公司最多翻页数")
    parser.add_argument("--headless", action="store_true", help="无头模式，不建议用于滑块页面")
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="查询结束后保持浏览器打开，便于人工复核",
    )
    return parser.parse_args()


def load_tasks(args: argparse.Namespace) -> list[CompanyTask]:
    if args.input:
        tasks: list[CompanyTask] = []
        with args.input.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                company = (row.get("company") or row.get("公司名称") or row.get("企业名称") or "").strip()
                if not company:
                    continue
                tasks.append(
                    CompanyTask(
                        company=company,
                        targets={
                            "A": int(row.get("A") or row.get("a") or 0),
                            "B": int(row.get("B") or row.get("b") or 0),
                            "C": int(row.get("C") or row.get("c") or 0),
                        },
                    )
                )
        if not tasks:
            raise SystemExit("输入 CSV 中没有可识别的公司。")
        return tasks

    if not args.company:
        raise SystemExit("请提供 --company，或使用 --input 指定批量 CSV。")
    return [CompanyTask(args.company, {"A": args.a, "B": args.b, "C": args.c})]


def sanitize_filename(value: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", value).strip("_") or "query"


def normalize_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def header_to_field(header: str) -> str | None:
    header = normalize_text(header)
    for field, aliases in FIELD_ALIASES.items():
        if any(alias in header for alias in aliases):
            return field
    return None


def infer_level(record: dict[str, str]) -> str:
    candidates = " ".join(
        normalize_text(record.get(key, ""))
        for key in ("level", "cert_no", "raw")
    )
    patterns = (
        r"([ABC])\s*\d*\s*类\s*证书编号",
        r"([ABC])\s*\d*\s*类",
        r"建安\s*([ABC])",
        r"安\s*([ABC])",
        r"([ABC])\s*证",
        r"类别[:：]?\s*([ABC])",
    )
    for pattern in patterns:
        match = re.search(pattern, candidates, flags=re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return normalize_text(record.get("level", "")).upper()


def install_hint() -> None:
    print("缺少依赖。请先执行：", file=sys.stderr)
    print("  python -m pip install -r requirements.txt --target .deps", file=sys.stderr)
    print('  $env:PYTHONPATH=".deps"; $env:PLAYWRIGHT_BROWSERS_PATH=".playwright-browsers"', file=sys.stderr)
    print("  python -m playwright install chromium", file=sys.stderr)


def launch_browser(playwright, *, headless: bool = False):
    errors: list[str] = []
    for channel in ("msedge", "chrome"):
        try:
            return playwright.chromium.launch(channel=channel, headless=headless)
        except Exception as exc:
            errors.append(f"{channel}: {exc}")
    try:
        return playwright.chromium.launch(headless=headless)
    except Exception as exc:
        errors.append(f"bundled chromium: {exc}")
    raise RuntimeError(
        "无法启动浏览器。轻量版需要电脑已安装 Microsoft Edge 或 Google Chrome；"
        "如果这台电脑不能安装浏览器，请使用包含内置浏览器的完整版。\n"
        + "\n".join(errors[-3:])
    )


def candidate_frames(page):
    frames = list(page.frames)
    frames.sort(key=lambda frame: 0 if "szzj.zfcxjs.tj.gov.cn" in frame.url else 1)
    return frames


def frame_text(frame) -> str:
    try:
        return frame.locator("body").inner_text(timeout=1000)
    except Exception:
        return ""


def click_first(page, selectors: Iterable[str], timeout: int = 1200) -> bool:
    for frame in candidate_frames(page):
        for selector in selectors:
            locator = frame.locator(selector).first
            try:
                if locator.count() and locator.is_visible(timeout=timeout):
                    locator.click(timeout=timeout)
                    return True
            except PlaywrightTimeoutError:
                continue
            except Exception:
                continue
    return False


def fill_company(page, company: str) -> None:
    selectors = (
        'input[placeholder*="企业"]',
        'input[placeholder*="公司"]',
        'input[placeholder*="单位"]',
        'input[aria-label*="企业"]',
        'input[aria-label*="公司"]',
        'input[type="text"]',
        "input:not([type])",
    )
    for frame in candidate_frames(page):
        if "企业名称" not in frame_text(frame) and "请输入企业名称" not in frame_text(frame):
            continue
        for selector in selectors:
            locator = frame.locator(selector).first
            try:
                if locator.count() and locator.is_visible(timeout=1500):
                    locator.fill(company, timeout=3000)
                    return
            except PlaywrightTimeoutError:
                continue
            except Exception:
                continue
    raise RuntimeError("没有找到公司名称输入框，请检查页面是否已正常打开。")


def fill_person_name(page, name: str) -> None:
    selectors = (
        'input[placeholder*="姓名"]',
        'input[aria-label*="姓名"]',
        'input[type="text"]',
        "input:not([type])",
    )
    for frame in candidate_frames(page):
        for selector in selectors:
            locator = frame.locator(selector).first
            try:
                if locator.count() and locator.is_visible(timeout=1500):
                    locator.fill(name, timeout=3000)
                    return
            except PlaywrightTimeoutError:
                continue
            except Exception:
                continue
    raise RuntimeError("没有找到姓名输入框，请检查页面是否已正常打开。")


def submit_query(page) -> bool:
    button_patterns = ("查询", "搜索", "检索")
    for frame in candidate_frames(page):
        for text in button_patterns:
            try:
                frame.get_by_role("button", name=re.compile(text)).click(timeout=1500)
                return True
            except Exception:
                pass
        try:
            clicked = frame.evaluate(
                """
                () => {
                  const visible = (el) => {
                    const box = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return box.width > 0 && box.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const nodes = [...document.querySelectorAll('button,a,input,[role="button"],.ant-btn')];
                  const node = nodes.find(el => visible(el) && /查询|搜索|检索/.test((el.innerText || el.value || el.title || '').trim()));
                  if (!node) return false;
                  node.click();
                  return true;
                }
                """
            )
            if clicked:
                return True
        except Exception:
            pass
    if click_first(page, ('button:has-text("查询")', 'a:has-text("查询")', 'input[value*="查询"]', '.ant-btn:has-text("查询")')):
        return True
    return False


def extract_visible_tables(page) -> list[dict[str, str]]:
    all_rows: list[dict[str, str]] = []
    script = """
        () => {
          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const tables = [...document.querySelectorAll('table')].filter(t => {
            const box = t.getBoundingClientRect();
            return box.width > 0 && box.height > 0 && clean(t.innerText);
          });
          const rows = [];
          for (const [tableIndex, table] of tables.entries()) {
            const trs = [...table.querySelectorAll('tr')];
            if (!trs.length) continue;
            let headers = [...trs[0].querySelectorAll('th,td')].map(c => clean(c.innerText));
            for (const [rowIndex, tr] of trs.slice(1).entries()) {
              const cells = [...tr.querySelectorAll('td,th')].map(c => clean(c.innerText));
              if (!cells.some(Boolean)) continue;
              const raw = cells.join(' | ');
              const row = {
                raw,
                __cells: cells,
                __row_key: tr.getAttribute('data-row-key') || '',
                __table_index: String(tableIndex),
                __row_index: String(rowIndex)
              };
              cells.forEach((cell, idx) => {
                const key = headers[idx] || `col_${idx + 1}`;
                row[key] = cell;
              });
              rows.push(row);
            }
          }
          return rows;
        }
        """
    for frame in candidate_frames(page):
        try:
            rows = frame.evaluate(script)
            all_rows.extend(rows)
        except Exception:
            continue
    return all_rows


def page_loading_state(page) -> dict[str, object]:
    script = """
        () => {
          const visible = (el) => {
            const box = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return box.width > 0 && box.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
          };
          const loadingSelectors = [
            '.ant-spin-spinning',
            '.ant-spin-dot-spin',
            '.ant-spin-dot',
            '.el-loading-mask',
            '.loading'
          ];
          const loading = loadingSelectors.some((selector) =>
            [...document.querySelectorAll(selector)].some(visible)
          );
          const bodyText = document.body ? document.body.innerText : '';
          const rowCount = [...document.querySelectorAll('tbody tr')].filter((tr) => {
            const text = (tr.innerText || '').replace(/\\s+/g, ' ').trim();
            return visible(tr) && text && !/暂无数据|No Data/.test(text);
          }).length;
          return { loading, rowCount, hasNoData: /暂无数据|No Data/.test(bodyText) };
        }
        """
    states: list[dict[str, object]] = []
    for frame in candidate_frames(page):
        try:
            states.append(frame.evaluate(script))
        except Exception:
            continue
    return {
        "loading": any(bool(item.get("loading")) for item in states),
        "row_count": sum(int(item.get("rowCount") or 0) for item in states),
        "has_no_data": any(bool(item.get("hasNoData")) for item in states),
    }


def wait_for_query_results(page, company: str, log=print, timeout_seconds: int = 60) -> None:
    deadline = time.time() + timeout_seconds
    last_log = 0.0
    stable_hits = 0
    last_signature = ""
    while time.time() < deadline:
        rows = normalize_rows(extract_visible_tables(page), company)
        state = page_loading_state(page)
        signature = "\n".join(item.get("raw", "") for item in rows)
        if rows and signature == last_signature and not state["loading"]:
            stable_hits += 1
        elif rows:
            stable_hits = 1
            last_signature = signature
        else:
            stable_hits = 0
            last_signature = ""
        if rows and stable_hits >= 3:
            log(f"目标公司结果已稳定，当前识别到 {len(rows)} 条证书记录。")
            return
        if not state["loading"] and state["has_no_data"]:
            # Ant Design 有时会先显示暂无数据再发请求，给它一点缓冲。
            page.wait_for_timeout(2500)
            rows = normalize_rows(extract_visible_tables(page), company)
            state = page_loading_state(page)
            if rows or state["loading"]:
                continue
            log("页面显示暂无数据，未发现可采集的人员行。")
            return
        now = time.time()
        if now - last_log >= 5:
            if state["row_count"] and not rows:
                log("页面已有列表行，但不是目标公司结果，继续等待目标公司查询结果刷新...")
            else:
                log("正在等待查询结果加载，请确认浏览器中已完成滑块且列表行已显示...")
            last_log = now
        page.wait_for_timeout(1000)
    log(f"等待 {timeout_seconds} 秒后仍未识别到结果行，继续导出当前页面可见数据。")


def save_debug_html(page, company: str) -> Path:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path = DEBUG_DIR / f"{sanitize_filename(company)}_{datetime.now():%Y%m%d_%H%M%S}.html"
    parts: list[str] = []
    for idx, frame in enumerate(candidate_frames(page)):
        try:
            parts.append(f"\n<!-- FRAME {idx}: {frame.url} -->\n")
            parts.append(frame.content())
        except Exception as exc:
            parts.append(f"\n<!-- FRAME {idx}: unable to read content: {exc} -->\n")
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def normalize_rows(rows: list[dict[str, str]], company: str) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for row in rows:
        if not is_target_company_row(row, company):
            continue
        positional = normalize_positional_row(row, company)
        if positional:
            normalized.extend(positional)
            continue

        cert_columns = [
            (key, value)
            for key, value in row.items()
            if key not in ("raw", "__cells", "__table_index", "__row_index") and "证书编号" in key and normalize_text(value)
        ]
        if cert_columns:
            for key, value in cert_columns:
                record = {
                    "company": company,
                    "name": "",
                    "cert_no": normalize_text(value),
                    "expires_at": "",
                    "level": infer_level({"level": key, "cert_no": value, "raw": f"{key} {value}"}),
                    "raw": normalize_text(row.get("raw", "")),
                    "_source_raw": normalize_text(row.get("raw", "")),
                    "_source_name": "",
                }
                for source_key, source_value in row.items():
                    field = header_to_field(source_key)
                    if field and field not in ("cert_no", "level"):
                        record[field] = normalize_text(source_value)
                record["_source_name"] = record["name"]
                if record["name"] and record["cert_no"]:
                    normalized.append(record)
            continue

        record = {
            "company": company,
            "name": "",
            "cert_no": "",
            "expires_at": "",
            "level": "",
            "raw": normalize_text(row.get("raw", "")),
            "_source_raw": normalize_text(row.get("raw", "")),
            "_source_name": "",
        }
        for key, value in row.items():
            field = header_to_field(key)
            if field:
                record[field] = normalize_text(value)
        record["level"] = infer_level(record)
        record["_source_name"] = record["name"]
        if record["name"] and record["cert_no"]:
            normalized.append(record)
    return normalized


def is_target_company_row(row: dict[str, str], company: str) -> bool:
    company = normalize_text(company)
    cells = row.get("__cells")
    if isinstance(cells, list) and len(cells) > 1:
        row_company = normalize_text(cells[1])
        if row_company and row_company not in ("企业名称", company):
            return row_company == company
    raw = normalize_text(row.get("raw", ""))
    if company and raw and "企业名称" not in raw:
        return company in raw
    return True


def normalize_positional_row(row: dict[str, str], company: str) -> list[dict[str, str]]:
    cells = row.get("__cells")
    if not isinstance(cells, list) or len(cells) < 8:
        return []
    clean_cells = [normalize_text(cell) for cell in cells]
    raw = normalize_text(row.get("raw", ""))

    # 天津安管人员列表列序通常为：
    # 序号、企业名称、姓名、性别、身份证号、A类证书编号、B类证书编号、C1、C2、C3、操作。
    name = clean_cells[2] if len(clean_cells) > 2 else ""
    if not name or name in ("姓名", "暂无数据"):
        return []
    cert_positions = (("A", 5), ("B", 6), ("C", 7), ("C", 8), ("C", 9))
    records: list[dict[str, str]] = []
    for level, index in cert_positions:
        if index >= len(clean_cells):
            continue
        cert_no = clean_cells[index]
        if not cert_no or cert_no in ("-", "--", "无", "暂无", "暂无数据"):
            continue
        records.append(
            {
                "company": company,
                "name": name,
                "cert_no": cert_no,
                "expires_at": "",
                "level": level,
                "raw": raw,
                "_source_raw": raw,
                "_source_name": name,
                "_source_row_index": str(row.get("__row_index", "")),
                "_source_row_key": str(row.get("__row_key", "")),
                "_source_table_index": str(row.get("__table_index", "")),
                "_source_page_no": str(row.get("__page_no", "")),
            }
        )
    return records


def normalize_date(value: str) -> str:
    value = normalize_text(value)
    value = value.replace("年", "-").replace("月", "-").replace("日", "")
    value = value.replace("/", "-").replace(".", "-")
    parts = value.split("-")
    if len(parts) >= 3 and all(part.isdigit() for part in parts[:3]):
        return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    return value


def normalize_cert_key(value: str) -> str:
    value = normalize_text(value)
    value = value.replace("（", "(").replace("）", ")")
    return re.sub(r"\s+", "", value).upper()


def dates_near_cert(text: str, cert_no: str) -> list[str]:
    cert_no = normalize_text(cert_no)
    if not cert_no:
        return []
    candidates: list[str] = []
    for match in re.finditer(re.escape(cert_no), text):
        end = min(len(text), match.end() + 800)
        snippet = text[match.end():end]
        label_match = re.search(r"有效期至\s*[:：]?\s*(20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)", snippet)
        if label_match:
            candidates.append(normalize_date(label_match.group(1)))
    return candidates


def extract_expiry_from_detail_text(text: str, records: list[dict[str, str]]) -> dict[str, str]:
    text = normalize_text(text)
    fields = extract_detail_fields_from_text(text)
    detail_cert = fields.get("证书编号", "")
    expiry = normalize_date(fields.get("有效期至", ""))
    if expiry:
        result: dict[str, str] = {}
        for record in records:
            cert_no = record.get("cert_no", "")
            cert_key = normalize_cert_key(cert_no)
            detail_key = normalize_cert_key(detail_cert)
            if detail_key and cert_key and (cert_key == detail_key or cert_key in detail_key or detail_key in cert_key):
                result[record["cert_no"]] = expiry
            elif len(records) == 1:
                result[record["cert_no"]] = expiry
        if result:
            return result

    result: dict[str, str] = {}
    for record in records:
        cert_no = record.get("cert_no", "")
        nearby = dates_near_cert(text, cert_no)
        if nearby:
            result[cert_no] = nearby[-1]
    return result


def extract_detail_fields_from_text(text: str) -> dict[str, str]:
    text = normalize_text(text)
    positions: list[tuple[int, str]] = []
    for label in DETAIL_LABELS:
        for match in re.finditer(re.escape(label), text):
            positions.append((match.start(), label))
    positions.sort()
    fields: dict[str, str] = {}
    for idx, (start, label) in enumerate(positions):
        value_start = start + len(label)
        value_end = len(text)
        for next_start, next_label in positions[idx + 1 :]:
            if next_start > value_start and next_label != label:
                value_end = next_start
                break
        value = text[value_start:value_end].strip(" ：:")
        if not value:
            continue
        if label == "有效期至":
            match = DATE_PATTERN.search(value)
            if match:
                fields[label] = normalize_date(match.group(1))
        elif label == "证书编号":
            # The detail page also shows the cert number as a heading; the labeled
            # row is the one immediately followed by 有效期至, so prefer that one.
            fields[label] = value
        else:
            fields[label] = value
    return fields


def extract_detail_fields_from_dom(page) -> dict[str, str]:
    script = """
        () => {
          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const fields = {};
          const hiddenByAncestor = (el) => {
            for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
              const ariaHidden = node.getAttribute && node.getAttribute('aria-hidden');
              const cls = String(node.className || '');
              if (ariaHidden === 'true' || /ant-tabs-tabpane-inactive/.test(cls)) return true;
            }
            return false;
          };
          const visible = (el) => {
            if (!el || hiddenByAncestor(el)) return false;
            const box = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return box.width > 0 && box.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
          };
          const rows = [...document.querySelectorAll('tr')];
          for (const row of rows) {
            if (!visible(row)) continue;
            const cells = [...row.querySelectorAll('th,td')].map((cell) => clean(cell.innerText));
            if (cells.length < 2) continue;
            for (let i = 0; i < cells.length - 1; i += 2) {
              const key = cells[i].replace(/[：:]/g, '');
              const value = cells[i + 1];
              if (key && value) fields[key] = value;
            }
            const first = cells[0].replace(/[：:]/g, '');
            if (first && cells[1]) fields[first] = cells[1];
          }
          const labels = [...document.querySelectorAll('label,span,div,td,th')];
          for (const label of labels) {
            if (!visible(label)) continue;
            const key = clean(label.innerText).replace(/[：:]/g, '');
            if (!/^(证书编号|有效期至)$/.test(key)) continue;
            let node = label.nextElementSibling;
            while (node && (!visible(node) || !clean(node.innerText))) node = node.nextElementSibling;
            if (node) fields[key] = clean(node.innerText);
          }
          return fields;
        }
        """
    merged: dict[str, str] = {}
    for frame in candidate_frames(page):
        try:
            fields = frame.evaluate(script)
            for key, value in fields.items():
                if key in DETAIL_LABELS and normalize_text(value):
                    merged[key] = normalize_text(value)
        except Exception:
            continue
    text_fields = extract_detail_fields_from_text(visible_page_text(page))
    for key in DETAIL_LABELS:
        if key not in merged and text_fields.get(key):
            merged[key] = text_fields[key]
    return merged


def extract_detail_record_from_page(page, record: dict[str, str]) -> dict[str, str]:
    fields = extract_detail_fields_from_dom(page)
    detail_cert = normalize_text(fields.get("证书编号", ""))
    expiry = normalize_date(fields.get("有效期至", ""))
    if not detail_cert and not expiry:
        return {}
    cert_no = normalize_text(record.get("cert_no", ""))
    cert_key = normalize_cert_key(cert_no)
    detail_key = normalize_cert_key(detail_cert)
    if detail_key and cert_key and not (cert_key == detail_key or cert_key in detail_key or detail_key in cert_key):
        return {}
    detail_name = normalize_text(fields.get("姓名", ""))
    target_name = normalize_text(record.get("name", ""))
    if detail_name and target_name and detail_name != target_name:
        return {}
    return {
        "company": fields.get("企业名称", record.get("company", "")),
        "name": detail_name or record.get("name", ""),
        "cert_no": detail_cert or record.get("cert_no", ""),
        "expires_at": expiry,
        "level": infer_level({"level": "", "cert_no": detail_cert or record.get("cert_no", ""), "raw": ""}) or record.get("level", ""),
        "raw": record.get("raw", ""),
    }


def apply_detail_record(record: dict[str, str], detail: dict[str, str]) -> None:
    for key in ("company", "name", "cert_no", "expires_at", "level"):
        if detail.get(key):
            record[key] = detail[key]


def click_detail_for_records(page, records: list[dict[str, str]], log=print) -> bool:
    first = records[0]
    raw = first.get("_source_raw") or first.get("raw", "")
    name = first.get("_source_name") or first.get("name", "")
    company = first.get("company", "")
    row_index = first.get("_source_row_index", "")
    row_key = first.get("_source_row_key", "")
    cert_numbers = [item.get("cert_no", "") for item in records if item.get("cert_no")]
    script = """
        ({ raw, name, company, certNumbers, rowKey }) => {
          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const certKey = (s) => clean(s).replace(/[（）]/g, (m) => m === '（' ? '(' : ')').replace(/\\s+/g, '').toUpperCase();
          const companyKey = clean(company);
          const visible = (el) => {
            const box = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return box.width > 0 && box.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
          };
          const scrollers = [...document.querySelectorAll('*')].filter((el) => {
            const box = el.getBoundingClientRect();
            return box.width > 0 && box.height > 0 && el.scrollWidth > el.clientWidth + 20;
          });
          for (const scroller of scrollers) scroller.scrollLeft = scroller.scrollWidth;

          const targetCertKeys = certNumbers.map(certKey).filter(Boolean);
          const detailLinks = [...document.querySelectorAll('a,button,span,[role="button"],.ant-btn')]
            .filter((el) => visible(el) && /详情|查看/.test(clean(el.innerText || el.value || el.title || '')));
          const rowMatchesTarget = (row) => {
            const text = clean(row.innerText);
            if (!text) return false;
            if (companyKey && !text.includes(companyKey)) return false;
            const hasName = name && text.includes(name);
            const normalized = certKey(text);
            const hasCert = targetCertKeys.some((cert) => cert && normalized.includes(cert));
            if (raw && text === raw) return true;
            return Boolean(hasName || hasCert);
          };
          const pointFor = (el, method) => {
            el.scrollIntoView({ block: 'center', inline: 'center' });
            const box = el.getBoundingClientRect();
            return {
              found: true,
              x: box.left + box.width / 2,
              y: box.top + box.height / 2,
              method,
              detailCount: detailLinks.length
            };
          };
          if (rowKey) {
            const keyedRows = [...document.querySelectorAll(`tr[data-row-key="${CSS.escape(rowKey)}"]`)].filter(visible);
            for (const keyedRow of keyedRows) {
              if (!rowMatchesTarget(keyedRow)) continue;
              const detail = [...keyedRow.querySelectorAll('a,button,span,[role="button"],.ant-btn')]
                .filter(visible)
                .find((el) => /详情|查看/.test(clean(el.innerText || el.value || el.title || '')));
              if (detail) return pointFor(detail, `row-key:${rowKey}`);
            }
            const keyedDetail = detailLinks.find((el) => {
              const tr = el.closest('tr');
              return tr && tr.getAttribute('data-row-key') === rowKey && rowMatchesTarget(tr);
            });
            if (keyedDetail) return pointFor(keyedDetail, `row-key-link:${rowKey}`);
          }
          const rows = [...document.querySelectorAll('tr')].filter(visible);
          const target = rows.find(rowMatchesTarget);
          if (!target) return false;
          const controls = [...target.querySelectorAll('button,a,span,[role="button"],.ant-btn')].filter(visible);
          const detail = controls.find((el) => /详情|查看/.test(clean(el.innerText || el.value || el.title || '')));
          if (!detail) return false;
          return pointFor(detail, 'same-row');
        }
        """
    payload = {"raw": raw, "name": name, "company": company, "certNumbers": cert_numbers, "rowKey": row_key}
    for frame in candidate_frames(page):
        try:
            result = frame.evaluate(script, payload)
            if result and result.get("found"):
                x = float(result["x"])
                y = float(result["y"])
                if frame != page.main_frame:
                    frame_box = frame.frame_element().bounding_box()
                    if frame_box:
                        x += float(frame_box["x"])
                        y += float(frame_box["y"])
                before_url = page.url
                before_text = visible_page_text(page)
                page.mouse.click(x, y)
                log(f"已用鼠标点击 {name} 的详情坐标（{result.get('method')}，本页详情按钮数 {result.get('detailCount')}）。")
                if wait_for_detail_page(page, before_url, before_text, timeout_seconds=8):
                    log(f"{name} 已进入详情页。")
                    return True
                log(f"{name} 点击后未检测到详情页切换，将尝试下一个定位方式。")
        except Exception:
            continue
    return False


def wait_for_detail_page(page, before_url: str, before_text: str, timeout_seconds: int = 8) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        text = visible_page_text(page)
        if page.url != before_url and "证书编号" in text:
            return True
        if "返回" in text and "证书编号" in text and ("有效期至" in text or text != before_text):
            return True
        page.wait_for_timeout(500)
    return False


def ensure_company_results(page, company: str, page_no: int = 1, log=print) -> bool:
    rows = normalize_rows(extract_visible_tables(page), company)
    if rows and page_no <= 1:
        return True

    text = visible_page_text(page)
    if "有效期至" in text and "返回" in text:
        close_detail(page)
        page.wait_for_timeout(1500)

    try:
        fill_company(page, company)
    except Exception:
        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        fill_company(page, company)

    if submit_query(page):
        log(f"已重新查询：{company}")
    else:
        log("重新查询时未能自动点击查询按钮，请检查页面。")
    wait_for_query_results(page, company, log=log, timeout_seconds=60)

    for idx in range(1, page_no):
        if not next_page(page):
            log(f"无法跳转到第 {page_no} 页，停留在第 {idx} 页。")
            return False
        wait_for_query_results(page, company, log=log, timeout_seconds=45)
    return bool(normalize_rows(extract_visible_tables(page), company))


def visible_page_text(page) -> str:
    chunks: list[str] = []
    for frame in candidate_frames(page):
        try:
            text = frame.locator("body").inner_text(timeout=1500)
            if text:
                chunks.append(text)
        except Exception:
            continue
    return "\n".join(chunks)


def close_detail(page) -> bool:
    selectors = (
        '.ant-modal-close',
        '.ant-drawer-close',
        'button:has-text("关闭")',
        'button:has-text("返回")',
        'a:has-text("返回")',
        '.ant-btn:has-text("返回")',
    )
    if click_first(page, selectors, timeout=1000):
        page.wait_for_timeout(1000)
        return True
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(700)
        return True
    except Exception:
        return False


def wait_for_builder_results(page, name: str, log=print, timeout_seconds: int = 60) -> bool:
    deadline = time.time() + timeout_seconds
    last_log = 0.0
    stable_hits = 0
    last_signature = ""
    while time.time() < deadline:
        rows = extract_visible_tables(page)
        matches = [
            row
            for row in rows
            if name in normalize_text(row.get("raw", "")) and "建造师" in normalize_text(row.get("raw", ""))
        ]
        state = page_loading_state(page)
        signature = "\n".join(normalize_text(row.get("raw", "")) for row in matches)
        if matches and signature == last_signature and not state["loading"]:
            stable_hits += 1
        elif matches:
            stable_hits = 1
            last_signature = signature
        else:
            stable_hits = 0
            last_signature = ""
        if matches and stable_hits >= 3:
            log(f"建造师查询结果已稳定：{name}，发现 {len(matches)} 条候选记录。")
            return True
        now = time.time()
        if now - last_log >= 5:
            log(f"正在等待建造师查询结果加载：{name}")
            last_log = now
        page.wait_for_timeout(1000)
    log(f"等待 {name} 的建造师查询结果超时。")
    return False


def click_builder_detail(page, name: str, log=print) -> bool:
    script = """
        ({ name }) => {
          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const visible = (el) => {
            const box = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return box.width > 0 && box.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
          };
          const scrollers = [...document.querySelectorAll('*')].filter((el) => {
            const box = el.getBoundingClientRect();
            return box.width > 0 && box.height > 0 && el.scrollWidth > el.clientWidth + 20;
          });
          for (const scroller of scrollers) scroller.scrollLeft = scroller.scrollWidth;
          const rows = [...document.querySelectorAll('tr')].filter(visible);
          const target = rows.find((row) => {
            const text = clean(row.innerText);
            return text.includes(name) && text.includes('建造师');
          });
          if (!target) return null;
          const controls = [...target.querySelectorAll('button,a,span,[role="button"],.ant-btn')].filter(visible);
          const detail = controls.find((el) => /详情|查看/.test(clean(el.innerText || el.value || el.title || '')));
          if (!detail) return null;
          detail.scrollIntoView({ block: 'center', inline: 'center' });
          const box = detail.getBoundingClientRect();
          return { x: box.left + box.width / 2, y: box.top + box.height / 2, row: clean(target.innerText) };
        }
        """
    for frame in candidate_frames(page):
        try:
            result = frame.evaluate(script, {"name": name})
            if not result:
                continue
            x = float(result["x"])
            y = float(result["y"])
            if frame != page.main_frame:
                frame_box = frame.frame_element().bounding_box()
                if frame_box:
                    x += float(frame_box["x"])
                    y += float(frame_box["y"])
            before_url = page.url
            before_text = visible_page_text(page)
            page.mouse.click(x, y)
            log(f"已点击建造师详情：{name}")
            if wait_for_builder_detail_page(page, before_url, before_text, timeout_seconds=12):
                return True
        except Exception:
            continue
    return False


def wait_for_builder_detail_page(page, before_url: str, before_text: str, timeout_seconds: int = 12) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        text = visible_page_text(page)
        if page.url != before_url and ("执业印章号" in text or "注册专业" in text):
            return True
        if "返回" in text and ("执业印章号" in text or "注册专业" in text or text != before_text):
            return True
        page.wait_for_timeout(500)
    return False


def extract_visible_label_fields(page, aliases: dict[str, tuple[str, ...]]) -> dict[str, str]:
    labels = [label for values in aliases.values() for label in values]
    script = """
        ({ labels }) => {
          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const hiddenByAncestor = (el) => {
            for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
              const ariaHidden = node.getAttribute && node.getAttribute('aria-hidden');
              const cls = String(node.className || '');
              if (ariaHidden === 'true' || /ant-tabs-tabpane-inactive/.test(cls)) return true;
            }
            return false;
          };
          const visible = (el) => {
            if (!el || hiddenByAncestor(el)) return false;
            const box = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return box.width > 0 && box.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
          };
          const wanted = new Set(labels);
          const fields = {};
          for (const row of [...document.querySelectorAll('tr')]) {
            if (!visible(row)) continue;
            const cells = [...row.querySelectorAll('th,td')].map((cell) => clean(cell.innerText));
            for (let i = 0; i < cells.length - 1; i++) {
              const key = cells[i].replace(/[：:]/g, '');
              if (wanted.has(key) && cells[i + 1]) fields[key] = cells[i + 1];
            }
          }
          return fields;
        }
        """
    raw: dict[str, str] = {}
    for frame in candidate_frames(page):
        try:
            values = frame.evaluate(script, {"labels": labels})
            for key, value in values.items():
                if normalize_text(value):
                    raw[normalize_text(key)] = normalize_text(value)
        except Exception:
            continue
    mapped: dict[str, str] = {}
    for field, field_aliases in aliases.items():
        for alias in field_aliases:
            if alias in raw:
                mapped[field] = raw[alias]
                break
    return mapped


def extract_builder_license_rows(page) -> list[dict[str, str]]:
    script = """
        () => {
          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const visible = (el) => {
            const box = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return box.width > 0 && box.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
          };
          const rows = [];
          for (const tr of [...document.querySelectorAll('tbody tr')]) {
            if (!visible(tr)) continue;
            const cells = [...tr.querySelectorAll('td,th')].map((cell) => clean(cell.innerText));
            if (cells.length < 7) continue;
            const text = cells.join(' ');
            if (!text.includes('建造师')) continue;
            rows.push({
              category: cells[1] || '',
              id_no: cells[2] || '',
              seal_no: cells[3] || '',
              valid_from: cells[5] || '',
              valid_to: cells[6] || '',
              major: cells[7] || '',
              raw: text
            });
          }
          return rows;
        }
        """
    result: list[dict[str, str]] = []
    for frame in candidate_frames(page):
        try:
            rows = frame.evaluate(script)
            for row in rows:
                result.append({key: normalize_text(value) for key, value in row.items()})
        except Exception:
            continue
    return result


def builder_detail_loading_state(page) -> dict[str, object]:
    script = """
        () => {
          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const visible = (el) => {
            const box = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return box.width > 0 && box.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
          };
          const loading = [...document.querySelectorAll('.ant-spin-spinning,.ant-spin-dot-spin,.ant-spin-dot')]
            .some(visible);
          const rows = [...document.querySelectorAll('tbody tr')]
            .filter(visible)
            .map((tr) => clean(tr.innerText))
            .filter((text) => text && !/暂无数据|No Data/.test(text));
          return {
            loading,
            rowCount: rows.length,
            builderRows: rows.filter((text) => text.includes('建造师')).length,
            hasNoData: /暂无数据|No Data/.test(document.body ? document.body.innerText : '')
          };
        }
        """
    states: list[dict[str, object]] = []
    for frame in candidate_frames(page):
        try:
            states.append(frame.evaluate(script))
        except Exception:
            continue
    return {
        "loading": any(bool(item.get("loading")) for item in states),
        "row_count": sum(int(item.get("rowCount") or 0) for item in states),
        "builder_rows": sum(int(item.get("builderRows") or 0) for item in states),
        "has_no_data": any(bool(item.get("hasNoData")) for item in states),
    }


def wait_for_builder_detail_rows(page, name: str, log=print, timeout_seconds: int = 90) -> bool:
    deadline = time.time() + timeout_seconds
    last_log = 0.0
    stable_hits = 0
    last_count = -1
    while time.time() < deadline:
        state = builder_detail_loading_state(page)
        builder_rows = int(state.get("builder_rows") or 0)
        if builder_rows > 0 and not state.get("loading"):
            if builder_rows == last_count:
                stable_hits += 1
            else:
                stable_hits = 1
                last_count = builder_rows
            if stable_hits >= 2:
                log(f"{name} 的建造师注册明细已加载，发现 {builder_rows} 条建造师记录。")
                return True
        else:
            stable_hits = 0
            last_count = builder_rows
        now = time.time()
        if now - last_log >= 5:
            if state.get("loading"):
                log(f"正在等待 {name} 的建造师注册明细加载...")
            elif state.get("has_no_data"):
                log(f"{name} 的建造师明细表暂显示暂无数据，继续等待刷新...")
            else:
                log(f"正在等待 {name} 的建造师明细行出现...")
            last_log = now
        page.wait_for_timeout(1000)
    log(f"等待 {name} 的建造师注册明细超时。")
    return False


def extract_builder_detail_records(page, company: str, name: str, log=print) -> list[dict[str, str]]:
    top_fields = extract_visible_label_fields(page, BUILDER_LABEL_ALIASES)
    detail_company = normalize_text(top_fields.get("company", ""))
    detail_name = normalize_text(top_fields.get("name", "")) or normalize_text(name)
    if detail_name and normalize_text(name) and detail_name != normalize_text(name):
        log(f"跳过建造师详情：当前详情姓名为“{detail_name}”，不是“{name}”。")
        return []
    if detail_company and normalize_text(company) not in detail_company and detail_company not in normalize_text(company):
        log(f"跳过建造师详情：{detail_name} 的注册单位为“{detail_company}”，与查询单位不一致。")
        return []

    rows = extract_visible_tables(page)
    records: list[dict[str, str]] = []
    for row in rows:
        cells = [normalize_text(cell) for cell in row.get("__cells", [])]
        category = normalize_text(row.get("注册类别", "")) or (cells[1] if len(cells) > 1 else "")
        if "建造师" not in category:
            continue
        id_no = normalize_text(row.get("证件编号", "")) or (cells[2] if len(cells) > 2 else "")
        seal_no = normalize_text(row.get("执业印章号", "")) or (cells[3] if len(cells) > 3 else "")
        valid_from = normalize_text(row.get("注册证书有效期开始", "")) or (cells[5] if len(cells) > 5 else "")
        valid_to = normalize_text(row.get("注册证书有效期结束", "")) or (cells[6] if len(cells) > 6 else "")
        major = normalize_text(row.get("注册专业", "")) or (cells[7] if len(cells) > 7 else "")
        record = {
            "name": detail_name,
            "gender": top_fields.get("gender", ""),
            "register_category": category,
            "id_no": id_no,
            "seal_no": seal_no,
            "valid_from": normalize_date(valid_from),
            "valid_to": normalize_date(valid_to),
            "major": major,
            "company": detail_company,
        }
        records.append(record)

    if not records:
        for row in extract_builder_license_rows(page):
            category = normalize_text(row.get("category", ""))
            if "建造师" not in category:
                continue
            records.append(
                {
                    "name": detail_name,
                    "gender": top_fields.get("gender", ""),
                    "register_category": category,
                    "id_no": row.get("id_no", ""),
                    "seal_no": row.get("seal_no", ""),
                    "valid_from": normalize_date(row.get("valid_from", "")),
                    "valid_to": normalize_date(row.get("valid_to", "")),
                    "major": row.get("major", ""),
                    "company": detail_company,
                }
            )

    if not records:
        # Some page variants may expose the builder record as a two-column detail table.
        category = normalize_text(top_fields.get("register_category", ""))
        if "建造师" in category:
            records.append(
                {
                    "name": detail_name,
                    "gender": top_fields.get("gender", ""),
                    "register_category": category,
                    "id_no": top_fields.get("id_no", ""),
                    "seal_no": top_fields.get("seal_no", ""),
                    "valid_from": normalize_date(top_fields.get("valid_from", "")),
                    "valid_to": normalize_date(top_fields.get("valid_to", "")),
                    "major": top_fields.get("major", ""),
                    "company": detail_company,
                }
            )
    return records


def collect_builder_for_name(page, company: str, name: str, wait_for_user=None, log=print) -> list[dict[str, str]]:
    name = normalize_text(name)
    if not name:
        return []
    log(f"\n正在查询建造师：{name}")
    page.goto(BUILDER_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    fill_person_name(page, name)
    if submit_query(page):
        log(f"已提交建造师姓名查询：{name}")
    else:
        log("未能自动识别建造师查询按钮，请手动点击查询。")
    if not wait_for_builder_results(page, name, log=log, timeout_seconds=25):
        log("如果建造师页面需要滑块验证或仍在加载，请手动完成验证并等待结果行出现后继续。")
        if wait_for_user:
            wait_for_user()
        if not wait_for_builder_results(page, name, log=log, timeout_seconds=70):
            return []
    if not click_builder_detail(page, name, log=log):
        log(f"未找到 {name} 的建造师详情入口。")
        return []
    if not wait_for_builder_detail_rows(page, name, log=log, timeout_seconds=90):
        debug_html = save_debug_html(page, f"建造师_{name}")
        log(f"{name} 的建造师详情明细未加载完成，已保存调试页面：{debug_html}")
        close_detail(page)
        return []
    records = extract_builder_detail_records(page, company, name, log=log)
    if records:
        for record in records:
            log(f"已采集建造师：{record['name']} / {record['register_category']} / {record['valid_to']}")
        close_detail(page)
        return records
    debug_html = save_debug_html(page, f"建造师_{name}")
    log(f"{name} 的详情页已加载，但未解析出单位一致的建造师注册明细，已保存调试页面：{debug_html}")
    close_detail(page)
    return []


def collect_builders(page, company: str, names: list[str], wait_for_user=None, log=print) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for name in names:
        name = normalize_text(name)
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        try:
            records.extend(collect_builder_for_name(page, company, name, wait_for_user=wait_for_user, log=log))
        except Exception as exc:
            log(f"建造师 {name} 查询失败：{exc}")
    return records


def click_detail_cert_tab(page, record: dict[str, str], log=print) -> bool:
    cert_no = record.get("cert_no", "")
    script = """
        ({ certNo }) => {
          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const certKey = (s) => clean(s).replace(/[（）]/g, (m) => m === '（' ? '(' : ')').replace(/\\s+/g, '').toUpperCase();
          const target = certKey(certNo);
          const visible = (el) => {
            const box = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return box.width > 0 && box.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
          };
          const candidates = [...document.querySelectorAll('[role="tab"].ant-tabs-tab, [role="tab"], .ant-tabs-tab')]
            .filter((el) => visible(el) && certKey(el.innerText || el.textContent || '').includes(target));
          if (!candidates.length) return null;
          let best = candidates[0];
          for (const candidate of candidates) {
            const cls = String(candidate.className || '');
            if (/active|selected/.test(cls)) {
              best = candidate;
              break;
            }
          }
          best.scrollIntoView({ block: 'center', inline: 'center' });
          const box = best.getBoundingClientRect();
          return { x: box.left + box.width / 2, y: box.top + box.height / 2, text: clean(best.innerText || best.textContent || '') };
        }
        """
    for frame in candidate_frames(page):
        try:
            result = frame.evaluate(script, {"certNo": cert_no})
            if not result:
                continue
            x = float(result["x"])
            y = float(result["y"])
            if frame != page.main_frame:
                frame_box = frame.frame_element().bounding_box()
                if frame_box:
                    x += float(frame_box["x"])
                    y += float(frame_box["y"])
            page.mouse.click(x, y)
            log(f"已点击详情页证书标签：{result.get('text')}")
            page.wait_for_timeout(800)
            return True
        except Exception:
            continue
    return False


def wait_for_detail_record(page, record: dict[str, str], log=print, timeout_seconds: int = 45) -> dict[str, str]:
    deadline = time.time() + timeout_seconds
    last_log = 0.0
    while time.time() < deadline:
        detail = extract_detail_record_from_page(page, record)
        if detail and detail.get("expires_at"):
            return detail
        now = time.time()
        if now - last_log >= 8:
            log(f"正在等待 {record.get('name', '')} / {record.get('cert_no', '')} 的详情页加载...")
            last_log = now
        page.wait_for_timeout(1000)
    return extract_detail_record_from_page(page, record)


def visible_detail_entry_count(page) -> int:
    script = """
        () => {
          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const visible = (el) => {
            const box = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return box.width > 0 && box.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
          };
          const scrollers = [...document.querySelectorAll('*')].filter((el) => {
            const box = el.getBoundingClientRect();
            return box.width > 0 && box.height > 0 && el.scrollWidth > el.clientWidth + 20;
          });
          for (const scroller of scrollers) scroller.scrollLeft = scroller.scrollWidth;
          return [...document.querySelectorAll('a,button,span,[role="button"],.ant-btn')]
            .filter((el) => visible(el) && /详情|查看/.test(clean(el.innerText || el.value || el.title || ''))).length;
        }
        """
    counts = []
    for frame in candidate_frames(page):
        try:
            counts.append(int(frame.evaluate(script) or 0))
        except Exception:
            continue
    return max(counts or [0])


def click_detail_entry_by_index(page, index: int, log=print) -> bool:
    script = """
        ({ index }) => {
          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const visible = (el) => {
            const box = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return box.width > 0 && box.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
          };
          const scrollers = [...document.querySelectorAll('*')].filter((el) => {
            const box = el.getBoundingClientRect();
            return box.width > 0 && box.height > 0 && el.scrollWidth > el.clientWidth + 20;
          });
          for (const scroller of scrollers) scroller.scrollLeft = scroller.scrollWidth;
          const links = [...document.querySelectorAll('a,button,span,[role="button"],.ant-btn')]
            .filter((el) => visible(el) && /详情|查看/.test(clean(el.innerText || el.value || el.title || '')));
          const link = links[index];
          if (!link) return null;
          link.scrollIntoView({ block: 'center', inline: 'center' });
          const box = link.getBoundingClientRect();
          const row = link.closest('tr');
          return {
            x: box.left + box.width / 2,
            y: box.top + box.height / 2,
            count: links.length,
            rowKey: row ? row.getAttribute('data-row-key') || '' : ''
          };
        }
        """
    for frame in candidate_frames(page):
        try:
            result = frame.evaluate(script, {"index": index})
            if not result:
                continue
            x = float(result["x"])
            y = float(result["y"])
            if frame != page.main_frame:
                frame_box = frame.frame_element().bounding_box()
                if frame_box:
                    x += float(frame_box["x"])
                    y += float(frame_box["y"])
            before_url = page.url
            before_text = visible_page_text(page)
            page.mouse.click(x, y)
            log(f"已点击第 {index + 1} 个详情入口（本页详情按钮数 {result.get('count')}，row-key {result.get('rowKey') or '-'}）。")
            if wait_for_detail_page(page, before_url, before_text, timeout_seconds=10):
                return True
            log(f"第 {index + 1} 个详情入口点击后未检测到详情页。")
        except Exception:
            continue
    return False


def detail_tab_texts(page) -> list[str]:
    script = """
        () => {
          const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
          const visible = (el) => {
            const box = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return box.width > 0 && box.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
          };
          return [...document.querySelectorAll('[role="tab"],.ant-tabs-tab')]
            .filter(visible)
            .map((el) => clean(el.innerText || el.textContent || ''))
            .filter((text) => /津建安/.test(text));
        }
        """
    for frame in candidate_frames(page):
        try:
            values = frame.evaluate(script)
            if values:
                return [normalize_text(item) for item in values]
        except Exception:
            continue
    fields = extract_detail_fields_from_dom(page)
    return [fields["证书编号"]] if fields.get("证书编号") else []


def collect_all_details_from_current_detail_page(page, company: str, log=print) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    tabs = detail_tab_texts(page)
    if not tabs:
        tabs = [""]
    log(f"详情页发现 {len(tabs)} 个证书标签。")
    for tab in tabs:
        probe = {"company": company, "name": "", "cert_no": tab, "level": infer_level({"cert_no": tab, "level": "", "raw": ""}), "raw": ""}
        if tab and not click_detail_cert_tab(page, probe, log=log):
            log(f"未能点击详情证书标签：{tab}，尝试读取当前激活详情。")
        detail = wait_for_detail_record(page, probe, log=log, timeout_seconds=45)
        if not detail or not detail.get("cert_no") or not detail.get("expires_at"):
            debug_html = save_debug_html(page, f"detail_pool_{tab or 'current'}")
            log(f"详情证书采集不完整，已保存调试页面：{debug_html}")
            continue
        if normalize_text(detail.get("company", "")) != normalize_text(company):
            log(f"跳过非目标公司详情：{detail.get('company', '')} / {detail.get('name', '')}")
            continue
        records.append(detail)
        log(f"已采集详情证书：{detail.get('level', '')} / {detail.get('name', '')} / {detail.get('cert_no', '')} / {detail.get('expires_at', '')}")
    return records


def enrich_expiry_from_details(page, records: list[dict[str, str]], log=print) -> list[dict[str, str]]:
    groups: dict[str, list[dict[str, str]]] = {}
    for record in records:
        key = record.get("_source_raw") or f"{record.get('name')}|{record.get('cert_no')}"
        groups.setdefault(key, []).append(record)

    total = len(groups)
    for idx, group in enumerate(groups.values(), start=1):
        if all(item.get("expires_at") for item in group):
            continue
        name = group[0].get("name", "")
        company = group[0].get("company", "")
        try:
            page_no = int(group[0].get("_source_page_no") or 1)
        except ValueError:
            page_no = 1
        log(f"正在打开详情补充有效期：{name}（{idx}/{total}）")
        if company and not ensure_company_results(page, company, page_no=page_no, log=log):
            log(f"未能重新定位到 {name} 所在列表页，已跳过有效期补充。")
            continue
        if not click_detail_for_records(page, group, log=log):
            log(f"未找到 {name} 的详情按钮，已跳过有效期补充。")
            continue
        for item in group:
            if not click_detail_cert_tab(page, item, log=log):
                log(f"未找到详情页证书标签：{item.get('cert_no', '')}，尝试读取当前详情表。")
            detail = wait_for_detail_record(page, item, log=log, timeout_seconds=45)
            if detail and detail.get("expires_at"):
                apply_detail_record(item, detail)
                log(f"已采集详情：{item.get('name', '')} / {item.get('cert_no', '')} / {item.get('expires_at', '')}")
            else:
                log(f"{item.get('name', '')} / {item.get('cert_no', '')} 未识别到有效期，保留列表数据继续。")
                debug_html = save_debug_html(page, f"detail_{item.get('name', '')}_{item.get('cert_no', '')}")
                log(f"已保存详情页调试页面：{debug_html}")
        close_detail(page)
        page.wait_for_timeout(1500)
    return records


def next_page(page) -> bool:
    disabled_hint = re.compile(r"disabled|layui-disabled|pagination-disabled|el-pager.*disabled", re.I)
    for frame in candidate_frames(page):
        candidates = frame.locator('a:has-text("下一页"), button:has-text("下一页"), li:has-text("下一页"), .ant-pagination-next')
        count = candidates.count()
        for idx in range(count):
            item = candidates.nth(idx)
            try:
                if not item.is_visible(timeout=800):
                    continue
                class_name = item.get_attribute("class") or ""
                aria_disabled = item.get_attribute("aria-disabled") or ""
                if aria_disabled.lower() == "true" or disabled_hint.search(class_name):
                    return False
                item.click(timeout=1500)
                page.wait_for_timeout(1200)
                return True
            except Exception:
                continue
    return False


def collect_company(page, task: CompanyTask, max_pages: int, wait_for_user=None, log=print) -> tuple[list[dict[str, str]], Path]:
    log(f"\n正在查询：{task.company}")
    page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    fill_company(page, task.company)
    if submit_query(page):
        log("已自动点击查询。")
    else:
        log("未能自动识别查询按钮，请在浏览器中手动点击查询。")

    log("请在浏览器中完成滑块验证，并等待表格出现人员结果行后继续。")
    log("如果页面一直显示“暂无数据”或转圈，请手动再点一次“查询”，完成验证并等结果行出现。")
    if wait_for_user:
        wait_for_user()
    else:
        input("完成后按 Enter 继续采集...")
    wait_for_query_results(page, task.company, log=log, timeout_seconds=60)

    records: list[dict[str, str]] = []
    seen_pages: set[str] = set()
    for page_no in range(1, max_pages + 1):
        page.wait_for_timeout(800)
        wait_for_query_results(page, task.company, log=log, timeout_seconds=30)
        rows = extract_visible_tables(page)
        page_records = normalize_rows(rows, task.company)
        for item in page_records:
            item["_source_page_no"] = str(page_no)
        log(f"本页识别到 {len(page_records)} 条目标公司证书记录。")
        signature = "\n".join(item["raw"] for item in page_records)
        if signature and signature in seen_pages:
            break
        if signature:
            seen_pages.add(signature)
        page_needed = select_remaining_targets(page_records, records, task.targets, task.required_names)
        if page_needed:
            log(f"本页选中 {len(page_needed)} 条证书，逐条进入详情补充有效期。")
            try:
                enrich_expiry_from_details(page, page_needed, log=log)
                if not ensure_company_results(page, task.company, page_no=page_no, log=log):
                    log("详情返回后未等到目标公司稳定结果，停止当前页采集。")
                    break
            except Exception as exc:
                log(f"本页补充有效期失败，保留列表数据继续。原因：{exc}")
            records = dedupe_records(records + page_needed)
            log_counts(records, task.targets, log=log)
        else:
            log("本页没有符合剩余 A/B/C 数量要求的证书。")
        if enough_records(records, task.targets, task.required_names):
            break
        if not ensure_company_results(page, task.company, page_no=page_no, log=log):
            break
        if not next_page(page):
            break
        log(f"已翻到第 {page_no + 1} 页...")

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    screenshot = SCREENSHOT_DIR / f"{sanitize_filename(task.company)}_{datetime.now():%Y%m%d_%H%M%S}.png"
    page.screenshot(path=str(screenshot), full_page=True)
    if not records:
        debug_html = save_debug_html(page, task.company)
        log(f"未识别到数据，已保存调试页面：{debug_html}")
    for level in ("A", "B", "C"):
        for required_name in names_for_level(task, level):
            if not selected_has_name(records, level, required_name):
                log(f"提示：指定 {level}证姓名“{required_name}”未在已采集结果中找到。")
    return dedupe_records(records), screenshot


def names_for_level(task: CompanyTask, level: str) -> list[str]:
    return [
        normalize_text(name)
        for name in task.required_names.get(level, [])
        if normalize_text(name)
    ]


def selected_has_name(records: list[dict[str, str]], level: str, name: str) -> bool:
    return any(
        normalize_text(item.get("level", "")).upper().startswith(level)
        and normalize_text(item.get("name", "")) == normalize_text(name)
        for item in records
    )


def record_key(record: dict[str, str]) -> tuple[str, str, str]:
    return (record.get("company", ""), record.get("name", ""), record.get("cert_no", ""))


def select_remaining_targets(
    page_records: list[dict[str, str]],
    selected_records: list[dict[str, str]],
    targets: dict[str, int],
    required_names: dict[str, list[str]] | None = None,
) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    required_names = required_names or {}
    seen = {record_key(item) for item in selected_records}
    for level in ("A", "B", "C"):
        target = targets.get(level, 0)
        if target <= 0:
            continue
        level_records = [
            item
            for item in page_records
            if normalize_text(item.get("level", "")).upper().startswith(level)
        ]
        for required_name in required_names.get(level, []):
            if selected_has_name(selected_records + selected, level, required_name):
                continue
            for item in level_records:
                if normalize_text(item.get("name", "")) == normalize_text(required_name) and record_key(item) not in seen:
                    selected.append(item)
                    seen.add(record_key(item))
                    break
        current = sum(
            1
            for item in selected_records + selected
            if normalize_text(item.get("level", "")).upper().startswith(level)
        )
        remaining = target - current
        if remaining <= 0:
            continue
        for item in level_records:
            if record_key(item) in seen:
                continue
            selected.append(item)
            seen.add(record_key(item))
            remaining -= 1
            if remaining <= 0:
                break
    return selected


def enough_records(records: list[dict[str, str]], targets: dict[str, int], required_names: dict[str, list[str]] | None = None) -> bool:
    required_names = required_names or {}
    for level, target in targets.items():
        if target <= 0:
            continue
        count = sum(1 for item in records if normalize_text(item.get("level", "")).upper().startswith(level))
        if count < target:
            return False
        for required_name in required_names.get(level, []):
            if required_name and not selected_has_name(records, level, required_name):
                return False
    return True


def log_counts(records: list[dict[str, str]], targets: dict[str, int], log=print) -> None:
    parts = []
    for level in ("A", "B", "C"):
        target = targets.get(level, 0)
        if target <= 0:
            continue
        count = sum(1 for item in records if normalize_text(item.get("level", "")).upper().startswith(level))
        parts.append(f"{level}:{count}/{target}")
    if parts:
        log("当前证书数量：" + "，".join(parts))


def dedupe_records(records: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, str]] = []
    for record in records:
        key = (record.get("company", ""), record.get("name", ""), record.get("cert_no", ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def select_targets(records: list[dict[str, str]], targets: dict[str, int], required_names: dict[str, list[str]] | None = None) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    required_names = required_names or {}
    seen: set[tuple[str, str, str]] = set()
    for level in ("A", "B", "C"):
        target = targets.get(level, 0)
        if target <= 0:
            continue
        level_records = [item for item in records if normalize_text(item.get("level", "")).upper().startswith(level)]
        for required_name in required_names.get(level, []):
            for item in level_records:
                if normalize_text(item.get("name", "")) == normalize_text(required_name) and record_key(item) not in seen:
                    selected.append(item)
                    seen.add(record_key(item))
                    break
        remaining = target - sum(1 for item in selected if normalize_text(item.get("level", "")).upper().startswith(level))
        if remaining <= 0:
            continue
        for item in level_records:
            if record_key(item) in seen:
                continue
            selected.append(item)
            seen.add(record_key(item))
            remaining -= 1
            if remaining <= 0:
                break
    return selected


def default_output_path(tasks: list[CompanyTask]) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if len(tasks) == 1:
        name = sanitize_filename(tasks[0].company)
    else:
        name = "批量查询"
    return OUTPUT_DIR / f"{name}_{timestamp}.xlsx"


def export_results(records: list[dict[str, str]], output: Path, builder_records: list[dict[str, str]] | None = None) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    headers = ["公司名称", "证书类别", "姓名", "证书编号", "有效期至", "查询时间", "页面截图", "原始行"]
    rows = [
        {
            "公司名称": item.get("company", ""),
            "证书类别": item.get("level", ""),
            "姓名": item.get("name", ""),
            "证书编号": item.get("cert_no", ""),
            "有效期至": item.get("expires_at", ""),
            "查询时间": item.get("queried_at", ""),
            "页面截图": item.get("screenshot", ""),
            "原始行": item.get("raw", ""),
        }
        for item in records
    ]
    builder_records = builder_records or []
    builder_rows = [
        {
            "姓名": item.get("name", ""),
            "性别": item.get("gender", ""),
            "注册类别": item.get("register_category", ""),
            "证件编号": item.get("id_no", ""),
            "执业印章号": item.get("seal_no", ""),
            "注册证书有效期开始": item.get("valid_from", ""),
            "注册证书有效期结束": item.get("valid_to", ""),
            "注册专业": item.get("major", ""),
            "注册证书所在单位名称": item.get("company", ""),
            "查询时间": item.get("queried_at", ""),
        }
        for item in builder_records
    ]

    if output.suffix.lower() == ".xlsx":
        write_xlsx_multi(
            output,
            [
                ("安管人员证书", headers, rows, (24, 10, 12, 30, 16, 20, 50, 80)),
                ("建造师信息", BUILDER_EXPORT_HEADERS, builder_rows, (14, 8, 18, 24, 24, 18, 18, 24, 32, 20)),
            ],
        )
        print(f"已导出 Excel：{output}")
        return

    write_csv(output, headers, rows)
    print(f"已导出 CSV：{output}")


def write_csv(output: Path, headers: list[str], rows: list[dict[str, str]]) -> None:
    with output.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def write_xlsx(output: Path, headers: list[str], rows: list[dict[str, str]]) -> None:
    write_xlsx_multi(output, [("安管人员证书", headers, rows, (24, 10, 12, 30, 16, 20, 50, 80))])


def sheet_xml(headers: list[str], rows: list[dict[str, str]], widths: tuple[int, ...]) -> str:
    sheet_rows = [headers] + [[row.get(header, "") for header in headers] for row in rows]
    sheet_xml_rows = []
    for row_idx, values in enumerate(sheet_rows, start=1):
        cells = []
        for col_idx, value in enumerate(values, start=1):
            cell_ref = f"{column_name(col_idx)}{row_idx}"
            style = ' s="1"' if row_idx == 1 else ""
            cells.append(
                f'<c r="{cell_ref}" t="inlineStr"{style}><is><t>{escape(str(value))}</t></is></c>'
            )
        sheet_xml_rows.append(f'<row r="{row_idx}">{"".join(cells)}</row>')

    cols = "".join(
        f'<col min="{idx}" max="{idx}" width="{width}" customWidth="1"/>'
        for idx, width in enumerate(widths, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<cols>{cols}</cols>"
        f"<sheetData>{''.join(sheet_xml_rows)}</sheetData>"
        "</worksheet>"
    )


def write_xlsx_multi(output: Path, sheets: list[tuple[str, list[str], list[dict[str, str]], tuple[int, ...]]]) -> None:
    worksheets = [sheet_xml(headers, rows, widths) for _, headers, rows, widths in sheets]
    sheet_entries = "".join(
        f'<sheet name="{escape(name)}" sheetId="{idx}" r:id="rId{idx}"/>'
        for idx, (name, _, _, _) in enumerate(sheets, start=1)
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{sheet_entries}</sheets>"
        "</workbook>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<fonts count=\"2\"><font><sz val=\"11\"/><name val=\"Calibri\"/></font>"
        "<font><b/><sz val=\"11\"/><name val=\"Calibri\"/></font></fonts>"
        "<fills count=\"1\"><fill><patternFill patternType=\"none\"/></fill></fills>"
        "<borders count=\"1\"><border/></borders>"
        "<cellStyleXfs count=\"1\"><xf numFmtId=\"0\" fontId=\"0\" fillId=\"0\" borderId=\"0\"/></cellStyleXfs>"
        "<cellXfs count=\"2\"><xf numFmtId=\"0\" fontId=\"0\" fillId=\"0\" borderId=\"0\" xfId=\"0\"/>"
        "<xf numFmtId=\"0\" fontId=\"1\" fillId=\"0\" borderId=\"0\" xfId=\"0\"/></cellXfs>"
        "</styleSheet>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + "".join(
            f'<Override PartName="/xl/worksheets/sheet{idx}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for idx in range(1, len(sheets) + 1)
        )
        +
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            f'<Relationship Id="rId{idx}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{idx}.xml"/>'
            for idx in range(1, len(sheets) + 1)
        )
        + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    )

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/styles.xml", styles)
        for idx, worksheet in enumerate(worksheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{idx}.xml", worksheet)


def column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def print_summary(all_records: list[dict[str, str]], tasks: list[CompanyTask]) -> None:
    print("\n查询汇总")
    for task in tasks:
        company_records = [item for item in all_records if item.get("company") == task.company]
        parts = []
        for level in ("A", "B", "C"):
            found = sum(1 for item in company_records if item.get("level") == level)
            target = task.targets.get(level, 0)
            if target:
                parts.append(f"{level}证 {found}/{target}")
        print(f"- {task.company}: {'，'.join(parts) if parts else '未设置数量要求'}")


def main() -> int:
    args = parse_args()

    if sync_playwright is None:
        install_hint()
        return 2

    tasks = load_tasks(args)
    output = args.output or default_output_path(tasks)

    selected_all: list[dict[str, str]] = []
    with sync_playwright() as p:
        browser = launch_browser(p, headless=args.headless)
        context = browser.new_context(viewport={"width": 1366, "height": 900})
        page = context.new_page()
        try:
            for task in tasks:
                records, screenshot = collect_company(page, task, args.max_pages)
                selected = select_targets(records, task.targets, task.required_names)
                if not selected and records:
                    print("提示：按 A/B/C 数量筛选结果为空，已改为导出本次识别到的全部列表记录。")
                    selected = records
                queried_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for item in selected:
                    item["queried_at"] = queried_at
                    item["screenshot"] = str(screenshot)
                selected_all.extend(selected)
                for level in ("A", "B", "C"):
                    target = task.targets.get(level, 0)
                    if target:
                        found = sum(1 for item in selected if item.get("level") == level)
                        if found < target:
                            print(f"提示：{task.company} {level}证只摘录到 {found} 条，少 {target - found} 条。")
        finally:
            if args.keep_open:
                print("\n浏览器保持打开。按 Ctrl+C 结束脚本。")
                try:
                    while True:
                        time.sleep(1)
                except KeyboardInterrupt:
                    pass
            browser.close()

    export_results(selected_all, output)
    print_summary(selected_all, tasks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
