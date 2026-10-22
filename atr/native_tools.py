"""Operator-owned builtins and independent examples, not model-supplied checks."""

from __future__ import annotations

from typing import Any

from atr.control.authority import BehaviorRule, TrustedCase
from atr.models import Check, Manifest, Program

SOURCE = """import csv, io, json, sys
x = json.load(sys.stdin)
op = x["operation"]
if TOOL == "native.json":
    value = x["value"]
    if op == "select":
        for part in x.get("path", []):
            if isinstance(value, list) and (type(part) is not int or part < 0):
                raise ValueError("array indices must be nonnegative integers")
            if isinstance(value, dict) and not isinstance(part, str):
                raise ValueError("object keys must be strings")
            value = value[part]
    elif op == "format":
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    elif op == "keys" and isinstance(value, dict):
        value = sorted(value)
    else:
        raise ValueError("unsupported JSON operation")
elif TOOL == "native.csv":
    if op == "parse":
        reader = csv.DictReader(io.StringIO(x["text"], newline=""))
        fields = reader.fieldnames
        if not fields or len(set(fields)) != len(fields):
            raise ValueError("CSV needs unique nonempty header")
        value = list(reader)
        if any(None in row or None in row.values() for row in value):
            raise ValueError("CSV row width differs from header")
    elif op == "write":
        fields = x["fields"]
        if len(set(fields)) != len(fields):
            raise ValueError("duplicate fields")
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\\n")
        writer.writeheader()
        writer.writerows(x["rows"])
        value = output.getvalue()
    else:
        raise ValueError("unsupported CSV operation")
else:
    lines = x["text"].splitlines()
    if op == "unique_lines":
        value = list(dict.fromkeys(lines))
    elif op == "sort_lines":
        value = sorted(lines)
    elif op == "count":
        value = {"lines": len(lines), "characters": len(x["text"]), "words": len(x["text"].split())}
    else:
        raise ValueError("unsupported text operation")
json.dump({"tool": TOOL, "value": value}, sys.stdout, ensure_ascii=False, allow_nan=False)
"""


def builtins() -> list[tuple[Program, list[TrustedCase], BehaviorRule]]:
    examples: dict[str, list[tuple[dict[str, Any], Any]]] = {
        "json": [
            ({"operation": "select", "value": {"a": [None, 9]}, "path": ["a", 1]}, 9),
            ({"operation": "keys", "value": {"z": 0, "a": 1}}, ["a", "z"]),
            ({"operation": "select", "value": None}, None),
        ],
        "csv": [
            ({"operation": "parse", "text": 'a,b\n"中,文",x\n'}, [{"a": "中,文", "b": "x"}]),
            ({"operation": "write", "fields": ["a"], "rows": [{"a": "x,y"}]}, 'a\n"x,y"\n'),
        ],
        "text": [
            ({"operation": "unique_lines", "text": "b\na\nb\n"}, ["b", "a"]),
            ({"operation": "sort_lines", "text": "z\na"}, ["a", "z"]),
            ({"operation": "count", "text": "a b\nc"}, {"lines": 2, "characters": 5, "words": 3}),
        ],
    }
    descriptions = {
        "json": "JSON 内联数据：select 路径提取、format 格式化、keys 排序键名",
        "csv": "CSV 与 JSON 转换：parse 解析带引号 CSV、write 从行对象生成 CSV",
        "text": "文本行处理：unique_lines 保序去重、sort_lines 排序、count 字符词行统计",
    }
    properties: dict[str, dict[str, Any]] = {
        "json": {
            "operation": {"enum": ["select", "format", "keys"]},
            "value": {},
            "path": {"type": "array", "maxItems": 64, "items": {"type": ["string", "integer"]}},
        },
        "csv": {
            "operation": {"enum": ["parse", "write"]},
            "text": {"type": "string", "maxLength": 131072},
            "fields": {"type": "array", "maxItems": 1000, "items": {"type": "string"}},
            "rows": {"type": "array", "maxItems": 10000, "items": {"type": "object"}},
        },
        "text": {
            "operation": {"enum": ["unique_lines", "sort_lines", "count"]},
            "text": {"type": "string", "maxLength": 131072},
        },
    }
    result = []
    for name, cases in examples.items():
        tool = "native." + name
        rule = BehaviorRule(checks=[Check(expected_path="tool", expected=tool)])
        program = Program(
            manifest=Manifest(
                id=tool,
                scope="global",
                description=descriptions[name],
                aliases=[name, name.upper()],
                tags=[name, "内联", "纯函数"],
                input_schema={
                    "type": "object",
                    "properties": properties[name],
                    "required": [
                        "operation",
                        *(["value"] if name == "json" else ["text"] if name == "text" else []),
                    ],
                    "additionalProperties": False,
                },
                output_schema={
                    "type": "object",
                    "properties": {"tool": {"const": tool}, "value": {}},
                    "required": ["tool", "value"],
                    "additionalProperties": False,
                },
            ),
            files={"main.py": f"TOOL = {tool!r}\n" + SOURCE},
        )
        trusted = [
            TrustedCase(
                inputs=inputs,
                rule=BehaviorRule(checks=[Check(expected={"tool": tool, "value": expected})]),
                name=f"operator example {i + 1}",
            )
            for i, (inputs, expected) in enumerate(cases)
        ]
        result.append((program, trusted, rule))
    return result
