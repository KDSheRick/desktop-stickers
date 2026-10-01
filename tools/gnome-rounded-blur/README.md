# GNOME Rounded Blur（第三方，GPL-3.0）

本目录内容来自上游项目 **[kancko/gnome-rounded-blur](https://github.com/kancko/gnome-rounded-blur)**：

| 文件 | 说明 |
| --- | --- |
| `rounded_blur_build.sh` | 上游的安装 / 卸载脚本（原样收录） |
| `LICENSE` | 上游的 **GPL-3.0** 许可证全文 |

> 这**两个文件遵循 GPL-3.0**，与本项目其余部分（MIT）相互独立。

## 为什么需要它

GNOME Shell 自带的模糊效果不支持圆角，贴纸想要「背景模糊 + 圆角」两者兼得，
就需要安装这个库，并配合 [Blur my Shell](https://github.com/aunetx/blur-my-shell) 使用。

## 安装（Ubuntu / Debian 系，需要 sudo）

```bash
bash tools/gnome-rounded-blur/rounded_blur_build.sh -i
```

安装后注销并重新登录一次，再执行 `bash enable-blur.sh` 开启贴纸模糊。

如果本机访问 `github.com` 的 git 克隆不稳定（国内网络常见），
可以先用本项目自带的脚本通过 GitHub API 把源码下载到本地：

```bash
python3 tools/fetch_rounded_blur_src.py /tmp/rounded-blur-src
```
