#!/usr/bin/env bash
# 把「系统贴纸」和「顶栏歌词插件」加入开机自启，并创建应用菜单快捷方式。
# 卸载：运行 ./install-autostart.sh --remove
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(command -v python3)"

AUTOSTART_DIR="$HOME/.config/autostart"
APPS_DIR="$HOME/.local/share/applications"
STICKERS_AUTOSTART="$AUTOSTART_DIR/sysstickers.desktop"
STICKERS_APP="$APPS_DIR/sysstickers.desktop"
LYRICS_AUTOSTART="$AUTOSTART_DIR/sysstickers-lyrics.desktop"
LYRICS_APP="$APPS_DIR/sysstickers-lyrics.desktop"

if [[ "${1:-}" == "--remove" ]]; then
    rm -f "$STICKERS_AUTOSTART" "$STICKERS_APP" "$LYRICS_AUTOSTART" "$LYRICS_APP"
    command -v update-desktop-database >/dev/null && update-desktop-database "$APPS_DIR" || true
    echo "已移除开机自启和快捷方式。"
    exit 0
fi

mkdir -p "$AUTOSTART_DIR" "$APPS_DIR"

write_desktop_file() {
    local target="$1" delay="$2"
    cat > "$target" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=系统贴纸
Name[en]=System Stickers
Comment=显示系统状态的桌面毛玻璃贴纸
Comment[en]=Frosted-glass desktop stickers showing system status
Exec=$PYTHON $DIR/main.py
Path=$DIR
Icon=utilities-system-monitor
Terminal=false
StartupNotify=false
StartupWMClass=sysstickers
X-GNOME-Autostart-Delay=$delay
EOF
}

write_lyrics_autostart() {
    cat > "$LYRICS_AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=顶栏歌词
Name[en]=Lyrics Panel
Comment=在顶栏显示当前歌词（沿用上次的开关状态）
Exec=$PYTHON $DIR/lyrics_tray.py --autostart
Path=$DIR
Icon=audio-x-generic
Terminal=false
StartupNotify=false
X-GNOME-Autostart-Delay=6
EOF
}

# 应用菜单里的「顶栏歌词」开关：点击一下开/关切换
write_lyrics_launcher() {
    cat > "$LYRICS_APP" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=顶栏歌词 开/关
Name[en]=Toggle Lyrics Panel
Comment=打开或关闭顶栏歌词（也可以在任意贴纸右键菜单里切换）
Exec=$PYTHON $DIR/lyrics_tray.py --toggle
Path=$DIR
Icon=audio-x-generic
Terminal=false
StartupNotify=false
EOF
}

# 开机自启（延迟几秒，等桌面就绪）
write_desktop_file "$STICKERS_AUTOSTART" 5
write_lyrics_autostart

# 应用菜单快捷方式
write_desktop_file "$STICKERS_APP" ""
write_lyrics_launcher

command -v update-desktop-database >/dev/null && update-desktop-database "$APPS_DIR" || true

echo "完成："
echo "  贴纸自启  -> $STICKERS_AUTOSTART"
echo "  歌词自启  -> $LYRICS_AUTOSTART（沿用上次开关状态）"
echo "  应用菜单  -> $STICKERS_APP / $LYRICS_APP"
echo "立即体验：python3 $DIR/main.py"
