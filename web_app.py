#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 Web 界面：JSON 接口测试数据生成器。

仅监听 127.0.0.1，所有输入和生成结果仅保存在当前进程内存中。
接口执行、鉴权和报告由 Postman / Apifox 等工具负责。
"""

import copy
import csv
import io
import json
import mimetypes
import re
import sys
import uuid
import zipfile
from collections import OrderedDict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import unquote, urlparse

import json_case_generator as generator


HOST = "127.0.0.1"
PORT = 8765
MAX_JSON_BYTES = 1024 * 1024
MAX_FIELDS = 5000
MAX_JOBS = 10
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATE_DIR = BASE_DIR / "templates"

# 仅保存用户主动下载所需的短期结果；有新结果时自动淘汰最早任务。
DOWNLOAD_JOBS: "OrderedDict[str, Dict[str, bytes]]" = OrderedDict()


class RequestError(Exception):
    """用于向界面返回可读的 4xx 错误。"""

    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        self.message = message
        self.status = status
        super().__init__(message)


def json_error_detail(error: json.JSONDecodeError) -> str:
    return f"JSON 格式错误：第 {error.lineno} 行、第 {error.colno} 列，{error.msg}。"


def parse_json_text(json_text: Any) -> Any:
    if not isinstance(json_text, str) or not json_text.strip():
        raise RequestError("请粘贴或上传 JSON 请求体样例。")
    if len(json_text.encode("utf-8")) > MAX_JSON_BYTES:
        raise RequestError("JSON 内容超过 1 MB 限制，请拆分或精简样例。")
    try:
        return json.loads(json_text)
    except json.JSONDecodeError as error:
        raise RequestError(json_error_detail(error)) from error


def get_fields(data: Any) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    fields, template = generator.extract_fields(data)
    fields, _ = generator.normalize_var_names_for_duplicates(fields, template)
    if len(fields) > MAX_FIELDS:
        raise RequestError(f"发现 {len(fields)} 个字段，超过 {MAX_FIELDS} 个字段限制，请拆分样例。")
    return fields, generator.build_field_meta(fields)


def display_sample_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return generator.primitive_to_csv_value(value)


def field_response(fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "seq": index,
            "raw_name": item["raw_name"],
            "var_name": item["var_name"],
            "path": item["path"],
            "level": item["level"],
            "type": item["type"],
            "parent": item["parent"],
            "is_container": item["is_container"],
            "sample_value": display_sample_value(item["sample_value"]),
        }
        for index, item in enumerate(fields, start=1)
    ]


def validate_generate_payload(payload: Dict[str, Any], field_meta: Dict[str, Dict[str, Any]]) -> Tuple[List[str], Dict[str, Any]]:
    required_fields = payload.get("required_fields", [])
    if not isinstance(required_fields, list) or not all(isinstance(item, str) for item in required_fields):
        raise RequestError("必填字段必须是字段变量名组成的列表。")
    required_fields = list(dict.fromkeys(required_fields))

    invalid_fields = [item for item in required_fields if item not in field_meta]
    if invalid_fields:
        raise RequestError(f"存在无效的必填字段：{', '.join(invalid_fields)}。请重新分析 JSON 后再生成。")

    config = payload.get("config", {})
    if not isinstance(config, dict):
        raise RequestError("生成配置格式不正确。")
    validate_containers = config.get("validate_container_fields", True)
    if not isinstance(validate_containers, bool):
        raise RequestError("“校验容器字段”必须为开关值。")

    validated_config = {"validate_container_fields": validate_containers}
    for key, default in (("default_normal_code", 200), ("default_error_code", 400)):
        value = config.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 100 <= value <= 599:
            label = "正常用例预期状态码" if key == "default_normal_code" else "异常用例预期状态码"
            raise RequestError(f"{label}必须是 100 至 599 的整数。")
        validated_config[key] = value
    return required_fields, validated_config


def build_case_groups(data: Any, fields: List[Dict[str, Any]], field_meta: Dict[str, Dict[str, Any]], required_fields: List[str], config: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    children_map = generator.build_children_map(field_meta)
    base_row = generator.build_baseline_row(fields, config["default_normal_code"])
    missing_rows = [copy.deepcopy(base_row)]
    empty_rows = [copy.deepcopy(base_row)]
    type_rows = [copy.deepcopy(base_row)]

    for item in fields:
        var_name = item["var_name"]
        meta = field_meta[var_name]
        if not generator.should_generate_for_field(var_name, meta, required_fields, config["validate_container_fields"]):
            continue
        missing_rows.append(generator.build_missing_case(base_row, var_name, field_meta, children_map, config["default_error_code"]))
        empty_rows.append(generator.build_empty_case(base_row, var_name, field_meta, children_map, config["default_error_code"]))
        type_rows.append(generator.build_type_error_case(base_row, var_name, field_meta, config["default_error_code"]))

    for rows in (missing_rows, empty_rows, type_rows):
        generator.add_request_body_to_rows(rows, data, field_meta)

    all_rows: List[Dict[str, Any]] = []
    seen = set()
    for rows in (missing_rows, empty_rows, type_rows):
        for row in rows:
            key = (row["req_name"], row["case_type"])
            if key not in seen:
                all_rows.append(row)
                seen.add(key)
    return {"missing": missing_rows, "empty": empty_rows, "type": type_rows, "all": all_rows}


def csv_bytes(header: List[str], rows: List[Dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=header)
    writer.writeheader()
    for row in rows:
        safe_row = {}
        for column in header:
            value = row.get(column, "")
            safe_row[column] = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
        writer.writerow(safe_row)
    return output.getvalue().encode("utf-8-sig")


def create_download_job(header: List[str], groups: Dict[str, List[Dict[str, Any]]], required_fields: List[str], config: Dict[str, Any]) -> str:
    names = {
        "missing": "case_required_missing.csv",
        "empty": "case_required_empty.csv",
        "type": "case_required_type.csv",
        "all": "case_all.csv",
    }
    files = {names[key]: csv_bytes(header, rows) for key, rows in groups.items()}
    config_bytes = json.dumps(
        {"required_fields": required_fields, "config": config}, ensure_ascii=False, indent=2
    ).encode("utf-8")
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, content in files.items():
            archive.writestr(filename, content)
        archive.writestr("generation_config.json", config_bytes)
    files["all_cases.zip"] = zip_buffer.getvalue()

    job_id = uuid.uuid4().hex
    DOWNLOAD_JOBS[job_id] = files
    while len(DOWNLOAD_JOBS) > MAX_JOBS:
        DOWNLOAD_JOBS.popitem(last=False)
    return job_id


def make_preview(rows: List[Dict[str, Any]], header: List[str], limit: int = 100) -> List[Dict[str, Any]]:
    return [{column: row.get(column, "") for column in header} for row in rows[:limit]]


class AppHandler(BaseHTTPRequestHandler):
    server_version = "JsonCaseGenerator/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        # 不记录用户输入的请求体，终端只保留访问状态。
        sys.stdout.write("[web] " + format % args + "\n")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_file(TEMPLATE_DIR / "index.html", "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/static/"):
            relative_path = Path(unquote(parsed.path[len("/static/"):]))
            if relative_path.is_absolute() or ".." in relative_path.parts:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            file_path = STATIC_DIR / relative_path
            mime_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
            self.send_file(file_path, f"{mime_type}; charset=utf-8" if mime_type.startswith("text/") else mime_type)
            return
        match = re.fullmatch(r"/api/download/([0-9a-f]{32})/(case_required_missing\.csv|case_required_empty\.csv|case_required_type\.csv|case_all\.csv|all_cases\.zip)", parsed.path)
        if match:
            self.handle_download(match.group(1), match.group(2))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if self.path not in {"/api/analyze", "/api/generate"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > MAX_JSON_BYTES + 100_000:
                raise RequestError("请求内容为空或超过允许大小。")
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise RequestError("请求数据格式不正确。")
            if self.path == "/api/analyze":
                self.handle_analyze(payload)
            else:
                self.handle_generate(payload)
        except RequestError as error:
            self.send_json({"ok": False, "error": error.message}, error.status)
        except json.JSONDecodeError:
            self.send_json({"ok": False, "error": "请求数据不是有效 JSON。"}, HTTPStatus.BAD_REQUEST)
        except Exception:
            self.send_json({"ok": False, "error": "生成时发生未预期错误，请检查输入后重试。"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_analyze(self, payload: Dict[str, Any]) -> None:
        data = parse_json_text(payload.get("json_text"))
        fields, _ = get_fields(data)
        self.send_json(
            {
                "ok": True,
                "top_level_type": generator.detect_type(data),
                "field_count": len(fields),
                "fields": field_response(fields),
            }
        )

    def handle_generate(self, payload: Dict[str, Any]) -> None:
        data = parse_json_text(payload.get("json_text"))
        fields, field_meta = get_fields(data)
        required_fields, config = validate_generate_payload(payload, field_meta)
        groups = build_case_groups(data, fields, field_meta, required_fields, config)
        header = generator.build_csv_header(fields)
        job_id = create_download_job(header, groups, required_fields, config)
        self.send_json(
            {
                "ok": True,
                "job_id": job_id,
                "header": header,
                "summary": {
                    "field_count": len(fields),
                    "required_count": len(required_fields),
                    "missing_count": len(groups["missing"]),
                    "empty_count": len(groups["empty"]),
                    "type_count": len(groups["type"]),
                    "all_count": len(groups["all"]),
                },
                "previews": {key: make_preview(rows, header) for key, rows in groups.items()},
            }
        )

    def handle_download(self, job_id: str, filename: str) -> None:
        files = DOWNLOAD_JOBS.get(job_id)
        if not files or filename not in files:
            self.send_json({"ok": False, "error": "下载结果已过期，请重新生成。"}, HTTPStatus.NOT_FOUND)
            return
        content = files[filename]
        content_type = "application/zip" if filename.endswith(".zip") else "text/csv; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Content-Disposition", f"attachment; filename={filename}")
        self.end_headers()
        self.wfile.write(content)

    def send_file(self, file_path: Path, content_type: str) -> None:
        if not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, body: Dict[str, Any], status: int = HTTPStatus.OK) -> None:
        content = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    print(f"JSON 接口测试数据生成器已启动： http://{HOST}:{PORT}")
    print("按 Ctrl+C 停止服务。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
