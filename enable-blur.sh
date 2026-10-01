#!/usr/bin/env bash
# 开启 / 关闭贴纸的背景模糊（模糊 + 圆角）。
#
# 用法：
#   bash enable-blur.sh            # 开启（需先安装 GNOME Rounded Blur 库并重新登录）
#   bash enable-blur.sh --disable  # 关闭（回到干净的半透明圆角样式）
#   bash enable-blur.sh --force    # 未检测到库时也强行开启（会出现直角毛边，不推荐）
set -euo pipefail

SCHEMA="org.gnome.shell.extensions.blur-my-shell"
SCHEMA_DIR="$HOME/.local/share/gnome-shell/extensions/blur-my-shell@aunetx/schemas"

if [ ! -d "$SCHEMA_DIR" ]; then
    echo "错误：找不到 blur-my-shell 扩展（$SCHEMA_DIR）" >&2
    exit 1
fi
export GSETTINGS_SCHEMA_DIR="$SCHEMA_DIR"

if [ "${1:-}" = "--disable" ]; then
    gsettings set "$SCHEMA.applications" whitelist "[]"
    echo "已关闭贴纸的背景模糊（保持干净的圆角样式）。"
    exit 0
fi

# 检查 GNOME Rounded Blur 库是否已安装（gi://Blur）
if ! python3 - <<'PY'
import gi
try:
    gi.require_version("Blur", "1.0")
    from gi.repository import Blur  # noqa: F401
except Exception:
    raise SystemExit(1)
PY
then
    echo "警告：本机未检测到 GNOME Rounded Blur 库（gi://Blur）。" >&2
    echo "请先运行 tools/gnome-rounded-blur/rounded_blur_build.sh -i 安装，然后注销/重新登录。" >&2
    echo "（如确认要在没有圆角支持的情况下开启模糊，请加 --force）" >&2
    [ "${1:-}" != "--force" ] && exit 1
fi

# 圆角半径 = 卡片圆角（style.css 的 border-radius，16）+ 窗口留白（main.py 的 WINDOW_MARGIN，9）
# 让模糊轮廓与卡片圆角同心、边缘宽度均匀。修改上述两项后请同步调整这里的值。
gsettings set "$SCHEMA.applications" corner-radius 25
gsettings set "$SCHEMA.applications" whitelist "['*SysStickers*','*sysstickers*']"

echo "已开启：贴纸背景模糊 + 圆角（25px，与卡片同心）。"
echo "提示：如果是刚安装完库，需要先注销/重新登录一次，扩展才能加载到它。"
