"""INI 风格配置文本（如 PMDG 的 ``options.ini``）的解析与写回。

示例文件内容::

    [Displays]
    Glass_PFD_ND=1
    ND_MinimumRunwayLength=5000

    [Airframe]
    SatcomAntenna=2
    Yoke CheckList L=1

本模块把文件建模为「有序条目列表」：``Section`` 记录分节标题，分节内、以及
节外的 ``Comment`` / ``KV`` 均按原顺序保留。写回时只替换用户声明过的键，
未声明/未知的键与注释不会丢失，从而兼容机模版本包含本工具未知选项的情况。

与 :mod:`.tlscfg`（无分节的扁平 ``key = value``）互补：此处保留 ``[Section]``，
且键值可以含空格（如 ``Yoke CheckList L``）。
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
        return f"{self.key}={self.value}"


class Section:
    """一个 ``[标题]`` 分节，含该分节下的有序条目。"""

    __slots__ = ("name", "body")

    def __init__(self, name: str) -> None:
        self.name = name
        self.body: List[Entry] = []

    def render(self) -> str:
        lines = [f"[{self.name}]"]
        lines.extend(e.render() for e in self.body)
        return "\n".join(lines)


Entry = Union[Comment, KV, Section]


def parse(text: str) -> List[Entry]:
    """把 INI 文本解析为有序条目列表（保留分节、注释、空行与键顺序）。"""
    entries: List[Entry] = []
    current: Optional[Section] = None

    def append(entry: Entry) -> None:
        if isinstance(entry, KV) and current is not None:
            current.body.append(entry)
        elif current is not None and isinstance(entry, Comment):
            # 空行/注释出现在分节内时归入该分节，避免打乱顺序
            current.body.append(entry)
        else:
            entries.append(entry)

    for raw in text.splitlines():
        line = raw.rstrip("\r")
        stripped = line.strip()
        if not stripped or stripped.startswith(_COMMENT_PREFIXES):
            append(Comment(line))
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current = Section(stripped[1:-1].strip())
            entries.append(current)
            continue
        eq = line.find("=")
        if eq <= 0:  # 没有 "=" 或键为空，按注释保留
            append(Comment(line))
            continue
        key = line[:eq].strip()
        value = line[eq + 1:].strip()
        if not key:
            append(Comment(line))
            continue
        append(KV(key, value))
    return entries


def parse_file(path: str) -> List[Entry]:
    """读取并解析一个配置文件，编码兼容 UTF-8 / GBK / latin-1。"""
    with open(path, "rb") as fh:
        data = fh.read()
    return parse(_decode_text(data))


def _decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def serialize(entries: List[Entry]) -> str:
    """把条目列表写回为文本（分节之间保留原有的空行/注释）。"""
    if not entries:
        return ""
    lines: List[str] = []
    for e in entries:
        lines.append(e.render())
    return "\n".join(lines) + "\n"


def _iter_kv(entries: List[Entry]):
    """深度遍历所有 KV（含分节内）。"""
    for e in entries:
        if isinstance(e, Section):
            for inner in e.body:
                if isinstance(inner, KV):
                    yield inner
        elif isinstance(e, KV):
            yield e


def to_dict(entries: List[Entry]) -> Dict[str, str]:
    """拍平成 {键: 值}（跨分节，键同名时前者优先）。"""
    out: Dict[str, str] = {}
    for kv in _iter_kv(entries):
        out.setdefault(kv.key, kv.value)
    return out


def get(entries: List[Entry], key: str, default: Optional[str] = None) -> Optional[str]:
    for kv in _iter_kv(entries):
        if kv.key == key:
            return kv.value
    return default


def set_key(entries: List[Entry], key: str, value: str,
            section: Optional[str] = None) -> None:
    """原地替换第一个同键记录；不存在则追加。

    默认追加到最后一个分节末尾；指定 ``section`` 时只在目标分节内查找/替换，
    分节不存在则新建该分节再写入（用于键名跨分节同名需落位的情况）。
    """
    if section is not None:
        _set_in_section(entries, section, key, value)
        return
    for kv in _iter_kv(entries):
        if kv.key == key:
            kv.value = value
            return
    # 追加：尽量进入最后一个分节
    for e in reversed(entries):
        if isinstance(e, Section):
            e.body.append(KV(key, value))
            return
    entries.append(KV(key, value))


# 与 tlscfg.set_kv 同名别名，便于引擎统一调用
set_kv = set_key


def _set_in_section(entries: List[Entry], section: str, key: str, value: str) -> None:
    """在指定分节内替换/追加键；分节不存在则新建。"""
    sec: Optional[Section] = None
    for e in entries:
        if isinstance(e, Section) and e.name == section:
            sec = e
            break
    if sec is None:
        sec = Section(section)
        entries.append(sec)
    for kv in sec.body:
        if kv.key == key:
            kv.value = value
            return
    sec.body.append(KV(key, value))
