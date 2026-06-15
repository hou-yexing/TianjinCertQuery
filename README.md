# 天津安管人员证书查询工具

这个工具用于从天津住建委公开查询页面检索指定公司的安管人员证书，并按 A/B/C 证数量摘录：

- 姓名
- 证书编号
- 有效期至

页面存在滑块验证。工具不会绕过验证，而是在查询后暂停，由人工在浏览器中完成滑块，随后继续自动采集、翻页、筛选和导出。

列表页不直接展示“有效期至”时，工具会逐行点击“详情”，从详情页面按证书编号匹配并回填有效期。

## 安装

```powershell
python -m pip install -r requirements.txt --target .deps
$env:PYTHONPATH=".deps"
$env:PLAYWRIGHT_BROWSERS_PATH=".playwright-browsers"
python -m playwright install chromium
```

## 单家公司查询

图形界面：

```powershell
run_gui.bat
```

或直接执行：

```powershell
python query_cert_gui.py
```

命令行：

```powershell
python query_cert.py --company "某某建设有限公司" --a 2 --b 5 --c 10
```

运行后会打开浏览器。出现滑块时手动完成验证，看到查询结果后回到终端按 Enter。随后工具会自动点击结果行的“详情”补充有效期。

## 批量查询

准备 CSV，例如 `companies.csv`：

```csv
company,A,B,C
某某建设有限公司,2,5,10
天津某某工程有限公司,1,3,6
```

执行：

```powershell
python query_cert.py --input companies.csv
```

## 输出

默认输出到 `output/公司名称_日期_时间.xlsx`。Excel 文件由 Python 标准库生成，不需要额外 Excel 组件。

同时会保存每家公司查询结果页截图到 `output/screenshots/`，便于留痕复核。

## 常用参数

- `--output result.xlsx`：指定输出文件
- `--max-pages 20`：每家公司最多翻页数
- `--keep-open`：结束后保持浏览器打开
- `--headless`：无头模式，不建议用于滑块页面

## 合规边界

工具只自动化人工可见的正常查询流程，不破解滑块、不调用打码平台、不逆向接口 token。建议控制查询频率，并仅用于有正当用途的资质核验、台账整理或投标资料整理。

## 打包给其它电脑使用

在开发电脑上执行：

```powershell
build_windows.bat
```

完成后把整个目录 `dist\TianjinCertQuery\` 复制到其它 Windows 电脑，运行里面的 `TianjinCertQuery.exe` 即可。不要只复制单个 exe，因为 Playwright 浏览器文件也在这个目录中。
