# JSON 接口测试数据生成器

这是一个本机运行的 JSON 接口测试数据生成工具：根据请求体样例，生成必填字段的缺失、为空和类型错误测试数据 CSV。真实接口执行、鉴权和测试报告不在本工具范围内，请在 Postman 或 Apifox 中完成。

## 启动界面

Windows 下双击 [启动界面.bat](启动界面.bat)，或在项目目录执行：

```powershell
.\.venv\Scripts\python.exe .\web_app.py
```

随后在浏览器打开 <http://127.0.0.1:8765>。服务仅监听本机地址；按终端中的 `Ctrl+C` 可以停止服务。

界面不依赖第三方 Python 包，支持 Python 3.8 及以上。

## 界面流程

1. 粘贴 JSON 或上传 `.json` 文件，点击“分析字段”。
2. 勾选必填字段，设置正常/异常预期状态码及是否校验容器字段。
3. 生成后默认查看排在首位的“全部用例”；可打开任一 `request_body` 并一键复制，再下载四份 CSV 或全部 ZIP。

下载的 CSV 使用 UTF-8 with BOM 编码；`request_body` 列为可直接作为 Postman/Apifox 请求体的数据源。

## 命令行模式

原有命令行工具仍可单独使用：

```powershell
python .\json_case_generator.py init --input .\input.json --outdir .\output
python .\json_case_generator.py generate --input .\input.json --outdir .\output --required .\output\required_fields.json --config .\output\config.json
```
