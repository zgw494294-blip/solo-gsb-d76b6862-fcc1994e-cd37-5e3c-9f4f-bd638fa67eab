"""无递归 JSON 序列化器测试：与 json.dumps 逐字节一致，深链不崩溃。"""

import json
import math
import random
import string

import pytest

from app.iterjson import DeepJSONResponse, dumps

KW = dict(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
# 闭合一个非叶子节点的片段（children 数组 ] + 节点对象 }）
CLOSE_NODE = "]}"


@pytest.mark.parametrize("obj", [
    None, True, False, 0, -7, 12345678901234567890, 1.5, 0.1, 1e100,
    "", "x", "中文标签🎉", 'a"b\\c\n\t\r\b\f', "\x00\x1f\x7f",
    [], [1], [1, 2, 3], [[[]]], [[1], [2, [3, 4]]],
    {}, {"a": 1}, {"a": {"b": [1, 2, {"c": None}]}}, {"errors": [], "tree": []},
    {None: 0, True: 1, False: 2, 7: 3, 2.5: 4, "k": "v"},
])
def test_matches_stdlib_json_for_scalars_and_shapes(obj):
    assert dumps(obj) == json.dumps(obj, **KW)


def test_matches_stdlib_json_for_nesting_response_payload():
    payload = {"errors": [], "tree": [
        {"id": "p", "label": "段落", "start": 0, "end": 8, "text": "目标：统一口径。",
         "children": [
             {"id": "s", "label": "短语", "start": 3, "end": 7,
              "text": "统一口径", "children": []},
         ]},
    ]}
    assert dumps(payload) == json.dumps(payload, **KW)


def test_random_structures_match_stdlib():
    random.seed(20261003)

    def rand(depth=0):
        r = random.random()
        if depth > 6 or r < 0.25:
            return random.choice([
                random.randint(-1000, 1000),
                "".join(random.choice('ab中文"\\\n\x01') for _ in range(random.randrange(5))),
                True, False, None, round(random.random() * 100, 3),
            ])
        n = random.randrange(5)
        if r < 0.6:
            return [rand(depth + 1) for _ in range(n)]
        return {
            "".join(random.choice(string.ascii_letters + '"\\') for _ in range(2)):
                rand(depth + 1)
            for _ in range(n)
        }

    for _ in range(5000):
        obj = rand()
        assert dumps(obj) == json.dumps(obj, **KW)


def _deep_node(n):
    """构造 id=0 在最外层、id=n-1 在最内层的 n 层单链。"""
    node = None
    for i in range(n - 1, -1, -1):
        node = {"id": str(i), "label": "L", "start": i, "end": 2 * n - i,
                "children": [] if node is None else [node]}
    return node


def test_deep_chain_serializes_with_default_recursion_limit():
    # 3000 层嵌套（远超默认 1000）：不提高递归上限即可完整序列化（非扁平结构）
    n = 3000
    out = dumps({"tree": [_deep_node(n)], "count": n})
    assert out.startswith(
        '{"tree":[{"id":"0","label":"L","start":0,"end":6000,"children":[{"id":"1"')
    assert out.count('"children"') == n
    # 叶子 children:[] 后收 }，其余 n-1 个祖先各收 ]}，tree 数组 ] 与顶层对象 } 收尾
    assert out.endswith('"children":[]' + "}" + CLOSE_NODE * (n - 1)
                        + '],"count":3000}')


def test_nan_and_infinity_rejected_like_stdlib():
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError):
            dumps(bad)
        with pytest.raises(ValueError):
            dumps({"x": bad})
        with pytest.raises(ValueError):
            json.dumps(bad, **KW)


def test_unsupported_types_raise_type_error():
    for bad in (object(), {1j: 2}, {(1, 2): 3}, b"bytes"):
        with pytest.raises(TypeError):
            dumps(bad)


def test_response_renders_utf8_bytes_deep_payload():
    n = 1500
    resp = DeepJSONResponse({"tree": [_deep_node(n)], "count": n})
    assert resp.status_code == 200
    # 与 starlette.JSONResponse 相同的媒体类型；render() 产出 UTF-8 字节
    assert resp.media_type == "application/json"
    assert resp.headers["content-type"] == "application/json"
    raw = resp.body
    assert raw.decode("utf-8").startswith(
        '{"tree":[{"id":"0","label":"L","start":0,"end":3000,"children":[{"id":"1"')
    assert raw.decode("utf-8").count('"children"') == n
    # 中文等非 ASCII 以 UTF-8 原样字节输出（不经 \uXXXX 转义）
    zh = DeepJSONResponse({"tree": [{"label": "段落", "children": []}]})
    assert b'"\xe6\xae\xb5\xe8\x90\xbd"' in zh.body
    assert b"\\u" not in zh.body
