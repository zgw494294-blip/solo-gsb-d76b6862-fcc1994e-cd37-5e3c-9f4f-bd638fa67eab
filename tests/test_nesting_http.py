"""嵌套批注审计 HTTP 接口：深树（超默认递归上限）端到端回归。

验证 POST /api/nesting/preview 与 /api/nesting/export 在默认递归上限下返回
完整的原嵌套 JSON（不提高全局递归上限、不限制深度、不扁平化）；原字段、
父子排序、预览错误列表、非法输入 422、有审计错误时导出 409 均保持不变。
"""

import importlib

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COPY_DB_PATH", str(tmp_path / "copies.db"))
    import app.main as main
    importlib.reload(main)
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c
    main._store = None


def ann(i, label="L"):
    return {"id": str(i), "start": i, "end": 2400 - i, "label": label}


DEEP_RULES = {"root_labels": ["L"], "children": {"L": ["L"]}}


def deep_payload(n=1200, mutate=None):
    anns = [ann(i) for i in range(n)]
    if mutate:
        mutate(anns)
    return {"text": "x" * 2400, "annotations": anns, "rules": DEEP_RULES}


def follow_chain(body_builder, n, *, expect_text):
    """迭代沿响应体单链向下校验完整原嵌套 JSON（不递归解析，避免测试工具自身触顶）。"""
    body = body_builder()
    # 顶层前缀：preview 为 {"errors":[],"tree":[…；export 为 {"tree":[…
    # 节点字段顺序与各层单传、原字段值均须保留
    assert body.count('"children"') == n
    if expect_text:
        assert body.count('"text"') == n
    else:
        assert '"text"' not in body
    for i in range(n):
        frag = f'"id":"{i}","label":"L","start":{i},"end":{2400 - i}'
        assert frag in body
    return body


def test_preview_deep_tree_returns_full_nested_json(client):
    n = 1200
    r = client.post("/api/nesting/preview", json=deep_payload(n))
    assert r.status_code == 200, r.text[:300]
    assert r.headers["content-type"].startswith("application/json")
    body = follow_chain(r.content.decode, n, expect_text=True)
    assert body.startswith('{"errors":[],"tree":[{"id":"0","label":"L",'
                           '"start":0,"end":2400,"text":"')
    # 最内层 id=1199 是叶子；外层 id=0 为唯一根
    assert '"id":"1199","label":"L","start":1199,"end":1201' in body
    assert '"id":"1200"' not in body


def test_export_deep_tree_returns_full_nested_json(client):
    n = 1200
    r = client.post("/api/nesting/export", json=deep_payload(n))
    assert r.status_code == 200, r.text[:300]
    body = follow_chain(r.content.decode, n, expect_text=False)
    assert body.startswith('{"tree":[{"id":"0","label":"L","start":0,"end":2400,'
                           '"children":[{"id":"1"')
    assert body.endswith('"children":[]' + "}" + "]}" * (n - 1) + '],"count":1200}')


def test_preview_deep_illegal_label_still_reports_per_rules(client):
    # 深层非法 label 仍按原 child_label 规则报告：600 自身 + 其子 601，共 2 条
    def mutate(anns):
        anns[600] = ann(600, "X")

    r = client.post("/api/nesting/preview", json=deep_payload(mutate=mutate))
    assert r.status_code == 200, r.text[:300]
    assert r.content.decode().count('"type":"child_label"') == 2
    assert '"ids":["599","600"]' in r.text
    assert '"ids":["600","601"]' in r.text
    assert '"type":"root_label"' not in r.text
    # 树照常返回（label 错误不影响建树）
    assert r.content.decode().count('"children"') == 1200


def test_export_deep_tree_with_illegal_label_is_409(client):
    def mutate(anns):
        anns[600] = ann(600, "X")

    r = client.post("/api/nesting/export", json=deep_payload(mutate=mutate))
    assert r.status_code == 409
    data = r.json()
    assert data["detail"]["message"] == "存在嵌套审计错误，禁止导出"
    assert [(e["type"], e["ids"]) for e in data["detail"]["errors"]] == [
        ("child_label", ["599", "600"]),
        ("child_label", ["600", "601"]),
    ]


def test_invalid_input_still_422(client):
    cases = [
        # 区间越界
        {"text": "x", "annotations": [{"id": "a", "start": 0, "end": 5, "label": "L"}],
         "rules": DEEP_RULES},
        # 空 label
        {"text": "xx", "annotations": [{"id": "a", "start": 0, "end": 2, "label": ""}],
         "rules": DEEP_RULES},
        # 重复 id
        {"text": "xx",
         "annotations": [{"id": "a", "start": 0, "end": 2, "label": "L"},
                          {"id": "a", "start": 1, "end": 2, "label": "L"}],
         "rules": DEEP_RULES},
        # root_labels 含重复 label
        {"text": "xx", "annotations": [{"id": "a", "start": 0, "end": 2, "label": "L"}],
         "rules": {"root_labels": ["L", "L"], "children": {}}},
        # children 列表含重复 label
        {"text": "xx", "annotations": [{"id": "a", "start": 0, "end": 2, "label": "L"}],
         "rules": {"root_labels": ["L"], "children": {"L": ["L", "L"]}}},
    ]
    for payload in cases:
        for path in ("/api/nesting/preview", "/api/nesting/export"):
            r = client.post(path, json=payload)
            assert r.status_code == 422, (path, r.status_code, r.text[:200])


def test_shallow_tree_compat_preview_and_export(client):
    # 浅树：字段（含 text 片段）、父子排序与错误列表形态保持不变
    payload = {
        "text": "目标：统一口径。",
        "annotations": [
            {"id": "p", "start": 0, "end": 8, "label": "段落"},
            {"id": "s", "start": 3, "end": 7, "label": "短语"},
        ],
        "rules": {"root_labels": ["段落"], "children": {"段落": ["短语"]}},
    }
    r = client.post("/api/nesting/preview", json=payload)
    assert r.status_code == 200
    assert r.json() == {
        "errors": [],
        "tree": [{
            "id": "p", "label": "段落", "start": 0, "end": 8, "text": "目标：统一口径。",
            "children": [{
                "id": "s", "label": "短语", "start": 3, "end": 7,
                "text": "统一口径", "children": [],
            }],
        }],
    }
    r = client.post("/api/nesting/export", json=payload)
    assert r.status_code == 200
    assert r.json() == {
        "tree": [{
            "id": "p", "label": "段落", "start": 0, "end": 8,
            "children": [{"id": "s", "label": "短语", "start": 3, "end": 7,
                          "children": []}],
        }],
        "count": 2,
    }


def test_shallow_crossing_preview_errors_export_409(client):
    payload = {
        "text": "abcdef",
        "annotations": [
            {"id": "a", "start": 0, "end": 3, "label": "L"},
            {"id": "b", "start": 2, "end": 5, "label": "L"},
        ],
        "rules": DEEP_RULES,
    }
    r = client.post("/api/nesting/preview", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["tree"] is None
    assert [e["type"] for e in data["errors"]] == ["crossing"]
    r = client.post("/api/nesting/export", json=payload)
    assert r.status_code == 409
    assert [e["type"] for e in r.json()["detail"]["errors"]] == ["crossing"]


def test_response_uses_utf8_for_non_ascii_deep_tree(client):
    # 中文 label/原文在深树响应中原样 UTF-8 输出（不经 \\uXXXX 转义）
    n = 1200
    payload = {
        "text": "标" * 2400,
        "annotations": [{"id": str(i), "start": i, "end": 2400 - i, "label": "层"}
                        for i in range(n)],
        "rules": {"root_labels": ["层"], "children": {"层": ["层"]}},
    }
    r = client.post("/api/nesting/preview", json=payload)
    assert r.status_code == 200
    raw = r.content.decode("utf-8")
    assert raw.count('"label":"层"') == n
    assert raw.startswith('{"errors":[],"tree":[{"id":"0","label":"层",'
                          '"start":0,"end":2400,"text":"标标')
    assert "\\u" not in raw
