#!/usr/bin/env python3
"""通过 GitHub API 下载 gnome-rounded-blur 源码。

用途：本机网络对 github.com 的 git 克隆会被重置（GnuTLS recv error），
本脚本用 api.github.com（可正常访问）按 git blob 逐文件重建仓库源码，
供 rounded_blur_build.sh 在克隆失败时兜底使用。

用法: python3 fetch_rounded_blur_src.py [目标目录]
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request

REPO = "kancko/gnome-rounded-blur"
REF = "master"


def api(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def main() -> int:
    dest = sys.argv[1] if len(sys.argv) > 1 else "gnome-rounded-blur"

    tree = api(f"https://api.github.com/repos/{REPO}/git/trees/{REF}?recursive=1")
    blobs = [item for item in tree.get("tree", []) if item.get("type") == "blob"]
    if not blobs:
        print("错误：API 返回的文件列表为空", file=sys.stderr)
        return 1

    os.makedirs(dest, exist_ok=True)
    for item in blobs:
        blob = api(f"https://api.github.com/repos/{REPO}/git/blobs/{item['sha']}")
        data = base64.b64decode(blob["content"])
        path = os.path.join(dest, item["path"])
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        print(f"  {item['path']} ({len(data)} 字节)")

    print(f"已通过 GitHub API 下载 {len(blobs)} 个文件到 {dest}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
