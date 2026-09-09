"""liveryAutoPackge 本地 Web 服务。

- 托管 ``web/`` 前端；
- 提供 JSON API：档案列表、目录浏览、原生文件夹选择、档案骨架、
  打包执行（支持从零新建，不依赖源涂装文件夹）。

只监听 127.0.0.1，请勿暴露到公网。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import string
import subprocess
import sys
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from packer import __version__
from packer import engine
from packer import profiles as profiles_mod

FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    # PyInstaller 单文件模式：只读资源在临时解包目录，可写输出放到 exe 所在目录
    RES_ROOT = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    RES_ROOT = os.path.dirname(os.path.abspath(__file__))
    BASE_DIR = RES_ROOT

WEB_DIR = os.path.join(RES_ROOT, "web")
PROFILES_DIR = os.path.join(RES_ROOT, "profiles")
OUTPUT_DIR = os.path.join(BASE_DIR, "打包样式")
_DEFAULT_PORT = 8655
_MAX_LIST_ITEMS = 600

PROFILE_CACHE: list = []
_PROFILE_STAMP: float = 0.0


def _profiles_stamp() -> float:
    stamp = 0.0
    try:
        for name in os.listdir(PROFILES_DIR):
            if name.lower().endswith(".json"):
                stamp = max(stamp, os.path.getmtime(os.path.join(PROFILES_DIR, name)))
    except OSError:
        pass
    return stamp


def profiles_ready() -> list:
    """档案文件变动后自动重载，改 JSON 不必重启服务即可生效。"""
    global PROFILE_CACHE, _PROFILE_STAMP
    stamp = _profiles_stamp()
    if not PROFILE_CACHE or stamp != _PROFILE_STAMP:
        PROFILE_CACHE = profiles_mod.load_profiles(PROFILES_DIR)
        _PROFILE_STAMP = stamp
    return PROFILE_CACHE


def load_profiles() -> list:
    return profiles_ready()


def _get_profile(pid: str) -> dict:
    p = profiles_mod.get_profile(PROFILE_CACHE, pid)
    if p is None:
        return profiles_mod.get_profile(PROFILE_CACHE, "generic_xplane") or {}
    return p


def _port_pids(port: int) -> list:
    """返回当前监听该端口的所有进程 PID（Windows 上可能同时有多个）。"""
    try:
        out = subprocess.check_output(["netstat", "-ano"], text=True,
                                      errors="replace", timeout=10)
    except Exception:
        return []
    want = f":{port}"
    pids = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING":
            if parts[1].endswith(want):
                try:
                    pids.append(int(parts[4]))
                except ValueError:
                    pass
    return pids


def _is_our_instance(port: int) -> bool:
    """端口上是否已是本工具的服务实例（以此决定能否安全重启）。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/info", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("app") == "liveryAutoPackge"
    except Exception:
        return False


def _kill_pid(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       check=False, capture_output=True)
    except OSError:
        pass


def _start_server(host: str, port: int, attempts: int = 10):
    """确保端口上只有本工具的最新实例在服务。

    Windows 的 SO_REUSEADDR 会让两个实例静默并存，因此不能靠“绑定失败”
    去发现旧实例：这里改为先探测端口，若被本工具旧实例占用就先关掉它、
    等端口释放后再独占绑定；被其它程序占用则直接报错（不误杀）。
    """
    for _ in range(attempts):
        pids = _port_pids(port)
        if pids:
            if not _is_our_instance(port):
                raise OSError(
                    f"端口 {port} 已被其它程序占用。\n"
                    f"请先关闭占用该端口的程序，或用 --port 指定其它端口。"
                )
            for pid in pids:
                print(f"[提示] 检测到旧版 liveryAutoPackge 服务 (PID {pid}) "
                      f"仍占用端口 {port}，")
                print("       自动关闭它，并以最新代码/档案重启…")
                _kill_pid(pid)
            time.sleep(0.6)
            continue
        try:
            return ThreadingHTTPServer((host, port), Handler)
        except OSError:
            time.sleep(0.6)
    raise OSError(f"端口 {port} 被占用且无法自动重启")


def _profile_summaries() -> list:
    return [
        {"id": p.get("id"), "name": p.get("name"), "vendor": p.get("vendor"),
         "aircraft": p.get("aircraft"),
         "fallback": profiles_mod.is_fallback(p)}
        for p in PROFILE_CACHE
    ]


def _as_json(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")


def _pick_start_dir() -> str:
    for cand in (
        os.path.join(os.path.expanduser("~"), "Desktop"),
        os.path.join(os.path.expanduser("~"), "Downloads"),
        OUTPUT_DIR,
        BASE_DIR,
        os.path.expanduser("~"),
    ):
        if os.path.isdir(cand):
            return cand
    return BASE_DIR


# ------------------------------------------------------------------ 原生对话框
def _ensure_tcl_env() -> None:
    """冻结 exe 时，若 PyInstaller 未自动定位 tcl/tk 数据目录，则手动指定到打包进来的目录。"""
    if not FROZEN:
        return
    meipass = getattr(sys, "_MEIPASS", "") or ""
    for var, sub in (("TCL_LIBRARY", "tcl9.0"), ("TK_LIBRARY", "tk9.0")):
        if os.environ.get(var):
            continue
        cand = os.path.join(meipass, sub)
        if os.path.isdir(cand):
            os.environ[var] = cand


def _tk_folder_dialog(title: str) -> str:
    """在当前进程主线程里弹原生文件夹选择框（仅用于冻结 exe 的子进程调用）。"""
    _ensure_tcl_env()
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askdirectory(title=title)
    root.destroy()
    return path or ""


def _tk_file_dialog(title: str, start_dir: str = "") -> str:
    """在当前进程主线程里弹原生「选择图片文件」对话框。"""
    _ensure_tcl_env()
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    kwargs = {"title": title}
    if start_dir and os.path.isdir(start_dir):
        kwargs["initialdir"] = start_dir
    path = filedialog.askopenfilename(
        **kwargs,
        filetypes=[("图片文件", "*.png *.dds *.tga *.bmp *.jpg *.jpeg"),
                   ("所有文件", "*.*")],
    )
    root.destroy()
    return path or ""


_FOLDER_DIALOG_SCRIPT = (
    "import sys,tkinter as tk; from tkinter import filedialog;"
    "r=tk.Tk(); r.withdraw(); r.attributes('-topmost', True);"
    "p=filedialog.askdirectory(title=sys.argv[1]); print(p or '')"
)
_FILE_DIALOG_SCRIPT = (
    "import sys,tkinter as tk; from tkinter import filedialog;"
    "r=tk.Tk(); r.withdraw(); r.attributes('-topmost', True);"
    "p=filedialog.askopenfilename(title=sys.argv[1],initialdir=sys.argv[2],"
    "filetypes=[('图片文件','*.png *.dds *.tga *.bmp *.jpg *.jpeg'),"
    "('所有文件','*.*')]); print(p or '')"
)


def _native_folder_dialog(title: str) -> dict:
    """弹原生文件夹选择框。dev：独立 python 子进程；冻结 exe：自身以 --native-folder-dialog 再跑一次。"""
    if FROZEN:
        cmd = [os.path.abspath(sys.executable), "--native-folder-dialog", title]
    else:
        cmd = [sys.executable, "-c", _FOLDER_DIALOG_SCRIPT, title]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return {"cancelled": True}
    except OSError as e:
        return {"error": f"无法启动文件夹选择：{e}"}
    if proc.returncode != 0:
        return {"cancelled": True}
    path = proc.stdout.strip()
    if not path:
        return {"cancelled": True}
    return {"path": path}


def _native_file_dialog(title: str, start_dir: str = "") -> dict:
    """弹原生「选择图片文件」对话框（可到任意位置）。"""
    if not start_dir or not os.path.isdir(start_dir):
        start_dir = _pick_start_dir()
    if FROZEN:
        cmd = [os.path.abspath(sys.executable),
               "--native-file-dialog", start_dir, title]
    else:
        cmd = [sys.executable, "-c", _FILE_DIALOG_SCRIPT, title, start_dir]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return {"cancelled": True}
    except OSError as e:
        return {"error": f"无法启动文件选择：{e}"}
    if proc.returncode != 0:
        return {"cancelled": True}
    path = proc.stdout.strip()
    if not path:
        return {"cancelled": True}
    return {"path": path}


def _list_dir(path: str) -> dict:
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise FileNotFoundError(path)
    entries = []
    try:
        items = sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError as e:
        raise FileNotFoundError(str(e))
    for it in items[: _MAX_LIST_ITEMS]:
        try:
            is_dir = it.is_dir()
        except OSError:
            continue
        try:
            size = "" if is_dir else it.stat().st_size
        except OSError:
            size = ""
        entries.append({"name": it.name, "dir": is_dir, "size": size})
    parent = os.path.dirname(path)
    return {
        "path": path,
        "parent": parent if parent != path else None,
        "atRoot": parent == path,
        "entries": entries,
    }


def _drives() -> list:
    out = []
    for letter in string.ascii_uppercase:
        d = f"{letter}:\\"
        if os.path.isdir(d):
            out.append({"name": f"{letter}:", "path": d})
    return out


# ------------------------------------------------------------------ 请求处理
class Handler(BaseHTTPRequestHandler):
    server_version = "liveryAutoPackge/" + __version__

    def log_message(self, fmt, *args):  # 精简日志
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # ---------- helpers
    def _send(self, status: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, status: int = 200):
        self._send(status, _as_json(obj))

    def _send_error(self, status: int, message: str):
        self._send_json({"error": message}, status)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        data = self.rfile.read(length)
        try:
            obj = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return obj if isinstance(obj, dict) else {}

    # ---------- routes
    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        try:
            if path == "/api/info":
                profiles_ready()
                self._send_json({
                    "app": "liveryAutoPackge",
                    "version": __version__,
                    "outputDir": OUTPUT_DIR,
                    "outputExists": os.path.isdir(OUTPUT_DIR),
                    "projectRoot": BASE_DIR,
                    "browseStart": _pick_start_dir(),
                    "profiles": _profile_summaries(),
                    "profileCount": len(PROFILE_CACHE),
                    "python": sys.version.split()[0],
                })
            elif path == "/api/profiles":
                profiles_ready()
                self._send_json(_profile_summaries())
            elif path == "/api/list":
                p = query.get("path", [""])[0] or _pick_start_dir()
                if p == "::drives":
                    self._send_json({"drives": _drives(), "atRoot": True})
                else:
                    self._send_json(_list_dir(p))
            elif path.startswith("/api/"):
                self._send_error(404, f"未知接口 {path}")
            else:
                self._serve_static(path)
        except FileNotFoundError:
            self._send_error(404, "目录或文件不存在")
        except PermissionError:
            self._send_error(403, "没有权限访问该路径")
        except Exception as e:  # noqa: BLE001
            self._send_error(500, f"服务端错误：{e}")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/scan":
                self._api_scan()
            elif path == "/api/detect":
                self._api_detect()
            elif path == "/api/package":
                self._api_package()
            elif path == "/api/browse-dialog":
                body = self._read_json()
                self._send_json(_native_folder_dialog(body.get("title", "选择文件夹")))
            elif path == "/api/browse-file":
                body = self._read_json()
                self._send_json(_native_file_dialog(
                    body.get("title", "选择贴图图片"),
                    body.get("startDir", ""),
                ))
            else:
                self._send_error(404, f"未知接口 {path}")
        except engine.LiveryError as e:
            self._send_error(400, str(e))
        except Exception as e:  # noqa: BLE001
            self._send_error(500, f"服务端错误：{e}")

    # ---------- api 实现
    def _api_scan(self):
        profiles_ready()
        body = self._read_json()
        # 无源文件夹：只按档案 id 返回“从零新建”骨架
        src = (body.get("path") or body.get("source") or "").strip()
        pid = body.get("profileId")
        if pid:
            profile = _get_profile(pid)
        elif src:
            info = engine.scan_source(os.path.abspath(src))
            profile = profiles_mod.pick_profile(PROFILE_CACHE, info.inventory)
        else:
            raise engine.LiveryError("缺少机模档案（profileId）")
        payload = engine.scan_payload(profile, src or None)
        payload["profileId"] = profile.get("id")
        self._send_json(payload)

    def _api_detect(self):
        profiles_ready()
        body = self._read_json()
        src = os.path.abspath(body.get("path") or "")
        info = engine.scan_source(src)
        profile = profiles_mod.pick_profile(PROFILE_CACHE, info.inventory)
        self._send_json({
            "profileId": profile.get("id"),
            "name": profile.get("name"),
            "vendor": profile.get("vendor"),
            "aircraft": profile.get("aircraft"),
            "fallback": profiles_mod.is_fallback(profile),
            "contentRoot": info.content_root,
        })

    def _api_package(self):
        profiles_ready()
        body = self._read_json()
        src = (body.get("path") or body.get("source") or "").strip() or None
        pid = body.get("profileId")
        if not pid:
            raise engine.LiveryError("缺少机模档案")
        profile = _get_profile(pid)
        out = os.path.abspath(body.get("outputDir") or OUTPUT_DIR)
        values = body.get("values") or {}
        overwrite = bool(body.get("overwrite"))
        if not os.path.isdir(out):
            try:
                os.makedirs(out, exist_ok=True)
            except OSError as e:
                raise engine.LiveryError(f"无法创建输出目录 {out}：{e}")
        report = engine.package(profile, src, values, out, overwrite=overwrite,
                                texture_overrides=body.get("textureMap"),
                                icon_source=body.get("iconPath"))
        self._send_json(report)

    # ---------- 静态文件
    def _serve_static(self, url_path: str):
        if url_path in ("/", ""):
            url_path = "/index.html"
        rel = url_path.lstrip("/")
        full = os.path.realpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(os.path.realpath(WEB_DIR)) or not os.path.isfile(full):
            self._send_error(404, "页面不存在")
            return
        ext = os.path.splitext(full)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
            ".json": "application/json; charset=utf-8",
        }.get(ext, "application/octet-stream")
        with open(full, "rb") as fh:
            data = fh.read()
        self._send(200, data, ctype)


def main():
    global OUTPUT_DIR

    # 子进程转换：打包时按「一张贴图一个进程」并行转 DDS（冻结 exe 内同样有效）。
    if "--convert" in sys.argv:
        i = sys.argv.index("--convert")
        from packer import dds as dds_worker
        sys.exit(dds_worker.cli_main(sys.argv[i + 1:i + 3]))

    # 自检：验证冻结 exe 内 tkinter 可正常工作（不弹窗），用于打包后的自动验证。
    if "--selftest-tk" in sys.argv:
        _ensure_tcl_env()
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        print("TK_OK", tkinter.TkVersion)
        root.destroy()
        sys.exit(0)

    # 冻结 exe 弹原生对话框时以子进程方式再次运行本程序（主线程里跑 Tk）。
    if "--native-folder-dialog" in sys.argv or "--native-file-dialog" in sys.argv:
        if "--native-folder-dialog" in sys.argv:
            i = sys.argv.index("--native-folder-dialog")
            title = sys.argv[i + 1] if i + 1 < len(sys.argv) else "选择文件夹"
            path = _tk_folder_dialog(title)
        else:
            i = sys.argv.index("--native-file-dialog")
            start = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
            title = sys.argv[i + 2] if i + 2 < len(sys.argv) else "选择图片文件"
            path = _tk_file_dialog(title, start)
        print(path or "")
        sys.exit(0)

    parser = argparse.ArgumentParser(description="liveryAutoPackge 本地服务")
    parser.add_argument("--port", type=int, default=_DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--output", default=OUTPUT_DIR,
                        help="打包输出目录（默认项目下 打包样式 文件夹）")
    args = parser.parse_args()

    if args.output:
        OUTPUT_DIR = os.path.abspath(args.output)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    load_profiles()

    server = _start_server(args.host, args.port)
    url = f"http://{args.host}:{args.port}/"
    print("=" * 58)
    print("  liveryAutoPackge  涂装自动打包工具")
    print(f"  打开浏览器: {url}")
    print(f"  输出目录:   {OUTPUT_DIR}")
    print(f"  机模档案:   {len(PROFILE_CACHE)} 个 ({PROFILES_DIR})")
    print("  关闭本窗口即可退出服务。")
    print("=" * 58)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
