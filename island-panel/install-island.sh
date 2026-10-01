#!/usr/bin/env bash
# 安装 / 启用「灵动岛」GNOME Shell 扩展（uuid 沿用 lyrics-panel@loong）
#
#   bash install-island.sh          # 安装并启用
#   bash install-island.sh --remove # 卸载
#
# 关于重载：GNOME 45+ 每个 Shell 进程只会 import 一次扩展模块，
#   - 第一次安装（或扩展入口 extension.js 变更后）：需要注销重新登录一次；
#   - 之后的日常修改：扩展内置了开发加载器，disable/enable 即可生效，
#     stylesheet.css 也会在每次启用时重新读取。
set -euo pipefail

UUID="lyrics-panel@loong"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="$HOME/.local/share/gnome-shell/extensions/$UUID"
PROJECT_DIR="$(dirname "$SRC_DIR")"

if [ "${1:-}" = "--remove" ]; then
    if command -v gnome-extensions >/dev/null 2>&1; then
        gnome-extensions disable "$UUID" >/dev/null 2>&1 || true
    fi
    rm -rf "$DEST_DIR"
    echo "已卸载 $UUID（系统时钟会在注销/重登或停用后自动恢复）。"
    exit 0
fi

mkdir -p "$DEST_DIR"

# 1) 扩展本体
cp -f "$SRC_DIR/metadata.json" "$SRC_DIR/stylesheet.css" \
      "$SRC_DIR/extension.js" "$SRC_DIR/island.js" "$SRC_DIR/media.js" "$SRC_DIR/usage.js" \
      "$DEST_DIR/"

# 2) 数据帮手：复用项目里的 Python 模块（API 用量 / 余额），并记录项目路径
cp -f "$PROJECT_DIR/api_usage.py" "$PROJECT_DIR/settings.py" "$DEST_DIR/"
printf '%s\n' "$PROJECT_DIR" > "$DEST_DIR/project-path"

# 3) 启用（写入 GSettings，下次登录自动加载）
if command -v gnome-extensions >/dev/null 2>&1; then
    gnome-extensions enable "$UUID" >/dev/null 2>&1 || true
    echo "--- 扩展状态 ---"
    gnome-extensions info "$UUID" 2>/dev/null | sed -n '1,4p' || true
fi

echo
echo "完成。第一次安装需要注销并重新登录一次，灵动岛才会出现在顶栏中间。"
echo "之后再改代码：gnome-extensions disable/enable $UUID 即可热重载。"
