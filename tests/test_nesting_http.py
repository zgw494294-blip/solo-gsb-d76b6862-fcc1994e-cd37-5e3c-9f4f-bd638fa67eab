"""嵌套批注审计 HTTP 接口回归（FastAPI TestClient）。

深树场景：2400 个 "x"，1200 条批注 id=str(i)、[i, 2400-i)、label="L"，
规则 root_labels=["L"]、children={"L": ["L"]}。预览与导出必须在默认
递归上限下返回完整的原嵌套 JSON——不提高全局递归上限、不限制深度、
不改为扁平结构。标准库 json.loads 同样是递归实现，无法解析上千层的
响应体，因此本文件自带迭代式 JSON 解析器（parse_deep）用于断言。
"""

import importlib
import json
import re
import sys

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 与 test_copy_api 一致：临时 SQLite 路径，避免污染本地 data/copies.db
    monkeypatch.setenv("COPY_DB_PATH", str(tmp_path / "copies.db"))
    import app.main as main
    importlib.reload(main)
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c
    main._store = None


_TOKEN = re.compile(r'''
      "(?:\\.|[^"\\])*"                                # 字符串
    | -?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?      # 数字
    | true | false | null
    | [}{[\]:,]
''', re.VERBOSE)


def parse_deep(s: str):
    """迭代式 JSON 解析（测试工具）：任意嵌套深度都不占用递归栈。

    标量（字符串 / 数字 / true / false / null）仍交给 json.loads 逐个解析，
    容器用显式栈组装；仅用于解析服务端输出的合法 JSON。
    """
    result = None
    have_result = False
    stack = []  # 帧：["dict", 对象, 暂存键] 或 ["list", 数组]

    def attach(value):
        nonlocal result, have_result
        if not stack:
            assert not have_result, "JSON 含多个顶层值"
            result, have_result = value, True
        elif stack[-1][0] == "list":
            stack[-1][1].append(value)
        else:
            key = stack[-1][2]
            assert key is not None, "对象值缺少键"
            stack[-1][1][key] = value
            stack[-1][2] = None

    for m in _TOKEN.finditer(s):
        tok = m.group(0)
        ch = tok[0]
        if ch == "{":
            obj = {}
            attach(obj)
            stack.append(["dict", obj, None])
        elif ch == "[":
            arr = []
            attach(arr)
            stack.append(["list", arr])
        elif ch == "}":
            kind, _, pending = stack.pop()
            assert kind == "dict" and pending is None, "对象未闭合"
        elif ch == "]":
            kind, _ = stack.pop()
            assert kind == "list", "数组未闭合"
        elif ch in ":,":
            continue
        elif ch == '"' and stack and stack[-1][0] == "dict" and stack[-1][2] is None:
            stack[-1][2] = json.loads(tok)  # 对象键
        else:
            attach(json.loads(tok))  # 字符串值 / 数字 / true / false / null
    assert have_result and not stack, "JSON 不完整"
    return result


def test_parse_deep_roundtrip_shallow():
    payload = {"a": [1, "x", None, True, {"b": [], "c": {"d": 2.5}}], "中": "文"}
    assert parse_deep(json.dumps(payload, ensure_ascii=False)) == payload


def ann(id, start, end, label):
    return {"id": id, "start": start, "end": end, "label": label}


def deep_payload(depth=1200):
    return {
        "text": "x" * (2 * depth),
        "annotations": [ann(str(i), i, 2 * depth - i, "L") for i in range(depth)],
        "rules": {"root_labels": ["L"], "children": {"L": ["L"]}},
    }


def walk_chain(node):
    """沿单链树逐层产出节点（迭代，避免测试自身递归）。"""
    while True:
        yield node
        assert len(node["children"]) <= 1
        if not node["children"]:
            return
        node = node["children"][0]


# ---------- 深树：默认递归上限下返回完整原嵌套 JSON ----------

def test_deep_tree_preview_returns_full_nested_json(client):
    limit = sys.getrecursionlimit()
    r = client.post("/api/nesting/preview", json=deep_payload())
    assert r.status_code == 200, r.text[:500]
    assert r.headers["content-type"].startswith("application/json")
    data = parse_deep(r.text)
    assert data["errors"] == []
    (root,) = data["tree"]
    for i, node in enumerate(walk_chain(root)):
        assert (node["id"], node["label"], node["start"], node["end"]) == \
            (str(i), "L", i, 2400 - i)
        assert node["text"] == "x" * (2400 - 2 * i)
    assert i == 1199
    assert sys.getrecursionlimit() == limit  # 未提高全局递归上限


def test_deep_tree_export_returns_full_nested_json(client):
    r = client.post("/api/nesting/export", json=deep_payload())
    assert r.status_code == 200, r.text[:500]
    data = parse_deep(r.text)
    assert data["count"] == 1200
    (root,) = data["tree"]
    for i, node in enumerate(walk_chain(root)):
        assert (node["id"], node["label"], node["start"], node["end"]) == \
            (str(i), "L", i, 2400 - i)
        assert set(node) == {"id", "label", "start", "end", "children"}
    assert i == 1199


def test_deep_tree_illegal_label_reported_and_export_blocked(client):
    # 深层非法 label 仍按原规则报告；存在审计错误时导出 409
    payload = deep_payload()
    payload["annotations"][1199]["label"] = "BAD"
    r = client.post("/api/nesting/preview", json=payload)
    assert r.status_code == 200, r.text[:500]
    data = parse_deep(r.text)
    assert [e["type"] for e in data["errors"]] == ["child_label"]
    assert data["errors"][0]["ids"] == ["1198", "1199"]
    (root,) = data["tree"]
    assert sum(1 for _ in walk_chain(root)) == 1200

    r = client.post("/api/nesting/export", json=payload)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["message"] == "存在嵌套审计错误，禁止导出"
    assert [e["type"] for e in detail["errors"]] == ["child_label"]
    assert detail["errors"][0]["ids"] == ["1198", "1199"]


# ---------- 浅树兼容：响应格式与状态码不变 ----------

def shallow_payload():
    return {
        "text": "第一章 总则\n本模块负责数据采集与清洗，目标是统一口径。",
        "annotations": [
            ann("n-doc", 0, 28, "文档"),
            ann("n-ch1", 0, 6, "章节"),
            ann("n-title", 4, 6, "短语"),
            ann("n-body", 6, 28, "段落"),
            ann("n-goal", 20, 27, "句子"),
            ann("n-core", 23, 27, "短语"),
        ],
        "rules": {
            "root_labels": ["文档"],
            "children": {
                "文档": ["章节"],
                "章节": ["句子"],
                "段落": ["句子", "短语"],
                "句子": ["短语"],
            },
        },
    }


def test_shallow_preview_format_unchanged(client):
    r = client.post("/api/nesting/preview", json=shallow_payload())
    assert r.status_code == 200, r.text[:500]
    data = r.json()
    assert [e["type"] for e in data["errors"]] == ["child_label", "child_label"]
    assert [e["ids"] for e in data["errors"]] == \
        [["n-ch1", "n-title"], ["n-doc", "n-body"]]
    assert data["tree"] == [
        {"id": "n-doc", "label": "文档", "start": 0, "end": 28,
         "text": "第一章 总则\n本模块负责数据采集与清洗，目标是统一口径。",
         "children": [
             {"id": "n-ch1", "label": "章节", "start": 0, "end": 6,
              "text": "第一章 总则",
              "children": [
                  {"id": "n-title", "label": "短语", "start": 4, "end": 6,
                   "text": "总则", "children": []},
              ]},
             {"id": "n-body", "label": "段落", "start": 6, "end": 28,
              "text": "\n本模块负责数据采集与清洗，目标是统一口径。",
              "children": [
                  {"id": "n-goal", "label": "句子", "start": 20, "end": 27,
                   "text": "目标是统一口径",
                   "children": [
                       {"id": "n-core", "label": "短语", "start": 23, "end": 27,
                        "text": "统一口径", "children": []},
                   ]},
              ]},
         ]},
    ]


def test_shallow_export_format_unchanged(client):
    payload = shallow_payload()
    # 修正两处 label 错误后为合法输入
    payload["annotations"][2]["label"] = "句子"   # n-title：章节 → 句子 ✓
    payload["annotations"][3]["label"] = "章节"   # n-body：文档 → 章节 ✓
    r = client.post("/api/nesting/export", json=payload)
    assert r.status_code == 200, r.text[:500]
    assert r.json() == {
        "tree": [
            {"id": "n-doc", "label": "文档", "start": 0, "end": 28,
             "children": [
                 {"id": "n-ch1", "label": "章节", "start": 0, "end": 6,
                  "children": [
                      {"id": "n-title", "label": "句子", "start": 4, "end": 6,
                       "children": []},
                  ]},
                 {"id": "n-body", "label": "章节", "start": 6, "end": 28,
                  "children": [
                      {"id": "n-goal", "label": "句子", "start": 20, "end": 27,
                       "children": [
                           {"id": "n-core", "label": "短语", "start": 23,
                            "end": 27, "children": []},
                       ]},
                  ]},
             ]},
        ],
        "count": 6,
    }


def test_shallow_export_conflict_when_errors(client):
    r = client.post("/api/nesting/export", json=shallow_payload())
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["message"] == "存在嵌套审计错误，禁止导出"
    assert [e["type"] for e in detail["errors"]] == ["child_label", "child_label"]


def test_span_errors_still_suppress_tree(client):
    payload = shallow_payload()
    payload["annotations"] = [ann("a", 0, 3, "文档"), ann("b", 2, 5, "文档")]
    r = client.post("/api/nesting/preview", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert [e["type"] for e in data["errors"]] == ["crossing"]
    assert data["tree"] is None


# ---------- 非法输入 422 不变 ----------

def test_invalid_interval_422(client):
    payload = shallow_payload()
    payload["annotations"] = [ann("a", 5, 3, "文档")]
    assert client.post("/api/nesting/preview", json=payload).status_code == 422
    assert client.post("/api/nesting/export", json=payload).status_code == 422


def test_empty_label_422(client):
    payload = shallow_payload()
    payload["annotations"] = [ann("a", 0, 3, "")]
    assert client.post("/api/nesting/preview", json=payload).status_code == 422


def test_duplicate_id_422(client):
    payload = shallow_payload()
    payload["annotations"] = [ann("a", 0, 3, "文档"), ann("a", 1, 2, "文档")]
    assert client.post("/api/nesting/preview", json=payload).status_code == 422


def test_duplicate_rule_labels_422(client):
    payload = shallow_payload()
    payload["rules"] = {"root_labels": ["文档", "文档"], "children": {}}
    assert client.post("/api/nesting/preview", json=payload).status_code == 422
    payload["rules"] = {"root_labels": [], "children": {"文档": ["章节", "章节"]}}
    assert client.post("/api/nesting/export", json=payload).status_code == 422
