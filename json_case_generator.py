#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
json_case_generator.py

功能：
1. 解析输入 JSON，提取所有字段（支持多层对象、数组中对象）
2. 自动处理重名字段，如 name1, name2
3. 生成变量化 JSON 模板
4. 生成字段元数据 field_meta.json
5. 输出字段清单 fields_report.csv
6. 输出 required_fields.json.sample / config.json.sample / guide / usage
7. 根据 required_fields.json + config.json 生成 CSV 测试用例：
   - 必填字段缺失
   - 必填字段为空
   - 字段类型错误
8. 每个 CSV 第一行永远为正常基准用例
9. 额外输出 request_body 列，便于 Postman / Apifox 直接执行 run 数据集

说明：
- 仅使用 Python 标准库
- 推荐 Python 3.9+
"""

import argparse
import copy
import csv
import json
import os
import sys
from pathlib import Path
from collections import defaultdict
from typing import Any, Dict, List, Tuple


# =========================
# 常量
# =========================
DELETE_ME = "DELETE_ME"
EMPTY_OBJ = "EMPTY_OBJ"
EMPTY_ARR = "EMPTY_ARR"
NA_VALUE = "N/A"
TYPE_ERROR = "TYPE_ERROR"

DEFAULT_NORMAL_CODE = 200
DEFAULT_ERROR_CODE = 400


# =========================
# 工具函数
# =========================
def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def load_json_file(file_path: Path) -> Any:
    with file_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json_file(file_path: Path, data: Any) -> None:
    with file_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_text_file(file_path: Path, content: str) -> None:
    with file_path.open("w", encoding="utf-8") as f:
        f.write(content)


def write_csv(file_path: Path, header: List[str], rows: List[Dict[str, Any]]) -> None:
    with file_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        for row in rows:
            safe_row = {}
            for col in header:
                val = row.get(col, "")
                if isinstance(val, (dict, list)):
                    safe_row[col] = json.dumps(val, ensure_ascii=False)
                else:
                    safe_row[col] = val
            writer.writerow(safe_row)


def detect_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        if not value:
            return "array"
        first = value[0]
        if isinstance(first, dict):
            return "array<object>"
        if isinstance(first, str):
            return "array<string>"
        if isinstance(first, bool):
            return "array<boolean>"
        if isinstance(first, int) and not isinstance(first, bool):
            return "array<integer>"
        if isinstance(first, float):
            return "array<number>"
        return "array<mixed>"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return "unknown"


def is_container_type(type_name: str) -> bool:
    return type_name in {"object", "array", "array<object>", "array<string>", "array<boolean>", "array<integer>", "array<number>", "array<mixed>"}


def primitive_to_csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        if len(value) == 0:
            return "[]"
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def csv_value_to_python(value: str, type_name: str) -> Any:
    """
    将 CSV 中的普通值转为 Python 类型。
    特殊占位符 DELETE_ME / EMPTY_OBJ / EMPTY_ARR / N/A / TYPE_ERROR 不在这里处理。
    """
    if type_name == "string":
        return value
    if type_name == "integer":
        if value == "":
            return ""
        return int(value)
    if type_name == "number":
        if value == "":
            return ""
        return float(value)
    if type_name == "boolean":
        if value == "":
            return ""
        val = str(value).strip().lower()
        if val == "true":
            return True
        if val == "false":
            return False
        return value
    if type_name.startswith("array") or type_name == "object":
        # 通常容器字段不依赖这一层直接解析，最终由特殊逻辑生成
        return value
    return value


def get_type_error_value(type_name: str) -> Any:
    if type_name == "string":
        return 12345
    if type_name in {"integer", "number"}:
        return "abc"
    if type_name == "boolean":
        return "abc"
    if type_name == "object":
        return "abc"
    if type_name.startswith("array"):
        return "abc"
    return "abc"


def get_empty_value_for_type(type_name: str) -> Any:
    if type_name == "object":
        return EMPTY_OBJ
    if type_name.startswith("array"):
        return EMPTY_ARR
    return ""


def parse_path_tokens(path_str: str) -> List[str]:
    """
    将路径字符串拆成 token 列表
    例如：
      policyRules[].criteriaConfig.minVulLevel
    -> ["policyRules[]", "criteriaConfig", "minVulLevel"]
    """
    if not path_str:
        return []
    return path_str.split(".")


def token_is_array(token: str) -> bool:
    return token.endswith("[]")


def token_base_name(token: str) -> str:
    return token[:-2] if token.endswith("[]") else token


# =========================
# 字段提取
# =========================
def extract_fields(data: Any) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    返回:
    1. fields: 有序字段列表
    2. template_json: 变量化 JSON 模板
    """
    name_counter = defaultdict(int)
    fields: List[Dict[str, Any]] = []

    def alloc_var_name(raw_name: str) -> str:
        name_counter[raw_name] += 1
        count = name_counter[raw_name]
        if count == 1:
            # 第一次出现:
            # 如果未来可能重名，先暂时返回原名，最终若再次出现则需要把第一次变成 name1
            # 为了稳定，我们采用更稳的策略：
            # 如果字段名后续可能重复无法预知，因此直接：
            #   第一次若全局第一次出现 -> 原名
            #   第二次起 -> name2
            # 但用户要求类似 name1, name2
            # 所以采用：
            #   若首次出现也编号：name1
            #   仅当字段从未重复时可保持原名？前面已确认示例中顶层 name -> name1。
            # 因此：
            if raw_name in {"name"}:
                return f"{raw_name}{count}"
            return raw_name
        else:
            # 若第一次是原名，则第二次变 raw2；但 name 要求 name1/name2
            if raw_name in {"name"}:
                return f"{raw_name}{count}"
            return f"{raw_name}{count}"

    def collect(
        value: Any,
        path_str: str,
        level: int,
        parent_var: str,
        raw_name: str = "",
        in_array_item: bool = False
    ) -> Any:
        """
        返回变量化结构
        """
        type_name = detect_type(value)

        # 顶层 ROOT 不作为字段
        if raw_name:
            var_name = alloc_var_name(raw_name)
            is_container = is_container_type(type_name)
            field_item = {
                "raw_name": raw_name,
                "var_name": var_name,
                "path": path_str,
                "level": level,
                "type": type_name,
                "parent": parent_var if parent_var else "ROOT",
                "is_container": is_container,
                "sample_value": value
            }
            fields.append(field_item)
            current_parent = var_name
        else:
            current_parent = "ROOT"

        if isinstance(value, dict):
            result = {}
            for k, v in value.items():
                child_path = f"{path_str}.{k}" if path_str else k
                result[k] = collect(
                    v,
                    child_path,
                    level + 1,
                    current_parent if raw_name else "ROOT",
                    raw_name=k,
                    in_array_item=False
                )
            return result

        if isinstance(value, list):
            if len(value) == 0:
                return []
            first = value[0]
            if isinstance(first, dict):
                item_result = {}
                for k, v in first.items():
                    child_path = f"{path_str}[].{k}" if path_str else f"{k}"
                    item_result[k] = collect(
                        v,
                        child_path.replace("[].", "[]."),
                        level + 1,
                        current_parent if raw_name else "ROOT",
                        raw_name=k,
                        in_array_item=True
                    )
                return [item_result]
            else:
                # 非对象数组直接整体变量化
                if raw_name:
                    matched_var = None
                    for f in reversed(fields):
                        if f["path"] == path_str:
                            matched_var = f["var_name"]
                            break
                    return f"{{{{{matched_var}}}}}" if matched_var else value
                return value

        # 叶子字段变量化
        if raw_name:
            matched_var = None
            for f in reversed(fields):
                if f["path"] == path_str:
                    matched_var = f["var_name"]
                    break
            return f"{{{{{matched_var}}}}}" if matched_var else value

        return value

    template_json = collect(data, "", 0, "", "", False)
    return fields, template_json


def normalize_var_names_for_duplicates(fields: List[Dict[str, Any]], template_json: Any) -> Tuple[List[Dict[str, Any]], Any]:
    """
    为了满足类似 name1/name2 的要求，这里对重复 raw_name 进行统一调整：
    - 若某个 raw_name 只出现一次，保留原 var_name（比如 type, priority）
    - 若某个 raw_name 出现多次，统一改成 raw1/raw2/raw3...
    """
    counts = defaultdict(int)
    raw_to_items = defaultdict(list)
    for item in fields:
        raw_to_items[item["raw_name"]].append(item)

    old_to_new = {}
    for raw_name, items in raw_to_items.items():
        if len(items) == 1:
            old_to_new[items[0]["var_name"]] = raw_name
            items[0]["var_name"] = raw_name
        else:
            for idx, item in enumerate(items, start=1):
                new_name = f"{raw_name}{idx}"
                old_to_new[item["var_name"]] = new_name
                item["var_name"] = new_name

    # 修正 parent
    for item in fields:
        parent = item["parent"]
        if parent in old_to_new:
            item["parent"] = old_to_new[parent]

    # 修正 template_json 中的变量
    template_str = json.dumps(template_json, ensure_ascii=False)
    for old, new in sorted(old_to_new.items(), key=lambda x: len(x[0]), reverse=True):
        template_str = template_str.replace(f"{{{{{old}}}}}", f"{{{{{new}}}}}")
    template_json = json.loads(template_str)

    return fields, template_json


def build_field_meta(fields: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    meta = {}
    for item in fields:
        meta[item["var_name"]] = {
            "raw_name": item["raw_name"],
            "path": item["path"],
            "level": item["level"],
            "type": item["type"],
            "parent": item["parent"],
            "is_container": item["is_container"],
            "sample_value": item["sample_value"]
        }
    return meta


def build_fields_report_rows(fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for idx, item in enumerate(fields, start=1):
        rows.append({
            "seq": idx,
            "raw_name": item["raw_name"],
            "var_name": item["var_name"],
            "path": item["path"],
            "level": item["level"],
            "type": item["type"],
            "parent": item["parent"],
            "is_container": "true" if item["is_container"] else "false",
            "sample_value": primitive_to_csv_value(item["sample_value"])
        })
    return rows


# =========================
# 初始化输出
# =========================
def build_required_fields_sample(fields: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "required_fields": [],
        "_comment": "请填写必填字段变量名，变量名请参考 fields_report.csv 中的 var_name 列。若字段重名，请使用 name1/name2 这类变量名。"
    }


def build_config_sample() -> Dict[str, Any]:
    return {
        "validate_container_fields": True,
        "default_normal_code": DEFAULT_NORMAL_CODE,
        "default_error_code": DEFAULT_ERROR_CODE,
        "_comment_1": "validate_container_fields=true 表示 policyRules / criteriaConfig 这类容器字段也参与缺失/为空/类型错误校验。",
        "_comment_2": "default_normal_code 为正常基准用例的预期状态码。",
        "_comment_3": "default_error_code 为异常用例的统一预期状态码。",
        "_comment_4": "执行 Postman / Apifox run 时，建议优先使用 CSV 中的 request_body 列作为最终请求体。"
    }


def build_required_fields_guide(fields: List[Dict[str, Any]]) -> str:
    lines = []
    lines.append("# 必填字段填写说明")
    lines.append("")
    lines.append("请在 required_fields.json 中填写 required_fields 数组。")
    lines.append("填写内容必须是字段变量名 var_name，不是原始字段名 raw_name，也不是路径 path。")
    lines.append("")
    lines.append("## 重要说明")
    lines.append("1. 如果字段名没有重复，通常变量名就是字段名本身，例如：type、priority。")
    lines.append("2. 如果字段名重复出现，变量名会自动编号，例如：name1、name2。")
    lines.append("3. 容器字段（如 object / array<object>）也可以填写为必填，例如：policyRules、criteriaConfig。")
    lines.append("4. 若 validate_container_fields=true，则容器字段也会生成 缺失/为空/类型错误 用例。")
    lines.append("")
    lines.append("## 当前可填写字段")
    lines.append("")
    for item in fields:
        lines.append(f"- {item['var_name']}    (raw_name={item['raw_name']}, path={item['path']}, type={item['type']})")
    lines.append("")
    lines.append("## required_fields.json 示例")
    lines.append("")
    lines.append('''{
  "required_fields": [
    "type",
    "name1",
    "policyRules",
    "name2",
    "criteriaConfig",
    "criteriaType"
  ]
}''')
    lines.append("")
    return "\n".join(lines)


def build_usage_md() -> str:
    lines = [
        "JSON 字段校验测试用例生成工具使用说明",
        "",
        "一、初始化分析 JSON",
        "",
        "执行命令：",
        "python json_case_generator.py init --input input.json --outdir output",
        "",
        "会生成以下文件：",
        "1. fields_report.csv",
        "2. json_template.json",
        "3. field_meta.json",
        "4. required_fields.json.sample",
        "5. config.json.sample",
        "6. required_fields_guide.txt",
        "7. USAGE.txt",
        "",
        "二、填写必填字段",
        "",
        "请将以下示例文件复制并重命名：",
        "1. required_fields.json.sample 复制为 required_fields.json",
        "2. config.json.sample 复制为 config.json",
        "",
        "然后根据 fields_report.csv 和 required_fields_guide.txt 填写内容。",
        "",
        "三、生成 CSV 测试用例",
        "",
        "执行命令：",
        "python json_case_generator.py generate --input input.json --outdir output --required output/required_fields.json --config output/config.json",
        "",
        "会生成以下文件：",
        "1. case_required_missing.csv",
        "2. case_required_empty.csv",
        "3. case_required_type.csv",
        "4. case_all.csv",
        "",
        "四、CSV 文件说明",
        "",
        "生成的 CSV 中包含以下重要列：",
        "1. req_name",
        "2. 各字段变量列",
        "3. expected_code",
        "4. test_purpose",
        "5. case_type",
        "6. request_body",
        "",
        "建议：",
        "在 Postman 或 Apifox 执行 run 时，优先使用 request_body 列作为最终请求体。",
        "",
        "原因：",
        "1. DELETE_ME 表示删除字段",
        "2. EMPTY_OBJ 表示 {}",
        "3. EMPTY_ARR 表示 []",
        "4. 容器字段缺失、为空、类型错误等场景，无法只靠简单变量替换表达",
        "",
        "五、case_type 含义",
        "",
        "1. positive：正常基准用例",
        "2. required_missing：必填字段缺失",
        "3. required_empty：必填字段为空",
        "4. type_error：字段类型错误",
        "",
        "六、特殊占位符说明",
        "",
        "1. DELETE_ME：删除该字段",
        "2. EMPTY_OBJ：该字段内容置为 {}",
        "3. EMPTY_ARR：该字段内容置为 []",
        "4. N/A：由于父字段缺失或置空，该子字段不适用",
        "5. TYPE_ERROR：用于标识字段类型错误场景，实际 request_body 会写入错误类型值",
        "",
        "七、适用场景",
        "",
        "该工具生成的 CSV 测试数据主要用于：",
        "1. Postman Collection Runner",
        "2. Apifox Run 数据集",
        "",
    ]
    return "\n".join(lines)
# =========================
# 构造基线数据
# =========================
def do_init(input_path: Path, outdir: Path) -> None:
    ensure_dir(outdir)
    data = load_json_file(input_path)

    fields, template_json = extract_fields(data)
    fields, template_json = normalize_var_names_for_duplicates(fields, template_json)
    field_meta = build_field_meta(fields)

    report_header = [
        "seq", "raw_name", "var_name", "path", "level",
        "type", "parent", "is_container", "sample_value"
    ]
    report_rows = build_fields_report_rows(fields)
    write_csv(outdir / "fields_report.csv", report_header, report_rows)

    save_json_file(outdir / "json_template.json", template_json)
    save_json_file(outdir / "field_meta.json", field_meta)
    save_json_file(outdir / "required_fields.json.sample", build_required_fields_sample(fields))
    save_json_file(outdir / "config.json.sample", build_config_sample())
    save_text_file(outdir / "required_fields_guide.txt", build_required_fields_guide(fields))
    save_text_file(outdir / "USAGE.txt", build_usage_md())

    print(f"[OK] 初始化完成，输出目录：{outdir}")
    print(f" - {outdir / 'fields_report.csv'}")
    print(f" - {outdir / 'json_template.json'}")
    print(f" - {outdir / 'field_meta.json'}")
    print(f" - {outdir / 'required_fields.json.sample'}")
    print(f" - {outdir / 'config.json.sample'}")
    print(f" - {outdir / 'required_fields_guide.txt'}")
    print(f" - {outdir / 'USAGE.txt'}")


def build_baseline_row(fields: List[Dict[str, Any]], normal_code: int) -> Dict[str, Any]:
    row = {
        "req_name": "正常基准用例",
        "expected_code": str(normal_code),
        "test_purpose": "正常请求",
        "case_type": "positive"
    }
    for item in fields:
        row[item["var_name"]] = primitive_to_csv_value(item["sample_value"])
    return row


def build_children_map(field_meta: Dict[str, Dict[str, Any]]) -> Dict[str, List[str]]:
    children_map = defaultdict(list)
    for var_name, meta in field_meta.items():
        parent = meta["parent"]
        if parent and parent != "ROOT":
            children_map[parent].append(var_name)
    return children_map


def get_descendants(var_name: str, children_map: Dict[str, List[str]]) -> List[str]:
    result = []

    def dfs(node: str) -> None:
        for child in children_map.get(node, []):
            result.append(child)
            dfs(child)

    dfs(var_name)
    return result


# =========================
# request_body 构建逻辑
# =========================
def build_runtime_template(data: Any, field_meta: Dict[str, Dict[str, Any]]) -> Any:
    """
    返回一个“可被 var_name 驱动”的结构骨架。
    叶子字段保留为 {"__VAR__": var_name}
    容器字段保留原结构，后续再根据 DELETE_ME / EMPTY_OBJ / EMPTY_ARR / TYPE_ERROR 做处理
    """
    path_to_var = {}
    for var_name, meta in field_meta.items():
        path_to_var[meta["path"]] = var_name

    def walk(value: Any, path_str: str) -> Any:
        if isinstance(value, dict):
            result = {}
            for k, v in value.items():
                child_path = f"{path_str}.{k}" if path_str else k
                result[k] = walk(v, child_path)
            return result

        if isinstance(value, list):
            if len(value) == 0:
                return []
            first = value[0]
            if isinstance(first, dict):
                result_item = {}
                for k, v in first.items():
                    child_path = f"{path_str}[].{k}" if path_str else k
                    result_item[k] = walk(v, child_path)
                return [result_item]
            else:
                var_name = path_to_var.get(path_str)
                if var_name:
                    return {"__VAR__": var_name}
                return value

        var_name = path_to_var.get(path_str)
        if var_name:
            return {"__VAR__": var_name}
        return value

    return walk(data, "")


def set_value_by_path(target: Any, path_tokens: List[str], new_value: Any) -> None:
    cur = target
    for idx, token in enumerate(path_tokens):
        is_last = idx == len(path_tokens) - 1
        is_arr = token_is_array(token)
        key = token_base_name(token)

        if is_last:
            cur[key] = new_value
            return

        if is_arr:
            if key not in cur or not isinstance(cur[key], list) or not cur[key]:
                cur[key] = [{}]
            cur = cur[key][0]
        else:
            if key not in cur or not isinstance(cur[key], dict):
                cur[key] = {}
            cur = cur[key]


def delete_by_path(target: Any, path_tokens: List[str]) -> None:
    cur = target
    for idx, token in enumerate(path_tokens):
        is_last = idx == len(path_tokens) - 1
        is_arr = token_is_array(token)
        key = token_base_name(token)

        if is_last:
            if isinstance(cur, dict) and key in cur:
                del cur[key]
            return

        if is_arr:
            if key not in cur or not isinstance(cur[key], list) or not cur[key]:
                return
            cur = cur[key][0]
        else:
            if key not in cur or not isinstance(cur[key], dict):
                return
            cur = cur[key]


def resolve_runtime_vars(runtime_template: Any, row: Dict[str, Any], field_meta: Dict[str, Dict[str, Any]]) -> Any:
    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "__VAR__" in node:
                var_name = node["__VAR__"]
                raw_val = row.get(var_name, "")
                meta = field_meta[var_name]
                type_name = meta["type"]

                if raw_val in {DELETE_ME, EMPTY_OBJ, EMPTY_ARR, NA_VALUE, TYPE_ERROR}:
                    return raw_val

                return csv_value_to_python(str(raw_val), type_name)

            new_obj = {}
            for k, v in node.items():
                new_obj[k] = walk(v)
            return new_obj

        if isinstance(node, list):
            return [walk(node[0])] if node else []

        return node

    return walk(runtime_template)


def apply_container_controls(body: Any, row: Dict[str, Any], field_meta: Dict[str, Dict[str, Any]]) -> Any:
    result = copy.deepcopy(body)

    ordered_items = sorted(field_meta.items(), key=lambda x: x[1]["path"].count("."))

    for var_name, meta in ordered_items:
        path = meta["path"]
        type_name = meta["type"]
        raw_val = row.get(var_name, "")

        if raw_val == NA_VALUE:
            continue

        path_tokens = parse_path_tokens(path)

        if raw_val == DELETE_ME:
            delete_by_path(result, path_tokens)
        elif raw_val == EMPTY_OBJ:
            set_value_by_path(result, path_tokens, {})
        elif raw_val == EMPTY_ARR:
            set_value_by_path(result, path_tokens, [])
        elif raw_val == TYPE_ERROR:
            set_value_by_path(result, path_tokens, get_type_error_value(type_name))

    def cleanup(node: Any) -> Any:
        special_values = {DELETE_ME, EMPTY_OBJ, EMPTY_ARR, NA_VALUE, TYPE_ERROR}

        if isinstance(node, dict):
            cleaned = {}
            for k, v in node.items():
                # 只有基础类型才能直接做 special value 判断
                if isinstance(v, (str, int, float, bool)) or v is None:
                    if v in special_values:
                        continue

                cleaned[k] = cleanup(v)
            return cleaned

        if isinstance(node, list):
            cleaned_list = []
            for item in node:
                # 只有基础类型才能直接做 special value 判断
                if isinstance(item, (str, int, float, bool)) or item is None:
                    if item in special_values:
                        continue
                cleaned_list.append(cleanup(item))
            return cleaned_list

        return node

    return cleanup(result)


def build_request_body(data: Any, row: Dict[str, Any], field_meta: Dict[str, Dict[str, Any]]) -> str:
    runtime_template = build_runtime_template(data, field_meta)
    body = resolve_runtime_vars(runtime_template, row, field_meta)
    body = apply_container_controls(body, row, field_meta)
    return json.dumps(body, ensure_ascii=False)


# =========================
# 用例生成
# =========================
def should_generate_for_field(
    var_name: str,
    meta: Dict[str, Any],
    required_fields: List[str],
    validate_container_fields: bool
) -> bool:
    if var_name not in required_fields:
        return False
    if meta["is_container"] and not validate_container_fields:
        return False
    return True


def build_missing_case(
    base_row: Dict[str, Any],
    target_var: str,
    field_meta: Dict[str, Dict[str, Any]],
    children_map: Dict[str, List[str]],
    error_code: int
) -> Dict[str, Any]:
    row = copy.deepcopy(base_row)
    row["req_name"] = f"字段缺失_{target_var}"
    row["expected_code"] = str(error_code)
    row["test_purpose"] = f"{target_var}字段缺失"
    row["case_type"] = "required_missing"

    row[target_var] = DELETE_ME

    descendants = get_descendants(target_var, children_map)
    for child in descendants:
        row[child] = DELETE_ME

    return row


def build_empty_case(
    base_row: Dict[str, Any],
    target_var: str,
    field_meta: Dict[str, Dict[str, Any]],
    children_map: Dict[str, List[str]],
    error_code: int
) -> Dict[str, Any]:
    row = copy.deepcopy(base_row)
    meta = field_meta[target_var]
    row["req_name"] = f"内容空_{target_var}"
    row["expected_code"] = str(error_code)
    row["test_purpose"] = f"{target_var}内容为空"
    row["case_type"] = "required_empty"

    row[target_var] = get_empty_value_for_type(meta["type"])

    if meta["is_container"]:
        descendants = get_descendants(target_var, children_map)
        for child in descendants:
            row[child] = NA_VALUE

    return row


def build_type_error_case(
    base_row: Dict[str, Any],
    target_var: str,
    field_meta: Dict[str, Dict[str, Any]],
    error_code: int
) -> Dict[str, Any]:
    row = copy.deepcopy(base_row)
    row["req_name"] = f"类型错误_{target_var}"
    row["expected_code"] = str(error_code)
    row["test_purpose"] = f"{target_var}字段类型错误"
    row["case_type"] = "type_error"
    row[target_var] = TYPE_ERROR
    return row


def add_request_body_to_rows(rows: List[Dict[str, Any]], data: Any, field_meta: Dict[str, Dict[str, Any]]) -> None:
    for row in rows:
        row["request_body"] = build_request_body(data, row, field_meta)


def build_csv_header(fields: List[Dict[str, Any]]) -> List[str]:
    cols = ["req_name"]
    cols.extend([item["var_name"] for item in fields])
    cols.extend(["expected_code", "test_purpose", "case_type", "request_body"])
    return cols


def do_generate(input_path: Path, outdir: Path, required_path: Path, config_path: Path) -> None:
    ensure_dir(outdir)

    data = load_json_file(input_path)
    required_data = load_json_file(required_path)
    config_data = load_json_file(config_path)

    required_fields = required_data.get("required_fields", [])
    validate_container_fields = bool(config_data.get("validate_container_fields", True))
    normal_code = int(config_data.get("default_normal_code", DEFAULT_NORMAL_CODE))
    error_code = int(config_data.get("default_error_code", DEFAULT_ERROR_CODE))

    fields, template_json = extract_fields(data)
    fields, template_json = normalize_var_names_for_duplicates(fields, template_json)
    field_meta = build_field_meta(fields)
    children_map = build_children_map(field_meta)

    all_vars = set(item["var_name"] for item in fields)
    invalid_required = [v for v in required_fields if v not in all_vars]
    if invalid_required:
        print("[ERROR] required_fields.json 中存在无效变量名：")
        for name in invalid_required:
            print(f" - {name}")
        print("请参考 fields_report.csv / required_fields_guide.txt 修正后再执行。")
        sys.exit(1)

    base_row = build_baseline_row(fields, normal_code)

    missing_rows = [copy.deepcopy(base_row)]
    empty_rows = [copy.deepcopy(base_row)]
    type_rows = [copy.deepcopy(base_row)]

    for item in fields:
        var_name = item["var_name"]
        meta = field_meta[var_name]

        if not should_generate_for_field(var_name, meta, required_fields, validate_container_fields):
            continue

        missing_rows.append(build_missing_case(base_row, var_name, field_meta, children_map, error_code))
        empty_rows.append(build_empty_case(base_row, var_name, field_meta, children_map, error_code))
        type_rows.append(build_type_error_case(base_row, var_name, field_meta, error_code))

    add_request_body_to_rows(missing_rows, data, field_meta)
    add_request_body_to_rows(empty_rows, data, field_meta)
    add_request_body_to_rows(type_rows, data, field_meta)

    all_rows = []
    seen = set()
    for group in (missing_rows, empty_rows, type_rows):
        for row in group:
            key = (row["req_name"], row["case_type"])
            if key not in seen:
                all_rows.append(row)
                seen.add(key)

    header = build_csv_header(fields)

    write_csv(outdir / "case_required_missing.csv", header, missing_rows)
    write_csv(outdir / "case_required_empty.csv", header, empty_rows)
    write_csv(outdir / "case_required_type.csv", header, type_rows)
    write_csv(outdir / "case_all.csv", header, all_rows)

    print(f"[OK] 用例生成完成，输出目录：{outdir}")
    print(f" - {outdir / 'case_required_missing.csv'}")
    print(f" - {outdir / 'case_required_empty.csv'}")
    print(f" - {outdir / 'case_required_type.csv'}")
    print(f" - {outdir / 'case_all.csv'}")


# =========================
# main
# =========================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="根据输入 JSON 生成字段报告、变量化模板，以及字段缺失/为空/类型错误测试用例 CSV。"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="初始化分析 JSON，生成字段清单、模板、示例配置和说明文件")
    init_parser.add_argument("--input", required=True, help="输入 JSON 文件路径")
    init_parser.add_argument("--outdir", required=True, help="输出目录")

    gen_parser = subparsers.add_parser("generate", help="根据 required_fields.json 和 config.json 生成 CSV 测试用例")
    gen_parser.add_argument("--input", required=True, help="输入 JSON 文件路径")
    gen_parser.add_argument("--outdir", required=True, help="输出目录")
    gen_parser.add_argument("--required", required=True, help="required_fields.json 文件路径")
    gen_parser.add_argument("--config", required=True, help="config.json 文件路径")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    input_path = Path(args.input)
    outdir = Path(args.outdir)

    if not input_path.exists():
        print(f"[ERROR] 输入文件不存在：{input_path}")
        sys.exit(1)

    if args.command == "init":
        do_init(input_path, outdir)
    elif args.command == "generate":
        required_path = Path(args.required)
        config_path = Path(args.config)

        if not required_path.exists():
            print(f"[ERROR] required_fields.json 不存在：{required_path}")
            sys.exit(1)

        if not config_path.exists():
            print(f"[ERROR] config.json 不存在：{config_path}")
            sys.exit(1)

        do_generate(input_path, outdir, required_path, config_path)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()