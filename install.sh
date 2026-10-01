#!/usr/bin/env bash
# 一键安装「桌面系统贴纸 + 顶栏歌词」。
#
#   bash install.sh            # 安装：依赖检查 + 开机自启 + 应用菜单 + 启动
#   bash install.sh --remove   # 卸载自启动与菜单快捷方式
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(command -v python3 || true)"

green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[1;33m%s\033[0m\n' "$*"; }
red() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }

if [[ "${1:-}" == "--remove" ]]; then
    bash "$DIR/install-autostart.sh" --remove
    if [ -n "$PYTHON" ]; then
        "$PYTHON" "$DIR/lyrics_tray.py" --stop >/dev/null 2>&1 || true
    fi
    green "已移除开机自启与菜单快捷方式（贴纸还在运行的话，右键菜单 →「退出」）"
    exit 0
fi

# ---------------------------------------------------------------- 1. 依赖检查
if [ -z "$PYTHON" ]; then
    red "未找到 python3，请先安装：sudo apt install python3"
    exit 1
fi

if ! "$PYTHON" - <<'DEPCHECK'
import sys

missing = []
if sys.version_info < (3, 10):
    missing.append("python3 >= 3.10（当前 %d.%d）" % sys.version_info[:2])
try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gtk  # noqa: F401
except Exception as exc:  # noqa: BLE001
    missing.append(f"python3-gi + gir1.2-gtk-4.0（{exc}）")
try:
    import psutil  # noqa: F401
except Exception:  # noqa: BLE001
    missing.append("python3-psutil")

if missing:
    print("缺少依赖：")
    for item in missing:
        print("  -", item)
    raise SystemExit(1)
DEPCHECK
then
    echo
    red "请先安装依赖，再重新运行本脚本："
    echo "  sudo apt install python3-gi gir1.2-gtk-4.0 python3-psutil python3-pil"
    exit 1
fi
green "✓ 依赖检查通过"

# ---------------------------------------------------------------- 2. 自启动 + 菜单
bash "$DIR/install-autostart.sh" >/dev/null
green "✓ 已加入开机自启（贴纸 + 顶栏歌词），并创建应用菜单快捷方式"

# ---------------------------------------------------------------- 3. 启动
setsid nohup "$PYTHON" "$DIR/main.py" >/tmp/sysstickers-start.log 2>&1 < /dev/null &
sleep 2
"$PYTHON" "$DIR/lyrics_tray.py" --start >/dev/null 2>&1 || true
green "✓ 贴纸与顶栏歌词已启动"

# ---------------------------------------------------------------- 4. 背景模糊（可选）
if "$PYTHON" -c "import gi; gi.require_version('Blur', '1.0'); from gi.repository import Blur" 2>/dev/null \
    && [ -d "$HOME/.local/share/gnome-shell/extensions/blur-my-shell@aunetx" ]; then
    if bash "$DIR/enable-blur.sh" >/dev/null 2>&1; then
        green "✓ 已开启背景模糊（圆角与卡片同心）"
    else
        yellow "背景模糊开启失败，可稍后手动执行：bash enable-blur.sh"
    fi
else
    yellow "· 未检测到 GNOME Rounded Blur 库，贴纸将以半透明玻璃样式运行。"
    echo "  想要真正的背景模糊（可选）："
    echo "    1) bash tools/gnome-rounded-blur/rounded_blur_build.sh -i   # 需要 sudo"
    echo "    2) 注销并重新登录"
    echo "    3) bash enable-blur.sh"
fi

# ---------------------------------------------------------------- 5. 完成
echo
green "安装完成 🎉"
echo "  · 调整外观（尺寸 / 圆角 / 不透明度 / 位置）："
echo "      应用菜单搜索「贴纸设置」，或右键任意贴纸 →「贴纸设置…」"
echo "  · 顶栏歌词开关：右键贴纸菜单，或控制器里的「顶栏歌词」"
echo "  · 卸载：bash install.sh --remove"
