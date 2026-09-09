"""X-Plane 涂装打包引擎。

负责把用户提供的「源涂装文件夹」整理、复制并生成 X-Plane 可识别的涂装：

1. 找出源文件夹里的**内容根目录**（兼容 zip 解压产生的多一层外文件夹）；
2. 按机模档案规整布局：贴图收进 ``objects/``、图标放到涂装根目录；
3. 若档案有配置文件（如 ToLiss 的 ``objects/livery.tlscfg``），把 UI 选项与
   源文件里未知的键合并写回（保留注释与顺序）；
4. 缺失缩略图时自动由大图生成；
5. 按 ``[型号] 航司 注册号`` 之类的模板命名并输出到目标目录。
"""
from __future__ import annotations

import os
import posixpath
import re
import shutil
import subprocess
import sys
import time
from fnmatch import fnmatch
from typing import Dict, List, Optional, Tuple

from . import dds
from . import pngutil
from . import tlscfg
from . import profiles as profiles_mod

# ---------------------------------------------------------------- 常量
_JUNK_DIRS = {"__macosx", ".git", ".svn", ".idea", ".vscode", ".hg"}
_JUNK_NAMES = {"thumbs.db", ".ds_store", "desktop.ini", "autorun.inf"}
_JUNK_EXT = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".exe", ".lnk",
    ".tmp", ".temp", ".bak", ".download", ".crdownload", ".dmg", ".part",
}
_TEXTURE_EXT = {".png", ".dds", ".tga", ".bmp", ".jpg", ".jpeg"}
_ICON_NAME_RE = re.compile(r"^.*_icon11(?:_thumb)?\.png$", re.IGNORECASE)
_CONFIG_NAMES = {"livery.tlscfg", "tlsconfig", "livery.cfg", "tls.cfg"}
_MAX_PURE_GEN_PIXELS = 16_000_000  # 无 Pillow 时仅对小贴图自动生成图标


class LiveryError(Exception):
    """业务错误，message 可直接展示给用户。"""


class FileInfo:
    __slots__ = ("rel_posix", "rel_native", "abs_path")

    def __init__(self, rel_posix: str, abs_path: str):
        self.rel_posix = rel_posix
        self.rel_native = rel_posix.replace("/", os.sep)
        self.abs_path = abs_path


class SourceInfo:
    def __init__(self, content_root: str):
        self.content_root = content_root
        self.root_name = os.path.basename(content_root)
        self.files: List[FileInfo] = []

    @property
    def inventory(self) -> profiles_mod.Inventory:
        return profiles_mod.Inventory(
            [f.rel_posix for f in self.files], self.root_name
        )


# ---------------------------------------------------------------- 分析
def _is_junk_name(name: str) -> bool:
    low = name.lower()
    if low in _JUNK_NAMES:
        return True
    ext = os.path.splitext(low)[1]
    return ext in _JUNK_EXT or low.endswith("~") or low.startswith("~$")


def _looks_like_root_here(root: str) -> bool:
    """顶层（不递归）直接出现 objects 目录 / 图标 / 贴图 / 配置即视为涂装内容根。"""
    try:
        entries = os.scandir(root)
    except OSError:
        return False
    with entries as it:
        for e in it:
            low = e.name.lower()
            if e.is_dir():
                if low == "objects":
                    return True
                continue
            if _is_junk_name(e.name):
                continue
            if _is_icon_name(e.name) or _is_config_name(e.name) or \
                    os.path.splitext(low)[1] in _TEXTURE_EXT:
                return True
    return False


def _count_content(root: str) -> int:
    n = 0
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d.lower() not in _JUNK_DIRS]
        for name in names:
            if _is_junk_name(name):
                continue
            low = os.path.basename(name).lower()
            if os.path.splitext(low)[1] in _TEXTURE_EXT or _ICON_NAME_RE.match(low):
                n += 1
    return n


def resolve_content_root(source: str) -> str:
    """兼容「zip 解压多包几层外文件夹」等情况，返回真正的内容根。"""
    source = os.path.abspath(source)
    if not os.path.isdir(source):
        raise LiveryError("源文件夹不存在或无法访问")
    cur = source
    while True:
        if _looks_like_root_here(cur):
            return cur
        children = [os.path.join(cur, d) for d in os.listdir(cur)
                    if os.path.isdir(os.path.join(cur, d))
                    and d.lower() not in _JUNK_DIRS]
        root_like = [c for c in children if _looks_like_root_here(c)]
        if len(root_like) == 1:
            return root_like[0]
        if len(root_like) > 1:
            return max(root_like, key=lambda c: _count_content(c))
        if len(children) == 1:
            if _count_content(children[0]) > 0:
                cur = children[0]
                continue
            return cur
        # 多个平级子目录：取贴图最多的一个
        if children:
            best = max(children, key=lambda c: _count_content(c))
            if _count_content(best) > 0:
                return best
        return cur


def scan_source(source: str, recursive=False) -> SourceInfo:
    """扫描源文件夹，返回内容清单（自动跳过垃圾文件/目录）。"""
    root = resolve_content_root(source)
    info = SourceInfo(root)
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d.lower() not in _JUNK_DIRS]
        for n in sorted(names):
            if _is_junk_name(n):
                continue
            full = os.path.join(base, n)
            rel = os.path.relpath(full, root).replace("\\", "/")
            info.files.append(FileInfo(rel, full))
    return info


# ---------------------------------------------------------------- 分类
def _is_icon_name(name: str) -> bool:
    return bool(_ICON_NAME_RE.match(name))


def _is_config_name(name: str) -> bool:
    low = name.lower()
    return low in _CONFIG_NAMES or low.endswith(".tlscfg")


def _is_texture_name(name: str) -> bool:
    return os.path.splitext(name.lower())[1] in _TEXTURE_EXT


def _normalize_rel(raw: str) -> str:
    """规范化目标相对路径（统一 posix 分隔符）；非法路径抛 LiveryError。"""
    p = posixpath.normpath(str(raw or "").replace("\\", "/"))
    if (not p or p == "." or p.startswith("/") or p.startswith("../")
            or "/../" in p or "\\" in p):
        raise LiveryError(f"非法的目标路径：{raw!r}")
    return p


def find_existing_config(info: SourceInfo, profile: dict) -> Optional[str]:
    """在内容根中寻找已存在的配置文件（按候选顺序）。"""
    candidates = profile.get("config", {}).get("paths", []) or []
    cand_low = {c.lower().replace("\\", "/") for c in candidates}
    for fi in info.files:
        if fi.rel_posix.lower() in cand_low:
            return fi.abs_path
    # 兜底：任意 .tlscfg
    for fi in info.files:
        if fi.rel_posix.lower().endswith(".tlscfg"):
            return fi.abs_path
    return None


def list_icons(info: SourceInfo) -> List[dict]:
    out = []
    for fi in info.files:
        base = os.path.basename(fi.rel_posix)
        if _is_icon_name(base):
            out.append({
                "name": base,
                "isThumb": "_thumb" in base.lower(),
                "size": _png_size(fi.abs_path),
            })
    return out


def _png_size(path: str) -> Optional[List[int]]:
    try:
        with open(path, "rb") as fh:
            head = fh.read(26)
        if head[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        import struct
        w, h = struct.unpack(">II", head[16:24])
        return [w, h]
    except OSError:
        return None


# ---------------------------------------------------------------- 命名
_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WS = re.compile(r"\s+")


def fill_template(tpl: str, values: Dict[str, str]) -> str:
    def rep(m):
        return str(values.get(m.group(1), "") or "")
    return re.sub(r"\{(\w+)\}", rep, tpl or "")


def sanitize_name(name: str) -> str:
    name = _INVALID_CHARS.sub("", name)
    name = _WS.sub(" ", name).strip()
    name = name.rstrip(". ")
    return name


def build_livery_name(profile: dict, values: Dict[str, str]) -> str:
    naming = profile.get("naming", {}) or {}
    prefix = fill_template(naming.get("prefixTemplate", ""), values).strip()
    core = fill_template(naming.get("nameTemplate", "") or naming.get("coreTemplate", ""), values).strip()
    parts = [p for p in (prefix, core) if p]
    name = sanitize_name(" ".join(parts))
    if not name:
        name = "Livery_" + time.strftime("%Y%m%d_%H%M%S")
    return name


# ---------------------------------------------------------------- 打包
def declared_config_keys(profile: dict) -> set:
    return {f.get("key") for f in profile.get("fields", []) if f.get("target") == "config"}

def meta_keys(profile: dict) -> set:
    return {f.get("key") for f in profile.get("fields", []) if f.get("target") == "meta"}


def _serialize_field_value(field: dict, value) -> str:
    val = "" if value is None else str(value)
    if field.get("type") == "bool":
        return tlscfg.normalize_bool(val)
    return val


def _copy_one(src_abs: str, dst_abs: str) -> None:
    os.makedirs(os.path.dirname(dst_abs), exist_ok=True)
    shutil.copy2(src_abs, dst_abs)


# ---------------------------------------------------------------- DDS 输出
# X-Plane 12 直接读 .dds (DXT5)。PNG/BMP 等位图打包时自动转成 DDS；只有
# 涂装图标（*_icon11*.png / *_thumb.png）保持 PNG 不变。
def _iconish_name(name: str) -> bool:
    """文件名是否属于“图标/缩略图”类（永不转 DDS，保持 PNG）。"""
    low = name.lower()
    return bool(_ICON_NAME_RE.match(name)) or low.endswith("_thumb.png")


def _raster_dds_dest(dest_rel: str) -> str:
    """把贴图目标路径的位图扩展名改写为 .dds；图标与非贴图保持原样。"""
    base = os.path.basename(dest_rel)
    if _iconish_name(base):
        return dest_rel
    low = dest_rel.lower()
    ext = os.path.splitext(low)[1]
    if ext in _TEXTURE_EXT and ext != ".dds":
        return os.path.splitext(dest_rel)[0] + ".dds"
    return dest_rel


def _needs_convert(src_abs: str, out_rel: str) -> bool:
    """源为位图且目标改名为 .dds 时才需要真正转换。"""
    return (out_rel.lower().endswith(".dds")
            and not src_abs.lower().endswith(".dds"))


def _converter_cmd() -> List[str]:
    """启动转换子进程的基础命令。

    开发态 = ``python server.py --convert ...``；冻结 exe = ``exe --convert ...``。
    由 server.main() 拦截 --convert，运行 dds.cli_main 后退出（不启动服务）。
    """
    if getattr(sys, "frozen", False):
        return [sys.executable]
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [sys.executable, os.path.join(here, "server.py")]


def _run_conversions(jobs: List[Tuple[str, str]],
                     warnings: List[str], actions: List[str]) -> int:
    """并行把 jobs 里的 (源位图, 目标.dds) 转成 DXT5。

    每张贴图单独一个子进程（纯 Python 编码是单核 CPU 密集，多进程才能在
    多核机器上把 FF777 的多张 4096 贴图压到几十秒）。并发上限 = 核数 - 1，
    留给 HTTP 服务自身一个核。返回成功转换数。
    """
    if not jobs:
        return 0
    cap = max(1, (os.cpu_count() or 2) - 1)
    base = _converter_cmd()
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    pool = []  # [proc, src, dst, started_at]
    errors = []
    n = len(jobs)
    i = 0
    deadline = time.time() + 900  # 单张 4096 最坏约几分钟，超时强制结束
    while i < n or pool:
        while len(pool) < cap and i < n:
            src, dst = jobs[i]
            i += 1
            try:
                proc = subprocess.Popen(
                    base + ["--convert", src, dst],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=create_no_window)
                pool.append([proc, src, dst, time.time()])
            except OSError as e:
                # 子进程起不来 → 回退到本进程内联转换（慢但可用）
                msg = dds.convert_file(src, dst)
                if msg:
                    errors.append((src, dst, f"无法启动转换进程且内联失败：{msg}"))
        time.sleep(0.03)
        for item in list(pool):
            proc = item[0]
            if proc.poll() is None:
                if time.time() - item[3] > 900:
                    proc.kill()
                    errors.append((item[1], item[2], "转换超时（>15 分钟），已终止"))
                    pool.remove(item)
                continue
            out = b"".join(proc.stdout) if proc.stdout else b""
            txt = out.decode("utf-8", "replace").strip()
            if proc.returncode != 0 or not txt.startswith("OK"):
                errors.append((item[1], item[2],
                               txt[len("ERR "):] if txt.startswith("ERR ") else txt or "转换失败"))
            pool.remove(item)
    for src, dst, msg in errors:
        try:
            os.remove(dst)
        except OSError:
            pass
        warnings.append(f"DDS 转换失败：{os.path.basename(src)} → "
                        f"{os.path.relpath(dst)}：{msg}")
    ok = n - len(errors)
    if ok:
        actions.append(f"把 {ok} 张贴图转为 DDS DXT5（图标保持 PNG）")
    return ok


def _place_one(src_abs: str, dest_rel: str, target: str,
               convert_jobs: List[Tuple[str, str]],
               stem_sources: Optional[Dict[str, str]] = None) -> str:
    """按 DDS 规则把一张源文件复制/排队转换到目标目录；返回最终相对路径。

    需要转 DDS 的位图只加入队列（打包末尾统一并行转换），其余立即复制。
    ``stem_sources`` 记录「去扩展名目标路径 → 源文件绝对路径」，供自动生成
    图标时找到原始位图（目标里只有转换后的 .dds）。
    """
    out_rel = _raster_dds_dest(dest_rel)
    dst_abs = os.path.join(target, out_rel)
    if _needs_convert(src_abs, out_rel):
        convert_jobs.append((src_abs, dst_abs))
    else:
        _copy_one(src_abs, dst_abs)
    if stem_sources is not None:
        stem_sources.setdefault(posixpath.splitext(out_rel)[0], src_abs)
    return out_rel


def _dds_to_png_bytes(abs_path: str) -> Optional[bytes]:
    """把目标里的 .dds 解码回 PNG 字节（自动生成图标用；仅 Pillow 支持读 DDS）。"""
    if not pngutil.has_pillow():
        return None
    try:
        import io
        from PIL import Image
        with Image.open(abs_path) as im:
            im = im.convert("RGBA")
            buf = io.BytesIO()
            im.save(buf, "PNG")
            return buf.getvalue()
    except Exception:  # noqa: BLE001
        return None


def _profile_writes_config(profile: dict) -> bool:
    """档案是否会在打包时自动写出配置文件（用于空源组装判断）。"""
    profile = profile or {}
    if not profile.get("config", {}).get("preferred"):
        return False
    return any(f.get("target") == "config" for f in (profile.get("fields", []) or []))


def _source_or_empty(source: Optional[str]) -> SourceInfo:
    """source 为空（None / ""）时返回空 SourceInfo，用于“从零新建涂装”。"""
    if not source:
        return SourceInfo("")
    return scan_source(source)


def package(profile: dict, source: Optional[str] = None,
            values: Optional[Dict[str, str]] = None,
            output_base: str = "", overwrite: bool = False,
            texture_overrides: Optional[Dict[str, str]] = None,
            icon_source: Optional[str] = None) -> dict:
    """执行打包。返回结构化报告。

    ``source`` 可为空（None / ""），表示“从零新建涂装”：不读取任何源文件夹，
    内容全部由手动指定的部位贴图、图标以及自动生成的配置文件组装而成。
    ``texture_overrides``：{目标相对路径(如 objects/fuselage.png): 源文件绝对路径}
    用户手动指定的「部位贴图」，会被复制到机模认识的目标名字/位置。
    ``icon_source``：用户手动选定的一张图片（绝对路径），将用它生成涂装大图标
    （如 ``a320_icon11.png``）与缩略图。
    """
    t0 = time.time()
    if not profile:
        profile = {}
    values = {k: ("" if v is None else str(v)) for k, v in (values or {}).items()}
    texture_overrides = {k: v for k, v in (texture_overrides or {}).items() if v}
    info = _source_or_empty(source)

    # 没有内容时，只要有「手动指定部位贴图 / 手动图标 / 可自动生成的配置」，
    # 仍可从零组装出一套完整涂装；否则没有任何可产出内容，直接报错。
    empty_source = not info.files
    if empty_source:
        has_manual = bool(texture_overrides) or bool(icon_source)
        if not has_manual and not _profile_writes_config(profile):
            if source:
                raise LiveryError(
                    "该文件夹里没有可用的涂装文件，且该机模档案没有任何可自动生成的内容"
                    "（无配置文件可写，也未选择部位贴图/图标）。\n"
                    "请在源文件夹放入涂装文件，或在本页为「部位贴图 / 图标」选择图片后重试。"
                )
            raise LiveryError(
                "尚未选择任何「部位贴图 / 图标」，且该机模档案不自动生成配置文件，"
                "因此没有可打包的内容。\n"
                "请先为某个部位或涂装图标选择一张图片，再点击打包。"
            )

    name = build_livery_name(profile, values)
    target = os.path.abspath(os.path.join(output_base, name))
    if os.path.exists(target):
        if not overwrite:
            raise LiveryError(f"输出目录已存在：{name}（已勾选覆盖则自动替换）")
        shutil.rmtree(target)
    os.makedirs(target, exist_ok=True)

    actions: List[str] = []
    warnings: List[str] = []
    if empty_source and source:
        warnings.append("该文件夹中没有可用涂装文件：将从你手动指定的部位贴图 / 图标"
                        "与自动生成的配置组装出整套涂装。")

    cfg_pref = profile.get("config", {}).get("preferred", "") or ""
    existing_cfg = find_existing_config(info, profile)
    cfg_entries: Optional[List] = None
    if existing_cfg:
        cfg_entries = tlscfg.parse_file(existing_cfg)

    cfg_routed = False
    copied = 0

    # 先处理「手动指定的部位贴图」：复制到机模认识的目标路径，并记下被占用的源文件
    consumed: set = set()
    mapped_dests: set = set()
    convert_jobs: List[Tuple[str, str]] = []
    stem_sources: Dict[str, str] = {}
    for raw_tgt, raw_src in (texture_overrides or {}).items():
        if not raw_src:
            continue
        tgt = _normalize_rel(raw_tgt)
        src = os.path.abspath(raw_src)
        if not os.path.isfile(src):
            raise LiveryError(f"指定贴图文件不存在：{raw_src}")
        out_rel = _place_one(src, tgt, target, convert_jobs, stem_sources)
        copied += 1
        mapped_dests.add(out_rel)
        actions.append(f"按指定：{os.path.basename(src)} → {out_rel}"
                       + ("（转 DDS）" if _needs_convert(src, out_rel) else ""))
        # 若该文件本就位于源内容根内，其原位置不再重复拷贝（重定向到目标路径）
        try:
            rel = os.path.relpath(src, info.content_root)
        except ValueError:
            rel = ""
        if rel and rel != "." and rel != os.pardir and \
                not rel.startswith(".." + os.sep):
            consumed.add(rel.replace("\\", "/"))

    for fi in info.files:
        if fi.rel_posix in consumed:
            continue
        base = os.path.basename(fi.rel_posix)
        rp = fi.rel_posix
        if _is_config_name(base):
            if not cfg_routed and cfg_pref:
                dest = cfg_pref
                cfg_routed = True
            else:
                # 多余的配置文件按普通文件保留在 objects 下
                dest = rp if rp.startswith("objects/") else "objects/" + rp
        elif _is_icon_name(base):
            dest = base  # 图标一律放涂装根目录
        elif rp.startswith("objects/"):
            dest = rp
        else:
            dest = "objects/" + rp
        # 若此文件的目标位置已被「手动指定贴图」占用，跳过以免覆盖指定内容
        if _raster_dds_dest(dest) in mapped_dests:
            continue
        _place_one(fi.abs_path, dest, target, convert_jobs, stem_sources)
        copied += 1
    if copied:
        actions.append(f"复制 {copied} 个文件")
    elif empty_source and source:
        actions.append("该文件夹中没有源文件可复制：内容由手动指定 / 自动生成组装")

    # 统一并行执行 DDS 转换（PNG/BMP → DXT5；图标保持 PNG）
    _run_conversions(convert_jobs, warnings, actions)

    # 图标处理
    icon_report = _handle_icons(profile, info, target, warnings, actions,
                                icon_source=icon_source,
                                stem_sources=stem_sources)

    # 配置文件写回
    config_report = _write_config(profile, values, cfg_entries, target, warnings, actions)

    return {
        "ok": True,
        "target": target,
        "name": name,
        "filesCopied": copied,
        "icons": icon_report,
        "config": config_report,
        "textureMap": {"count": len(mapped_dests)},
        "warnings": warnings,
        "actions": actions,
        "elapsedMs": int((time.time() - t0) * 1000),
    }


def _write_user_icon(icon_cfg: dict, prefix_hint: str, src_abs: str,
                     icon_size, thumb_size, target: str,
                     warnings: List[str], actions: List[str]) -> Optional[dict]:
    """把用户手动选定的一张图片写成涂装大图标与缩略图。失败返回 None。"""
    big_name = f"{prefix_hint}_icon11.png"
    thumb_name = f"{prefix_hint}_icon11_thumb.png"
    try:
        with open(src_abs, "rb") as fh:
            data = fh.read()
    except OSError as e:
        warnings.append(f"读取所选图标失败：{e}")
        return None
    if not data:
        warnings.append("所选图标文件为空，已忽略")
        return None

    is_png = data.startswith(b"\x89PNG\r\n\x1a\n")
    if is_png:
        big_out = data  # PNG 原样保留，避免二次压缩
    else:
        # 非 PNG：需 Pillow 打开并转成图标尺寸的 PNG（含透明留白，与自动生成一致）
        if not pngutil.has_pillow():
            warnings.append("所选图标不是 PNG，且本机未安装 Pillow，无法自动转换。"
                            "请改选 PNG 图片，或执行 `pip install pillow` 后重试")
            return None
        big_out = pngutil.resize_with_preferred(data, icon_size[0], icon_size[1])
        if big_out is None:
            warnings.append("所选图标无法被读取/转换，请改选常见图片（png / jpg 等）")
            return None

    thumb_out: Optional[bytes] = None
    try:
        if is_png and not pngutil.has_pillow():
            # 无 Pillow 时纯 Python 缩放有像素上限：超大图只写大图标（X-Plane 会自动补缩略图）
            try:
                w, h = pngutil.load_size(big_out)
            except Exception:
                w, h = 0, 0
            if (w or 0) * (h or 0) > _MAX_PURE_GEN_PIXELS:
                warnings.append("所选 PNG 尺寸过大，未生成缩略图"
                                "（可改选较小图片或 `pip install pillow`）")
            else:
                thumb_out = pngutil.resize_with_preferred(
                    big_out, thumb_size[0], thumb_size[1])
        else:
            thumb_out = pngutil.resize_with_preferred(
                big_out, thumb_size[0], thumb_size[1])
    except Exception as e:  # noqa: BLE001
        warnings.append(f"生成缩略图失败：{e}")

    try:
        with open(os.path.join(target, big_name), "wb") as fh:
            fh.write(big_out)
    except OSError as e:
        warnings.append(f"写入图标失败：{e}")
        return None
    actions.append(f"使用所选图片作为大图标 {big_name}")
    if thumb_out:
        try:
            with open(os.path.join(target, thumb_name), "wb") as fh:
                fh.write(thumb_out)
            actions.append(f"由所选图标生成缩略图 {thumb_name}")
        except OSError as e:
            warnings.append(f"写入缩略图失败：{e}")
    return {"big": big_name, "thumb": thumb_name if thumb_out else None,
            "existing": [], "userIcon": True}


def _handle_icons(profile: dict, info: SourceInfo, target: str,
                  warnings: List[str], actions: List[str],
                  icon_source: Optional[str] = None,
                  stem_sources: Optional[Dict[str, str]] = None) -> dict:
    icon_cfg = profile.get("icons", {}) or {}
    thumb_size = tuple(icon_cfg.get("thumbSize", [174, 107]))
    icon_size = tuple(icon_cfg.get("iconSize", [800, 450]))
    prefix_hint = icon_cfg.get("prefixHint", "livery")

    # 用户手动选了一张图片作图标 → 优先使用它
    if icon_source:
        report = _write_user_icon(icon_cfg, prefix_hint, icon_source,
                                  icon_size, thumb_size, target, warnings, actions)
        if report:
            return report

    big: Optional[str] = None   # 目标根目录下的大图标文件名
    thumb: Optional[str] = None
    for name in sorted(os.listdir(target)):
        if not _is_icon_name(name):
            continue
        p = os.path.join(target, name)
        if "_thumb" in name.lower():
            thumb = name
        elif big is None:
            big = name

    # 有图标但缺缩略图 → 由大图生成
    if big and not thumb:
        thumb_name = big[:-len(".png")] + "_thumb.png"
        try:
            with open(os.path.join(target, big), "rb") as fh:
                data = fh.read()
            out = pngutil.resize_with_preferred(data, thumb_size[0], thumb_size[1])
            if out:
                with open(os.path.join(target, thumb_name), "wb") as fh:
                    fh.write(out)
                thumb = thumb_name
                actions.append(f"由大图标生成缩略图 {thumb_name}")
            else:
                warnings.append("无法生成缩略图（PNG 解码失败）")
        except OSError as e:
            warnings.append(f"生成缩略图失败：{e}")

    if big is None:
        # 完全没图标：尝试从某张贴图生成
        gen = _try_generate_icons(info, icon_cfg, prefix_hint, target,
                                  warnings, actions, stem_sources)
        return {"big": gen[0] if gen else None, "thumb": gen[1] if gen else None,
                "existing": [n for n in (big, thumb) if n]}
    return {"big": big, "thumb": thumb, "existing": []}


def _try_generate_icons(info: SourceInfo, icon_cfg: dict, prefix_hint: str,
                        target: str, warnings: List[str], actions: List[str],
                        stem_sources: Optional[Dict[str, str]] = None):
    icon_size = tuple(icon_cfg.get("iconSize", [800, 450]))
    thumb_size = tuple(icon_cfg.get("thumbSize", [174, 107]))
    src_rel = icon_cfg.get("iconSource", "") or ""
    if not src_rel:
        warnings.append("涂装缺少 *_icon11.png 大图标（可选，不影响加载）")
        return None
    # 找生成图标要用的原始位图，依次尝试：
    # 1) 源目录里的原图（objects/fuselage.png）；2) 打包时手动选图记录
    # （stem_sources：目标去扩展名 → 用户源文件）；3) 目标里的 .dds（需 Pillow 解码）。
    stem = posixpath.splitext(_normalize_rel(src_rel))[0]
    src_abs = os.path.join(info.content_root, src_rel.replace("/", os.sep))
    if not os.path.isfile(src_abs) and stem_sources and stem in stem_sources:
        src_abs = stem_sources[stem]
    data = None
    if os.path.isfile(src_abs):
        if src_abs.lower().endswith(".dds"):
            # 源本身就是 .dds（例如再次打包已转好的涂装）→ 先解码成 PNG
            data = _dds_to_png_bytes(src_abs)
        else:
            try:
                with open(src_abs, "rb") as fh:
                    raw = fh.read()
            except OSError:
                raw = b""
            data = raw if raw and not raw.startswith(b"DDS ") \
                else (_dds_to_png_bytes(src_abs) if raw else None)
    if not data:
        # 源图不可得 → 尝试解码目标里已转换好的 .dds（仅 Pillow 支持 DDS 读取）
        dds_abs = os.path.join(target, _raster_dds_dest(src_rel).replace("/", os.sep))
        if os.path.isfile(dds_abs):
            data = _dds_to_png_bytes(dds_abs)
    if not data:
        warnings.append(f"缺少图标且未找到贴图 {src_rel}，无法自动生成")
        return None
    try:
        w, h = pngutil.load_size(data)
    except Exception:
        warnings.append("缺少图标且贴图无法读取，无法自动生成")
        return None
    use_pillow = pngutil.has_pillow()
    if not use_pillow and (w or 0) * (h or 0) > _MAX_PURE_GEN_PIXELS:
        warnings.append(f"缺少图标，且 {src_rel} 为 {w}x{h}，无 Pillow 时过大不自动生成。"
                        "请放置 *_icon11.png，或 `pip install pillow`")
        return None
    if not use_pillow:
        actions.append("未安装 Pillow，图标由内置纯 Python 引擎生成（较慢），建议 pip install pillow")

    big_name = f"{prefix_hint}_icon11.png"
    thumb_name = f"{prefix_hint}_icon11_thumb.png"
    try:
        big_out = pngutil.resize_with_preferred(data, icon_size[0], icon_size[1])
        thumb_out = pngutil.resize_with_preferred(data, thumb_size[0], thumb_size[1])
    except Exception as e:  # noqa: BLE001
        warnings.append(f"自动生成图标失败：{e}")
        return None
    if not big_out or not thumb_out:
        warnings.append("自动生成图标失败（解码错误）")
        return None
    with open(os.path.join(target, big_name), "wb") as fh:
        fh.write(big_out)
    with open(os.path.join(target, thumb_name), "wb") as fh:
        fh.write(thumb_out)
    actions.append(f"由贴图 {src_rel} 自动生成图标 {big_name} / {thumb_name}")
    return (big_name, thumb_name)


def _write_config(profile: dict, values: Dict[str, str],
                  cfg_entries: Optional[List], target: str,
                  warnings: List[str], actions: List[str]) -> dict:
    cfg = profile.get("config", {}) or {}
    pref = cfg.get("preferred", "") or ""
    fields = profile.get("fields", [])
    declared = declared_config_keys(profile)
    metas = meta_keys(profile)
    if not pref and not declared:
        return {"path": None, "created": False, "reason": "该档案无需配置文件"}

    entries = list(cfg_entries) if cfg_entries else []
    # 依次写入声明字段（保持档案中的顺序与默认）
    for f in fields:
        if f.get("target") != "config":
            continue
        key = f.get("key")
        val = values.get(key)
        if val is None:
            val = f.get("default", "")
        val = _serialize_field_value(f, val)
        if val == "" and not isinstance(val, bool):
            continue
        tlscfg.set_kv(entries, key, val)
    # 追加来源文件中未知、且被用户编辑的键（保留）
    for key, val in values.items():
        if key in metas or key in declared or val in (None, ""):
            continue
        tlscfg.set_kv(entries, key, val)

    if not entries:
        return {"path": None, "created": False, "reason": "无配置内容"}

    dest = os.path.abspath(os.path.join(target, pref)) if pref else None
    if dest:
        text = tlscfg.serialize(entries)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(text)
        if cfg_entries:
            actions.append(f"更新配置 {pref}（{len([e for e in entries if isinstance(e, tlscfg.KV)])} 个键）")
        else:
            actions.append(f"新建配置 {pref}")
        return {"path": pref, "created": not bool(cfg_entries), "keys": len(entries)}
    warnings.append("档案声明了配置字段但未指定写入路径")
    return {"path": None, "created": False, "reason": "未指定配置路径"}


# ---------------------------------------------------------------- 扫描预览
def _icon_setup(profile: dict, info: SourceInfo) -> Optional[dict]:
    """档案图标自动生成信息，供前端渲染「图标」选择行。"""
    icon_cfg = (profile or {}).get("icons", {}) or {}
    if not icon_cfg:
        return None
    prefix = icon_cfg.get("prefixHint", "livery") or "livery"
    src_rel = (icon_cfg.get("iconSource") or "").replace("\\", "/").lstrip("/")
    present = list_icons(info)
    return {
        "prefix": prefix,
        "bigName": f"{prefix}_icon11.png",
        "thumbName": f"{prefix}_icon11_thumb.png",
        "iconSize": icon_cfg.get("iconSize", [800, 450]),
        "thumbSize": icon_cfg.get("thumbSize", [174, 107]),
        "autoSource": src_rel,
        "autoPossible": bool(src_rel) and any(
            fi.rel_posix.lower() == src_rel.lower() for fi in info.files),
        "sourceHasIcon": bool(present),
        "sourceIconNames": [i["name"] for i in present],
    }


def scan_payload(profile: dict, source: Optional[str] = None) -> dict:
    """为前端生成「档案骨架 / 扫描预览」。

    ``source`` 为空时表示“从零新建”：不扫描任何文件夹，仅按档案声明返回字段默认值、
    部位贴图清单与图标信息（无 source 相关字段、无警告）。
    """
    info = _source_or_empty(source)
    warnings: List[str] = []
    profile = profile or {}
    fields = profile.get("fields", [])
    declared = declared_config_keys(profile)
    metas = meta_keys(profile)

    existing_cfg_path = find_existing_config(info, profile)
    existing_values: Dict[str, str] = {}
    if existing_cfg_path:
        existing_values = tlscfg.to_dict(tlscfg.parse_file(existing_cfg_path))

    filled = []
    for f in fields:
        key = f.get("key")
        value = existing_values.get(key, f.get("default", ""))
        item = dict(f)
        item["value"] = "" if value is None else str(value)
        item["source"] = "existing" if key in existing_values else "default"
        filled.append(item)

    extras = []
    if existing_cfg_path:
        for key, val in existing_values.items():
            if key not in declared and key not in metas:
                extras.append({
                    "key": key,
                    "label": key,
                    "type": "text",
                    "target": "config",
                    "value": val,
                    "source": "existing-unknown",
                    "group": "其他键（源文件自动读取，可修改）",
                })

    icons = list_icons(info)
    counts = {
        "files": len(info.files),
        "textures": sum(1 for f in info.files if _is_texture_name(os.path.basename(f.rel_posix))),
        "icons": len(icons),
        "configs": 1 if existing_cfg_path else 0,
    }
    if not info.files and source:
        warnings.append("该文件夹为空：可在第 3 步为「部位贴图 / 图标」选择图片，"
                        "程序将从零组装整套涂装（必要时自动生成配置文件）。")

    # 部位贴图：档案声明的、机模按文件名识别的贴图（用户可手动指定）
    tp = profile.get("textureParts", {}) or {}
    tex_parts = []
    for pt in tp.get("parts", []) or []:
        if not pt.get("target"):
            continue
        target = _normalize_rel(pt.get("target", ""))
        # 目标写的是 .dds，而源文件夹里同名位图可能仍是 .png → 忽略扩展名比较
        target_stem = posixpath.splitext(target)[0].lower()
        in_place = any(
            posixpath.splitext(fi.rel_posix)[0].lower() == target_stem
            for fi in info.files
        )
        tex_parts.append({
            "key": pt.get("key", ""),
            "label": pt.get("label", target),
            "target": target,
            "help": pt.get("help", ""),
            "inPlace": in_place,
        })
    source_textures = [
        fi.rel_posix for fi in info.files
        if _is_texture_name(os.path.basename(fi.rel_posix))
        and not _is_icon_name(os.path.basename(fi.rel_posix))
        and not _is_config_name(os.path.basename(fi.rel_posix))
    ]

    defaults = {}
    for it in filled:
        defaults[it["key"]] = it["value"]
    for it in extras:
        defaults[it["key"]] = it["value"]

    return {
        "ok": True,
        "contentRoot": info.content_root,
        "rootName": info.root_name,
        "profile": {
            "id": profile.get("id", ""),
            "name": profile.get("name", ""),
            "vendor": profile.get("vendor", ""),
            "aircraft": profile.get("aircraft", ""),
            "description": profile.get("description", ""),
            "installHint": profile.get("installHint", ""),
            "textureDir": profile.get("textureDir", "objects"),
            "hasConfig": bool(profile.get("config", {}).get("preferred")),
            "naming": profile.get("naming", {}),
        },
        "fields": filled + extras,
        "existingConfigPath": existing_cfg_path,
        "icons": icons,
        "iconSetup": _icon_setup(profile, info),
        "counts": counts,
        "textureParts": {
            "note": tp.get("note", ""),
            "parts": tex_parts,
        },
        "sourceTextures": source_textures,
        "warnings": warnings,
        "namePreview": build_livery_name(profile, defaults),
    }
