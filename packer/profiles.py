"""机模档案（Profile）加载与自动识别。

每个机模厂商/机型对应 ``profiles/*.json`` 一个档案，定义：

- 如何从源文件夹**识别**该机模（检测规则与权重）；
- 交互式**字段**（写入配置文件或仅用于命名的元数据）；
- 命名模板、图标尺寸、配置文件候选路径等。

新增机模时，在 ``profiles/`` 下复制一份 json 修改即可，无需改动 Python。
"""
from __future__ import annotations

import json
import os
from fnmatch import fnmatch
from typing import Dict, List, Optional

_DEFAULT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "profiles")


class Inventory:
    """源目录的内容清单（内容根已确定后构造），供检测规则匹配。"""

    def __init__(self, files: List[str], root_name: str):
        # files: 相对内容根的路径，统一小写 + 正斜杠
        self.files_lower = {f.lower().replace("\\", "/") for f in files}
        self.root_name = (root_name or "").lower()

    def _candidates(self, path: str) -> List[str]:
        p = path.lower().replace("\\", "/").lstrip("/")
        if any(ch in p for ch in "*?["):
            return [f for f in self.files_lower if fnmatch(f, p)]
        return [f for f in self.files_lower if f == p or f.endswith("/" + p)]


def rule_matches(rule: Dict, inv: Inventory) -> bool:
    kind = rule.get("type")
    if kind == "file_exists":
        return bool(inv._candidates(str(rule.get("path", ""))))
    if kind == "file_glob":
        pat = str(rule.get("path", "")).lower().replace("\\", "/")
        if pat.startswith("/"):
            pat = pat[1:]
        return any(fnmatch(f, pat) for f in inv.files_lower)
    if kind == "name_contains":
        val = str(rule.get("value", "")).lower()
        return val in inv.root_name
    if kind == "file_name_contains":
        val = str(rule.get("value", "")).lower()
        return any(val in os.path.basename(f) for f in inv.files_lower)
    return False


def load_profiles(profiles_dir: Optional[str] = None) -> List[dict]:
    """读取全部档案 json，返回列表；无法解析的文件会被跳过。"""
    directory = os.path.abspath(profiles_dir or _DEFAULT_DIR)
    profiles: List[dict] = []
    if os.path.isdir(directory):
        for name in sorted(os.listdir(directory)):
            if not name.lower().endswith(".json"):
                continue
            try:
                with open(os.path.join(directory, name), "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict) and data.get("id"):
                    data["_file"] = os.path.join(directory, name)
                    profiles.append(data)
            except (OSError, json.JSONDecodeError):
                continue
    return profiles


def is_fallback(profile: dict) -> bool:
    """没有检测规则的档案作为兜底档案（仅当其它档案都不匹配时使用）。"""
    return not profile.get("detect")


def pick_profile(profiles: List[dict], inv: Inventory) -> dict:
    """对源内容清单打分，返回最匹配的档案（无匹配则返回兜底档案）。"""
    best: Optional[dict] = None
    best_score = 0
    fallback: Optional[dict] = None
    for p in profiles:
        if is_fallback(p):
            if fallback is None:
                fallback = p
            continue
        score = sum(
            int(r.get("weight", 0))
            for r in p.get("detect", [])
            if rule_matches(r, inv)
        )
        if score >= int(p.get("minScore", 0)) and score > best_score:
            best = p
            best_score = score
    return best if best is not None else (fallback or {"id": "generic_xplane"})


def get_profile(profiles: List[dict], pid: str) -> Optional[dict]:
    for p in profiles:
        if p.get("id") == pid:
            return p
    return None


def build_inventory(content_root: str) -> Inventory:
    """从内容根目录直接构造清单（引擎未介入时使用）。"""
    files: List[str] = []
    for base, _dirs, names in os.walk(content_root):
        for n in names:
            rel = os.path.relpath(os.path.join(base, n), content_root)
            files.append(rel)
    return Inventory(files, os.path.basename(content_root))
