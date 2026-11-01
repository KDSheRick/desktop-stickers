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
CONTROL_APP="$APPS_DIR/sysstickers-control.desktop"
SWITCH_AUTOSTART="$AUTOSTART_DIR/sysstickers-switch.desktop"
SWITCH_APP="$APPS_DIR/sysstickers-switch.desktop"

if [[ "${1:-}" == "--remove" ]]; then
    rm -f "$STICKERS_AUTOSTART" "$STICKERS_APP" "$LYRICS_AUTOSTART" "$LYRICS_APP" \
          "$CONTROL_APP" "$SWITCH_AUTOSTART" "$SWITCH_APP"
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
Exec=$PYTHON $DIR/main.py --autostart
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

# 顶栏总开关（眼睛图标）：单击显示 / 隐藏全部贴纸
write_switch_autostart() {
    cat > "$SWITCH_AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=桌面贴纸开关
Name[en]=Stickers Switch
Comment=顶栏图标：一键显示 / 隐藏全部桌面贴纸
Exec=$PYTHON $DIR/stickers_switch.py --run
Path=$DIR
Icon=view-reveal-symbolic
Terminal=false
StartupNotify=false
X-GNOME-Autostart-Delay=4
EOF
}

# 应用菜单里的「贴纸 开/关」：一键切换，并确保顶栏开关在线
write_switch_launcher() {
    cat > "$SWITCH_APP" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=贴纸 开/关
Name[en]=Toggle Stickers
Comment=一键显示 / 隐藏全部桌面贴纸（与顶栏图标等效）
Exec=$PYTHON $DIR/stickers_switch.py --toggle
Path=$DIR
Icon=view-reveal-symbolic
Terminal=false
StartupNotify=false
EOF
}

# 开机自启（延迟几秒，等桌面就绪）
write_desktop_file "$STICKERS_AUTOSTART" 5
write_lyrics_autostart
write_switch_autostart

# 应用菜单里的「贴纸设置」控制器
write_control_launcher() {
    cat > "$CONTROL_APP" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=贴纸设置
Name[en]=Sticker Settings
Comment=调整卡片尺寸 / 位置 / 圆角 / 不透明度
Exec=$PYTHON $DIR/control.py
Path=$DIR
Icon=preferences-desktop-theme
Terminal=false
StartupNotify=false
EOF
}

# 应用菜单快捷方式
write_desktop_file "$STICKERS_APP" ""
write_lyrics_launcher
write_control_launcher
write_switch_launcher

command -v update-desktop-database >/dev/null && update-desktop-database "$APPS_DIR" || true

echo "完成："
echo "  贴纸自启  -> $STICKERS_AUTOSTART（沿用上次开关状态）"
echo "  歌词自启  -> $LYRICS_AUTOSTART（沿用上次开关状态）"
echo "  开关自启  -> $SWITCH_AUTOSTART（顶栏眼睛图标）"
echo "  应用菜单  -> $STICKERS_APP / $LYRICS_APP / $SWITCH_APP / $CONTROL_APP（贴纸设置）"
echo "立即体验：python3 $DIR/main.py"
