#!/usr/bin/env bash
# 安装 / 启用「歌词顶栏」GNOME Shell 扩展（lyrics-panel@loong）
#
#   bash install-lyrics-panel.sh          # 安装并启用
#   bash install-lyrics-panel.sh --remove # 卸载
set -euo pipefail

UUID="lyrics-panel@loong"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="$HOME/.local/share/gnome-shell/extensions/$UUID"

_shell_reload() {
    busctl --user call org.gnome.Shell.Extensions /org/gnome/Shell/Extensions \
        org.gnome.Shell.Extensions ReloadExtension s "$UUID" >/dev/null 2>&1 || true
}

if [ "${1:-}" = "--remove" ]; then
    if command -v gnome-extensions >/dev/null 2>&1; then
        gnome-extensions disable "$UUID" >/dev/null 2>&1 || true
    fi
    rm -rf "$DEST_DIR"
    _shell_reload
    echo "已卸载 $UUID。"
    exit 0
fi

mkdir -p "$DEST_DIR"
cp -f "$SRC_DIR/metadata.json" "$SRC_DIR/stylesheet.css" "$SRC_DIR/extension.js" "$DEST_DIR/"

# 1) 让正在运行的 Shell 重新扫描并加载（新装扩展必须先 Reload）
_shell_reload

# 2) 写入启用状态（下次登录自动加载）
if command -v gnome-extensions >/dev/null 2>&1; then
    gnome-extensions enable "$UUID" >/dev/null 2>&1 || true
    echo "--- 扩展状态 ---"
    gnome-extensions info "$UUID" 2>/dev/null | sed -n '1,4p' || true
else
    busctl --user call org.gnome.Shell.Extensions /org/gnome/Shell/Extensions \
        org.gnome.Shell.Extensions EnableExtension s "$UUID" >/dev/null 2>&1 || true
fi

echo "完成。若顶栏没有出现歌词，注销并重新登录一次即可。"
