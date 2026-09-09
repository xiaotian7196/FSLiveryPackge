"""Pure-Python DDS writer (BC3 / DXT5) plus RGBA image loader.

X-Plane 12 reads .dds DXT5 textures. Pillow on this stack can only *write*
uncompressed DDS, so a real DXT5/BC3 compressor is implemented here in pure
Python to keep the packaged EXE self-contained (no numpy / external tools).

All functions are intentionally dependency-light: image decoding prefers
Pillow when available and falls back to the pure-Python PNG decoder from
packer.pngutil otherwise.
"""

import os
import struct
import sys
import tempfile

from . import pngutil

try:
    from PIL import Image as _PILImage
except Exception:  # pragma: no cover - environment dependent
    _PILImage = None


# ---------------------------------------------------------------------------
# Source image -> RGBA bytes
# ---------------------------------------------------------------------------

def load_rgba(path):
    """Return (width, height, rgba_bytes) for an image file.

    Pillow is used when installed (covers PNG/BMP/JPEG/...). Without Pillow
    only PNG files can be decoded (pure Python). Returns None when the file
    cannot be decoded.
    """
    if _PILImage is not None:
        try:
            with _PILImage.open(path) as im:
                im = im.convert("RGBA")
                w, h = im.size
                rgba = im.tobytes()
                return w, h, rgba
        except Exception:
            return None
    # Pillow unavailable -> pure-Python PNG path only.
    try:
        data = pngutil.decode_png(path)
    except Exception:
        return None
    if data is None:
        return None
    w, h, rows = data
    rgba = bytearray()
    for row in rows:
        n = len(row)
        for i in range(0, n - 2, 3):
            rgba.append(row[i])
            rgba.append(row[i + 1])
            rgba.append(row[i + 2])
            rgba.append(255)
    return w, h, bytes(rgba)


# ---------------------------------------------------------------------------
# BC3 (DXT5) block encoder
# ---------------------------------------------------------------------------

def _c565(r, g, b):
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def _encode_alpha_block(rs, gs, bs, as_, alpha_byte):
    """Encode 16 alphas (3-bit 8-value mode) into alpha_byte[0:8]."""
    amin = 255
    amax = 0
    for a in as_:
        if a > amax:
            amax = a
        if a < amin:
            amin = a
    alpha_byte[0] = amax
    alpha_byte[1] = amin
    if amax == amin:
        # All 16 alphas identical: every index resolves to that value.
        alpha_byte[2:8] = b"\x00" * 6
        return alpha_byte
    # Decoder uses 8-value interpolation between the endpoints.
    codes = [amax, amin]
    codes.extend(((8 - i) * amax + (i - 1) * amin) // 7 for i in range(2, 8))
    bits = 0
    for p in range(16):
        a = as_[p]
        best = 0
        bd = 257
        for i in range(8):
            d = a - codes[i]
            if d < 0:
                d = -d
            if d < bd:
                bd = d
                best = i
        bits |= best << (3 * p)
    alpha_byte[2:8] = bits.to_bytes(6, "little")
    return alpha_byte


def _encode_color_block(rs, gs, bs, as_, out):
    """Encode BC1 color endpoints/indices for one block into out[0:8]."""
    # Colour is meaningless for fully transparent pixels; output zeros.
    any_vis = False
    for a in as_:
        if a > 0:
            any_vis = True
            break
    if not any_vis:
        out[0:8] = b"\x00" * 8
        return out
    # gather visible pixels
    vr = []
    vg = []
    vb = []
    n = 0
    for p in range(16):
        if as_[p] > 0:
            vr.append(rs[p])
            vg.append(gs[p])
            vb.append(bs[p])
            n += 1
    # Single-colour (flat) block fast path.
    if all(v == vr[0] for v in vr) and all(v == vg[0] for v in vg) and all(
        v == vb[0] for v in vb
    ):
        e = _c565(vr[0], vg[0], vb[0])
        if e == 0:
            e = 1
        out[0:8] = struct.pack("<HHI", e, e - 1, 0)
        return out
    mr = sum(vr) // n
    mg = sum(vg) // n
    mb = sum(vb) // n

    def dist2(r1, g1, b1, r2, g2, b2):
        dr = r1 - r2
        dg = g1 - g2
        db = b1 - b2
        return dr * dr + dg * dg + db * db

    # Seed the two clusters by the two most separated extremes.
    c0r = mr
    c0g = mg
    c0b = mb
    c1r = mr
    c1g = mg
    c1b = mb
    bd = -1
    for p in range(n):
        d = dist2(vr[p], vg[p], vb[p], mr, mg, mb)
        if d > bd:
            bd = d
            c0r, c0g, c0b = vr[p], vg[p], vb[p]
    bd = -1
    for p in range(n):
        d = dist2(vr[p], vg[p], vb[p], c0r, c0g, c0b)
        if d > bd:
            bd = d
            c1r, c1g, c1b = vr[p], vg[p], vb[p]
    # One Lloyd pass refines the two cluster means.
    s0r = s0g = s0b = 0
    s1r = s1g = s1b = 0
    n0 = 0
    n1 = 0
    for p in range(n):
        if dist2(vr[p], vg[p], vb[p], c0r, c0g, c0b) <= dist2(
            vr[p], vg[p], vb[p], c1r, c1g, c1b
        ):
            s0r += vr[p]
            s0g += vg[p]
            s0b += vb[p]
            n0 += 1
        else:
            s1r += vr[p]
            s1g += vg[p]
            s1b += vb[p]
            n1 += 1
    if n0:
        c0r, c0g, c0b = s0r // n0, s0g // n0, s0b // n0
    if n1:
        c1r, c1g, c1b = s1r // n1, s1g // n1, s1b // n1
    e0 = _c565(c0r, c0g, c0b)
    e1 = _c565(c1r, c1g, c1b)
    # BC3 has no 1-bit-alpha mode: the colour block must be strictly
    # ordered (color0 > color1) or decoders fall back to that mode and
    # index 3 becomes transparent. Swap or nudge the endpoints to enforce it.
    if e0 <= e1:
        if e0 < e1:
            e0, e1 = e1, e0
        elif e0 > 0:
            e1 = e0 - 1
        else:
            e0, e1 = 1, 0
    # Rebuild the 4-colour palette from 565 endpoints (what the decoder sees).
    p0r, p0g, p0b = _from565(e0)
    p1r, p1g, p1b = _from565(e1)
    # 4-colour mode (2 endpoints interpolated at 1/3 and 2/3).
    c2r = (2 * p0r + p1r) // 3
    c2g = (2 * p0g + p1g) // 3
    c2b = (2 * p0b + p1b) // 3
    c3r = (p0r + 2 * p1r) // 3
    c3g = (p0g + 2 * p1g) // 3
    c3b = (p0b + 2 * p1b) // 3
    pr = (p0r, p1r, c2r, c3r)
    pg = (p0g, p1g, c2g, c3g)
    pb = (p0b, p1b, c2b, c3b)
    bits = 0
    for p in range(16):
        r = rs[p]
        g = gs[p]
        b = bs[p]
        best = 0
        bd = 196608
        # Unrolled palette distance for speed.
        for i in range(4):
            dr = r - pr[i]
            dg = g - pg[i]
            db = b - pb[i]
            d = dr * dr + dg * dg + db * db
            if d < bd:
                bd = d
                best = i
        bits |= best << (2 * p)
    out[0:8] = struct.pack("<HHI", e0, e1, bits)
    return out


def _from565(v):
    r = (v >> 11) & 0x1F
    g = (v >> 5) & 0x3F
    b = v & 0x1F
    return (r << 3) | (r >> 2), (g << 2) | (g >> 1), (b << 3) | (b >> 2)


def rgba_to_dds(width, height, rgba):
    """Encode RGBA8 pixels into a DXT5 (BC3) DDS file (no mipmaps).

    width/height need not be multiples of four (edges are clamped).
    Returns the complete .dds file contents as bytes.
    """
    bw = (width + 3) // 4
    bh = (height + 3) // 4
    data = bytearray(128 + bw * bh * 16)
    stride = width * 4
    block = bytearray(16)
    alpha8 = bytearray(8)
    rs = [0] * 16
    gs = [0] * 16
    bs = [0] * 16
    as_ = [0] * 16
    block = bytearray(16)
    alpha8 = bytearray(8)
    color8 = bytearray(8)
    idx = 0
    for by in range(bh):
        y0 = by * 4
        for bx in range(bw):
            x0 = bx * 4
            p = 0
            for yy in range(4):
                y = y0 + yy
                row_off = y * stride if y < height else -1
                for xx in range(4):
                    x = x0 + xx
                    if row_off >= 0 and x < width:
                        o = row_off + x * 4
                        rs[p] = rgba[o]
                        gs[p] = rgba[o + 1]
                        bs[p] = rgba[o + 2]
                        as_[p] = rgba[o + 3]
                    else:
                        rs[p] = 0
                        gs[p] = 0
                        bs[p] = 0
                        as_[p] = 0
                    p += 1
            _encode_alpha_block(rs, gs, bs, as_, alpha8)
            _encode_color_block(rs, gs, bs, as_, color8)
            block[0:8] = alpha8
            block[8:16] = color8
            data[idx:idx + 16] = block
            idx += 16
    # Assemble the DDS header.
    header = bytearray(128)
    header[0:4] = b"DDS "
    struct.pack_into("<I", header, 4, 124)          # dwSize
    struct.pack_into("<I", header, 8, 0x1007)       # DDSD_CAPS|HEIGHT|WIDTH|PIXELFORMAT|PITCH
    struct.pack_into("<I", header, 12, height)      # dwHeight
    struct.pack_into("<I", header, 16, width)       # dwWidth
    struct.pack_into("<I", header, 20, bw * bh * 16)  # dwPitchOrLinearSize
    struct.pack_into("<I", header, 24, 0)           # dwDepth
    struct.pack_into("<I", header, 28, 0)           # dwMipMapCount
    # reserved[11] (bytes 32..75) left zero
    struct.pack_into("<I", header, 76, 32)          # pf dwSize
    struct.pack_into("<I", header, 80, 0x4)         # DDPF_FOURCC
    header[84:88] = b"DXT5"
    struct.pack_into("<I", header, 88, 0)           # pf RGBBitCount
    struct.pack_into("<I", header, 92, 0)           # pf RBitMask
    struct.pack_into("<I", header, 96, 0)           # pf GBitMask
    struct.pack_into("<I", header, 100, 0)          # pf BBitMask
    struct.pack_into("<I", header, 104, 0)          # pf ABitMask
    struct.pack_into("<I", header, 108, 0x1000)     # dwCaps (DDSCAPS_TEXTURE)
    # remaining caps fields zero
    return bytes(header) + bytes(data)


# ---------------------------------------------------------------------------
# Whole-file helpers + subprocess worker entry point
# ---------------------------------------------------------------------------

def convert_file(src, dst):
    """Convert a raster image (PNG/BMP/JPEG/...) into a DXT5 .dds file.

    src  : absolute path of the source image (PNG/BMP/JPEG/TGA/... or .dds).
    dst  : absolute path of the .dds file to create (parent dirs auto-made).
    Returns None on success or a human readable error message on failure.
    Existing ``dst`` is replaced atomically; nothing is written on failure.
    """
    try:
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        if src.lower().endswith(".dds"):
            # Already compressed: plain copy is the correct result.
            if os.path.abspath(src) != os.path.abspath(dst):
                with open(src, "rb") as fh:
                    blob = fh.read()
                _atomic_write(dst, blob)
            return None
        loaded = load_rgba(src)
        if loaded is None:
            return "无法解码图片（格式不支持或文件损坏）"
        w, h, rgba = loaded
        blob = rgba_to_dds(w, h, rgba)
        _atomic_write(dst, blob)
        return None
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"


def _atomic_write(dst, blob):
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(dst) + ".", suffix=".tmp",
                               dir=os.path.dirname(dst) or ".")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def cli_main(argv):
    """Subprocess worker: convert argv[0]=src argv[1]=dst.

    Prints an ASCII ``OK`` line on success or ``ERR <message>`` on failure and
    returns the process exit code. Used by engine.package() to run conversions
    in parallel subprocesses (safe in the frozen PyInstaller EXE).
    """
    if len(argv) < 2:
        sys.stdout.write("ERR 参数不足\n")
        sys.stdout.flush()
        return 2
    src, dst = argv[0], argv[1]
    msg = convert_file(src, dst)
    if msg is None:
        sys.stdout.write("OK\n")
        sys.stdout.flush()
        return 0
    try:
        sys.stdout.write("ERR " + msg + "\n")
    except UnicodeEncodeError:
        sys.stdout.write("ERR <转换失败>\n")
    sys.stdout.flush()
    return 1
