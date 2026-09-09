"""Toliss 风格 ``键 = 值`` 文本（livery.tlscfg / tlsconfig / livery.cfg）的解析与写回。

示例文件内容::

    eng_type = PWG
    has_MultiFunRwyLights = NO
    has_satcom = NO
    has_eRudder = NO
    use_Imperial_Units = \tYES

本模块把文件建模为「有序条目列表」，注释与未知键在写回时得以保留，
只替换 / 追加用户修改过的键。这样即使用户的机模版本包含本工具未知的
键，也不会在打包时丢失。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Union

# 视为注释/无意义行的行首前缀
_COMMENT_PREFIXES = ("#", "//", ";")


class Comment:
    """文件中的注释行或空行（原样保留）。"""

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text

    def render(self) -> str:
        return self.text


class KV:
    """一条 ``key = value`` 记录。"""

    __slots__ = ("key", "value")

    def __init__(self, key: str, value: str) -> None:
        self.key = key
        self.value = value

    def render(self) -> str:
        return f"{self.key} = {self.value}"


Entry = Union[Comment, KV]


def parse(text: str) -> List[Entry]:
    """把配置文本解析为有序条目列表。"""
    entries: List[Entry] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        stripped = line.strip()
        if not stripped or stripped.startswith(_COMMENT_PREFIXES):
            entries.append(Comment(line))
            continue
        eq = line.find("=")
        if eq <= 0:  # 没有 "=" 或键为空，按注释保留
            entries.append(Comment(line))
            continue
        key = line[:eq].strip()
        value = line[eq + 1:].strip()
        if not key:
            entries.append(Comment(line))
            continue
        entries.append(KV(key, value))
    return entries


def parse_file(path: str) -> List[Entry]:
    """读取并解析一个配置文件，编码兼容 UTF-8 / GBK / latin-1。"""
    with open(path, "rb") as fh:
        data = fh.read()
    text = _decode_text(data)
    return parse(text)


def _decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def serialize(entries: List[Entry]) -> str:
    """把条目列表写回为文本。"""
    if not entries:
        return ""
    lines = [e.render() for e in entries]
    return "\n".join(lines) + "\n"


def to_dict(entries: List[Entry]) -> Dict[str, str]:
    return {e.key: e.value for e in entries if isinstance(e, KV)}


def get(entries: List[Entry], key: str, default: Optional[str] = None) -> Optional[str]:
    for e in entries:
        if isinstance(e, KV) and e.key == key:
            return e.value
    return default


def set_kv(entries: List[Entry], key: str, value: str) -> None:
    """原地替换同键值；不存在则在文件末尾追加一条。"""
    for e in entries:
        if isinstance(e, KV) and e.key == key:
            e.value = value
            return
    entries.append(KV(key, value))


def normalize_bool(value: str) -> str:
    """把常见的布尔写法归一化为 YES / NO（保留原意）。"""
    return "YES" if str(value).strip().upper() in ("YES", "Y", "TRUE", "1", "ON") else "NO"
