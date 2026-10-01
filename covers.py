"""专辑封面：解析来源、下载缓存、生成顶栏图标位图。

- 优先用播放器提供的 mpris:artUrl（file:// / http(s):// / data: 都支持）
- Firefox 不提供 artUrl 时，用网易云搜索结果里的封面（lyrics.fetch_track 一并返回）
- 缓存目录：~/.cache/sysstickers/covers/
- 顶栏图标：SNI 的 IconPixmap 要求 A,R,G,B 字节序（预乘），且不能为空，
  所以没有封面时用程序生成的「圆角占位图 + 音符」。

可单独测试：python3 covers.py "夜航星" "不才"
"""

from __future__ import annotations

import base64
import hashlib
import os
import urllib.parse
import urllib.request

import lyrics

_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "sysstickers", "covers")
_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) sysstickers/1.0"
_COVER_SIZE = 160              # 下载尺寸（卡片 72、顶栏 48 都够用）
_ICON_SIZES = (16, 24, 32, 48)  # 顶栏图标候选尺寸（2x 屏会挑 32）
_ICON_RADIUS = 0.22            # 图标圆角比例

try:
    from PIL import Image, ImageChops, ImageDraw
    _HAS_PIL = True
except ImportError:  # 没有 Pillow 时降级为 GdkPixbuf（无圆角）
    _HAS_PIL = False


# ---------------------------------------------------------------- 下载与缓存

def _cache_path(url: str) -> str:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return os.path.join(_CACHE_DIR, digest + ".img")


def download_cover(url: str | None) -> str | None:
    """把封面下载/复制到本地缓存，返回文件路径；失败返回 None。"""
    if not url:
        return None
    path = _cache_path(url)
    if os.path.exists(path):
        return path
    try:
        data = _read_source(url)
        if not data:
            return None
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(path + ".tmp", "wb") as fh:
            fh.write(data)
        os.replace(path + ".tmp", path)
        return path
    except Exception:
        return None


def _read_source(url: str) -> bytes | None:
    if url.startswith("data:"):
        header, _, payload = url.partition(",")
        if "base64" in header:
            return base64.b64decode(payload)
        return urllib.parse.unquote_to_bytes(payload)
    if url.startswith("file://"):
        from gi.repository import GLib

        path = GLib.filename_from_uri(url)[0]
        with open(path, "rb") as fh:
            return fh.read()
    # 网易云图床支持 ?param=WxH 取缩略图，省流量
    if "music.126.net" in url and "?" not in url:
        url = f"{url}?param={_COVER_SIZE}y{_COVER_SIZE}"
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read()


def cover_for(title: str, artist: str, album: str = "", art_url: str | None = None) -> str | None:
    """后台线程里用：解析并下载封面，返回本地文件路径。"""
    if art_url:
        path = download_cover(art_url)
        if path:
            return path
    track = lyrics.fetch_track(title, artist, album) or {}
    return download_cover(track.get("cover"))


def load_track(title: str, artist: str, album: str = "", art_url: str | None = None):
    """后台线程里用：一次拿到 (歌词行列表, 本地封面路径)。"""
    track = lyrics.fetch_track(title, artist, album) or {}
    cover = download_cover(art_url) if art_url else None
    if cover is None:
        cover = download_cover(track.get("cover"))
    return track.get("lines"), cover


# ---------------------------------------------------------------- 顶栏图标

def icon_pixmaps(cover_path: str | None):
    """生成 SNI IconPixmap [(w, h, 预乘 ARGB 字节)]；保证非空。"""
    if _HAS_PIL:
        try:
            image = Image.open(cover_path).convert("RGBA") if cover_path else _placeholder(_COVER_SIZE)
            return [_pixmap(image, size) for size in _ICON_SIZES]
        except Exception:
            pass
    return _pixmaps_via_pixbuf(cover_path)


def _placeholder(size: int):
    """没有封面时的占位图：半透明圆角方块 + 音符。"""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=int(size * _ICON_RADIUS),
                           fill=(255, 255, 255, 44))
    # 音符：符头 + 符干 + 符尾
    head_r = size * 0.13
    cx, cy = size * 0.42, size * 0.68
    draw.ellipse((cx - head_r, cy - head_r * 0.75, cx + head_r, cy + head_r * 0.75),
                 fill=(255, 255, 255, 240))
    stem = size * 0.055
    x0 = cx + head_r - stem / 2
    draw.rectangle((x0, size * 0.26, x0 + stem, cy - head_r * 0.2),
                   fill=(255, 255, 255, 240))
    draw.polygon([(x0 + stem, size * 0.26), (x0 + stem + size * 0.2, size * 0.33),
                  (x0 + stem, size * 0.46)], fill=(255, 255, 255, 240))
    return image


def _pixmap(image, size: int) -> tuple:
    """缩放 + 圆角 + 转成预乘 ARGB（A,R,G,B 字节序）。"""
    scaled = image.resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1),
                                           radius=int(size * _ICON_RADIUS), fill=255)
    alpha = ImageChops.multiply(scaled.getchannel("A"), mask)
    scaled.putalpha(alpha)

    pixels = scaled.tobytes()  # RGBA
    out = bytearray(len(pixels))
    for i in range(0, len(pixels), 4):
        r, g, b, a = pixels[i], pixels[i + 1], pixels[i + 2], pixels[i + 3]
        if a != 255:
            r = r * a // 255
            g = g * a // 255
            b = b * a // 255
        out[i] = a
        out[i + 1] = r
        out[i + 2] = g
        out[i + 3] = b
    return (size, size, bytes(out))


def _pixmaps_via_pixbuf(cover_path: str | None):
    """无 Pillow 时的降级：GdkPixbuf 缩放（不做圆角）；仍保证非空。"""
    try:
        from gi.repository import GdkPixbuf
    except Exception:
        return None
    try:
        if cover_path:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(cover_path)
        else:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(
                "/usr/share/icons/Adwaita/symbolic/mimetypes/audio-x-generic-symbolic.svg")
    except Exception:
        return None
    results = []
    for size in _ICON_SIZES:
        scaled = pixbuf.scale_simple(size, size, GdkPixbuf.InterpType.BILINEAR)
        width, height = scaled.get_width(), scaled.get_height()
        stride = scaled.get_rowstride()
        raw = scaled.get_pixels()
        out = bytearray(width * height * 4)
        for y in range(height):
            for x in range(width):
                index = y * stride + x * 4
                r, g, b, a = raw[index], raw[index + 1], raw[index + 2], raw[index + 3]
                if a != 255:
                    r = r * a // 255
                    g = g * a // 255
                    b = b * a // 255
                target = (y * width + x) * 4
                out[target] = a
                out[target + 1] = r
                out[target + 2] = g
                out[target + 3] = b
        results.append((width, height, bytes(out)))
    return results or None


if __name__ == "__main__":  # 自测：python3 covers.py "夜航星" "不才"
    import sys

    title = sys.argv[1] if len(sys.argv) > 1 else "夜航星"
    artist = sys.argv[2] if len(sys.argv) > 2 else "不才"
    path = cover_for(title, artist)
    print("封面文件:", path)
    pixmaps = icon_pixmaps(path)
    print("位图:", [(w, h, len(data)) for w, h, data in pixmaps] if pixmaps else None)
    if path:
        target = "/tmp/opencode/cover_preview.png"
        os.makedirs("/tmp/opencode", exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(open(path, "rb").read())
        print("已复制预览 ->", target)
