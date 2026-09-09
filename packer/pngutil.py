"""纯 Python 的 PNG 解码 / 缩放 / 编码工具。

用途：在未安装 Pillow 的环境里，仍可从机模贴图（PNG）自动生成 X-Plane
涂装图标（800x450）与缩略图（174x107）。若环境装有 Pillow，engine 会
优先使用 Pillow，本模块仅作为兜底。

支持的输入：8/16 位深、色彩类型 0(灰度)/2(RGB)/3(调色板)/6(RGBA)。
输出：8 位 RGBA 的 PNG。
"""
from __future__ import annotations

import struct
import zlib
from typing import List, Optional, Tuple

_HEADER = b"\x89PNG\r\n\x1a\n"

COLOR_GRAY = 0
COLOR_RGB = 2
COLOR_PALETTE = 3
COLOR_GRAY_ALPHA = 4
COLOR_RGBA = 6


class PngError(Exception):
    pass


def _chunk_type(data: bytes, off: int) -> Tuple[str, int, int]:
    """返回 (类型名, 数据起点, 数据终点)。"""
    length = struct.unpack(">I", data[off:off + 4])[0]
    ctype = data[off + 4:off + 8].decode("latin-1")
    start = off + 8
    end = start + length
    return ctype, start, end


def decode_png(data: bytes) -> Tuple[int, int, List[bytes]]:
    """解码 PNG 为 (宽, 高, 每行像素 bytes 列表)。输出 RGBA8 行。"""
    if not data.startswith(_HEADER):
        raise PngError("不是合法的 PNG 文件")
    pos = 8
    width = height = bitdepth = colortype = 0
    idat = bytearray()
    palette: List[Tuple[int, int, int]] = []
    trns: Optional[bytes] = None
    compression = filter_m = interlace = 0
    saw_ihdr = False
    saw_iend = False
    while pos < len(data):
        ctype, start, end = _chunk_type(data, pos)
        body = data[start:end]
        if ctype == "IHDR":
            if len(body) < 13:
                raise PngError("IHDR 损坏")
            width, height, bitdepth = struct.unpack(">IIB", body[:9])
            colortype, compression, filter_m, interlace = body[9:13]
            saw_ihdr = True
        elif ctype == "IDAT":
            idat += body
        elif ctype == "PLTE":
            if len(body) % 3:
                raise PngError("PLTE 损坏")
            palette = [tuple(body[i:i + 3]) for i in range(0, len(body), 3)]
        elif ctype == "tRNS":
            trns = body
        elif ctype == "IEND":
            saw_iend = True
        pos = end + 4  # 跳过 CRC
    if not saw_ihdr:
        raise PngError("缺少 IHDR")
    if not saw_iend:
        # 很多截图工具缺失 IEND 前的收尾也常见，宽容处理
        pass
    if interlace != 0:
        raise PngError("不支持隔行(Adam7) PNG")

    raw = zlib.decompress(bytes(idat))
    return _unfilter(width, height, bitdepth, colortype, raw, palette, trns)


def _channels_for(colortype: int) -> int:
    if colortype == COLOR_RGBA:
        return 4
    if colortype == COLOR_RGB:
        return 3
    if colortype == COLOR_PALETTE:
        return 1
    if colortype == COLOR_GRAY_ALPHA:
        return 2
    if colortype == COLOR_GRAY:
        return 1
    raise PngError(f"不支持的色彩类型 {colortype}")


def _unfilter(width, height, bitdepth, colortype, raw, palette, trns):
    channels = _channels_for(colortype)
    bpp = max(1, channels * bitdepth // 8)
    stride = (width * channels * bitdepth + 7) // 8
    rows_bytes: List[bytes] = []
    prev = bytearray(stride)
    off = 0
    for _ in range(height):
        if off >= len(raw):
            raise PngError("PNG 数据不完整")
        ftype = raw[off]
        off += 1
        row = bytearray(raw[off:off + stride])
        off += stride
        if len(row) != stride:
            raise PngError("PNG 行数据不完整")
        _unfilter_row(ftype, row, prev, bpp)
        prev = row
        rows_bytes.append(bytes(row))
    # 转换为 RGBA8
    out = _to_rgba8(rows_bytes, width, height, bitdepth, colortype, palette, trns)
    return width, height, out


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter_row(ftype: int, row: bytearray, prev: bytearray, bpp: int) -> None:
    n = len(row)
    if ftype == 0:
        return
    if ftype == 1:  # Sub
        for i in range(bpp, n):
            row[i] = (row[i] + row[i - bpp]) & 0xFF
    elif ftype == 2:  # Up
        for i in range(n):
            row[i] = (row[i] + prev[i]) & 0xFF
    elif ftype == 3:  # Average
        for i in range(n):
            left = row[i - bpp] if i >= bpp else 0
            row[i] = (row[i] + ((left + prev[i]) >> 1)) & 0xFF
    elif ftype == 4:  # Paeth
        for i in range(n):
            left = row[i - bpp] if i >= bpp else 0
            up_left = prev[i - bpp] if i >= bpp else 0
            row[i] = (row[i] + _paeth(left, prev[i], up_left)) & 0xFF
    else:
        raise PngError(f"未知滤波类型 {ftype}")


def _scale_down_8(row_in: bytes, row_out: bytearray, factor: int) -> None:
    """8 位单通道 box 均值下采样（factor 为原采样间隔）。"""
    n_out = len(row_out)
    acc = 0
    cnt = 0
    for oi in range(n_out):
        acc = 0
        cnt = 0
        base = oi * factor
        for di in range(factor):
            if base + di < len(row_in):
                acc += row_in[base + di]
                cnt += 1
        row_out[oi] = acc // cnt if cnt else 0


def _to_rgba8(rows, width, height, bitdepth, colortype, palette, trns):
    def sample_byte(b: int) -> int:
        return b  # 8-bit

    out_rows: List[bytes] = []
    if bitdepth == 16:
        # 合并成 8 位
        rr = []
        for row in rows:
            nr = bytearray(len(row) // 2)
            for i in range(len(nr)):
                nr[i] = row[i * 2]
            rr.append(bytes(nr))
        rows = rr
        bitdepth = 8

    if colortype == COLOR_RGBA:
        out_rows = [bytes(row) for row in rows]
    elif colortype == COLOR_RGB:
        for row in rows:
            nr = bytearray(width * 4)
            for i in range(width):
                nr[i * 4] = row[i * 3]
                nr[i * 4 + 1] = row[i * 3 + 1]
                nr[i * 4 + 2] = row[i * 3 + 2]
                nr[i * 4 + 3] = 255
            out_rows.append(bytes(nr))
    elif colortype == COLOR_GRAY_ALPHA:
        for row in rows:
            nr = bytearray(width * 4)
            for i in range(width):
                nr[i * 4] = nr[i * 4 + 1] = nr[i * 4 + 2] = row[i * 2]
                nr[i * 4 + 3] = row[i * 2 + 1]
            out_rows.append(bytes(nr))
    elif colortype == COLOR_GRAY:
        for row in rows:
            nr = bytearray(width * 4)
            for i in range(width):
                nr[i * 4] = nr[i * 4 + 1] = nr[i * 4 + 2] = row[i]
                nr[i * 4 + 3] = 255
            out_rows.append(bytes(nr))
    elif colortype == COLOR_PALETTE:
        alpha_map = {}
        if trns and len(trns) <= len(palette):
            alpha_map = {i: trns[i] for i in range(len(trns))}
        for row in rows:
            nr = bytearray(width * 4)
            for i in range(width):
                idx = row[i]
                r, g, b = palette[idx] if idx < len(palette) else (0, 0, 0)
                a = alpha_map.get(idx, 255)
                nr[i * 4] = r
                nr[i * 4 + 1] = g
                nr[i * 4 + 2] = b
                nr[i * 4 + 3] = a
            out_rows.append(bytes(nr))
    else:
        raise PngError(f"不支持的色彩类型 {colortype}")
    return out_rows


def encode_png(width: int, height: int, rows_rgba8: List[bytes]) -> bytes:
    """把 RGBA8 行编码为标准 PNG。"""
    stride = width * 4
    raw = bytearray()
    prev = bytearray(stride)
    for row in rows_rgba8:
        if len(row) != stride:
            raise PngError("编码时行宽不一致")
        # 使用 Sub 滤波(1) 或 None(0)——选 Sub 通常压缩更好
        filtered = bytearray(stride + 1)
        filtered[0] = 1
        for i in range(stride):
            left = row[i - 4] if i >= 4 else 0
            filtered[i + 1] = (row[i] - left) & 0xFF
        raw += filtered
        prev = row  # noqa 仅用于引用保持
    ihdr = struct.pack(">IIBBBBB", width, height, 8, COLOR_RGBA, 0, 0, 0)
    idat = zlib.compress(bytes(raw), 6)

    def chunk(ctype: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + ctype + payload + \
            struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF)

    return _HEADER + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _fit_scale(src_w: int, src_h: int, dst_w: int, dst_h: int) -> Tuple[int, int]:
    """返回保持宽高比放缩后（<= 目标）的实际像素尺寸。"""
    if src_w <= 0 or src_h <= 0:
        raise PngError("源图像尺寸非法")
    ratio = min(dst_w / src_w, dst_h / src_h)
    if ratio >= 1:
        return src_w, src_h  # 不放大
    return max(1, int(src_w * ratio)), max(1, int(src_h * ratio))


def resize_png(data: bytes, dst_w: int, dst_h: int, pad=True) -> bytes:
    """把 PNG 等比缩放并（可选）居中补边到目标尺寸。

    大于目标则 box 均值缩小；小于等于目标则直接居中拷贝（不放大）。
    pad=False 时不补边，返回贴合后的原始尺寸。
    """
    sw, sh, rows = decode_png(data)
    fw, fh = _fit_scale(sw, sh, dst_w, dst_h)

    def box_down(w: int, h: int, src_rows: List[bytes], tw: int, th: int) -> List[bytes]:
        if tw == w and th == h:
            return src_rows
        xs = w / tw
        ys = h / th
        out: List[bytes] = []
        for oy in range(th):
            y0 = int(oy * ys)
            y1 = max(y0 + 1, int((oy + 1) * ys))
            nr = bytearray(tw * 4)
            for ox in range(tw):
                x0 = int(ox * xs)
                x1 = max(x0 + 1, int((ox + 1) * xs))
                acc = [0, 0, 0, 0]
                n = 0
                for yy in range(y0, y1):
                    if yy >= h:
                        break
                    row = src_rows[yy]
                    for xx in range(x0, x1):
                        if xx >= w:
                            break
                        p = xx * 4
                        acc[0] += row[p]
                        acc[1] += row[p + 1]
                        acc[2] += row[p + 2]
                        acc[3] += row[p + 3]
                        n += 1
                if n:
                    nr[ox * 4] = acc[0] // n
                    nr[ox * 4 + 1] = acc[1] // n
                    nr[ox * 4 + 2] = acc[2] // n
                    nr[ox * 4 + 3] = acc[3] // n
            out.append(bytes(nr))
        return out

    fitted = box_down(sw, sh, rows, fw, fh)
    if not pad:
        return encode_png(fw, fh, fitted)

    canvas = [bytearray(dst_w * 4) for _ in range(dst_h)]
    ox = (dst_w - fw) // 2
    oy = (dst_h - fh) // 2
    for y in range(fh):
        src = fitted[y]
        row = canvas[oy + y]
        for x in range(fw):
            row[(ox + x) * 4: (ox + x) * 4 + 4] = src[x * 4: x * 4 + 4]
    return encode_png(dst_w, dst_h, [bytes(r) for r in canvas])


def load_size(data: bytes) -> Tuple[int, int]:
    if not data.startswith(_HEADER) or len(data) < 24:
        raise PngError("不是合法的 PNG 文件")
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def _pil_available() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except Exception:
        return False


def has_pillow() -> bool:
    return _pil_available()


def resize_with_preferred(data: bytes, dst_w: int, dst_h: int) -> Optional[bytes]:
    """优先用 Pillow，否则用纯 Python 实现。失败返回 None。"""
    try:
        import PIL.Image as Image  # type: ignore
        from PIL import ImageOps  # type: ignore
        img = Image.open(__import__("io").BytesIO(data))
        if img.mode not in ("RGBA", "RGB", "LA", "L"):
            img = img.convert("RGBA")
        img = ImageOps.contain(img, (dst_w, dst_h), Image.LANCZOS)
        canvas = Image.new("RGBA", (dst_w, dst_h), (0, 0, 0, 0))
        canvas.paste(img, ((dst_w - img.width) // 2, (dst_h - img.height) // 2))
        buf = __import__("io").BytesIO()
        canvas.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        pass
    try:
        return resize_png(data, dst_w, dst_h, pad=True)
    except Exception:
        return None
