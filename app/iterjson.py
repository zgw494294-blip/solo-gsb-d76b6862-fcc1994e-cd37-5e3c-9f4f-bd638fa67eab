"""无递归 JSON 序列化（显式栈迭代实现）。

嵌套审计的合法结果可能是一棵远超 Python 默认递归上限（1000）的深树：
starlette/fastapi 默认响应链用的 json.dumps 是递归实现，序列化深度约 1000
即抛 RecursionError（响应变成 500）。这里提供迭代版 dumps 与配套的
DeepJSONResponse：嵌套深度只增加显式栈长度，不增加调用深度，不提高全局
递归上限，也不改动其他模块的响应编码。

输出与 ``json.dumps(obj, ensure_ascii=False, allow_nan=False,
separators=(",", ":"))`` 逐字节一致（紧凑分隔、非 ASCII 原样、NaN/Infinity
非法）；支持嵌套审计响应所需的 dict / list / str / int / float / bool /
None（dict 的非 str 键按 json.dumps 规则接受 int/float/bool/None）。
"""

from __future__ import annotations

import math

from starlette.responses import Response


def _encode_string(s: str) -> str:
    """按 JSON 规则转义字符串（非 ASCII 原样输出）。"""
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif o < 0x20:
            # U+0000–U+001F 控制字符与 CPython json 的转义集合一致（DEL 不转义）
            out.append(f"\\u{o:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _encode_float(x: float) -> str:
    if x != x or x == math.inf or x == -math.inf:
        raise ValueError("Out of range float values are not JSON compliant")
    return repr(x)


def _encode_key(key) -> str:
    """dict 键编码：str 直接转义；int/float/bool/None 按 json.dumps 规则转成字符串。"""
    if isinstance(key, str):
        return _encode_string(key)
    if key is None:
        return '"null"'
    if isinstance(key, bool):
        return '"true"' if key else '"false"'
    if isinstance(key, int):
        return '"' + str(key) + '"'
    if isinstance(key, float):
        return '"' + _encode_float(key) + '"'
    raise TypeError(
        f"keys must be str, int, float, bool or None, not {key.__class__.__name__}")


# 容器结束哨兵（各自携带开始/结束括号）
_LIST_END = object()
_DICT_END = object()
_COMMA = object()
_COLON = object()


class _Raw:
    """已是最终 JSON 片段的标记（dict 键编码后原样落盘，避免二次转义）。"""

    __slots__ = ("text",)

    def __init__(self, text: str):
        self.text = text


def dumps(obj) -> str:
    """迭代序列化 obj 为紧凑 JSON 字符串（无递归，嵌套深度无上限）。"""
    chunks: list[str] = []
    # 栈元素：JSON 字面量片段、容器内分隔哨兵，或 _LIST_END/_DICT_END
    stack: list = [obj]

    while stack:
        token = stack.pop()
        if token is _LIST_END:
            chunks.append("]")
        elif token is _DICT_END:
            chunks.append("}")
        elif token is _COMMA:
            chunks.append(",")
        elif token is _COLON:
            chunks.append(":")
        elif isinstance(token, _Raw):
            chunks.append(token.text)
        elif isinstance(token, (list, tuple)):
            chunks.append("[")
            # 结束哨兵先入栈底；元素逆序压栈使其正序弹出，元素之间插逗号
            stack.append(_LIST_END)
            for i, value in enumerate(reversed(token)):
                if i:
                    stack.append(_COMMA)
                stack.append(value)
        elif isinstance(token, dict):
            chunks.append("{")
            stack.append(_DICT_END)
            for i, (key, value) in enumerate(reversed(list(token.items()))):
                if i:
                    stack.append(_COMMA)
                stack.append(value)
                stack.append(_COLON)
                stack.append(_Raw(_encode_key(key)))
        elif isinstance(token, str):
            chunks.append(_encode_string(token))
        elif token is None:
            chunks.append("null")
        elif token is True:
            chunks.append("true")
        elif token is False:
            chunks.append("false")
        elif isinstance(token, int):
            chunks.append(str(token))
        elif isinstance(token, float):
            chunks.append(_encode_float(token))
        else:
            raise TypeError(
                f"Object of type {token.__class__.__name__} is not JSON serializable")

    return "".join(chunks)


class DeepJSONResponse(Response):
    """与 starlette JSONResponse 兼容，但用无递归 dumps 渲染（深树不 500）。"""

    media_type = "application/json"

    def render(self, content) -> bytes:
        return dumps(content).encode("utf-8")
