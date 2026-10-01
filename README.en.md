# Desktop Stickers (Frosted Glass)

**简体中文** · [English](README.en.md)

A GTK4 desktop widget set: **11 independent frosted-glass stickers** — borderless,
individually draggable, live system status. Plus a **top-bar lyrics plugin** that
shows the current lyric line while music is playing.

Project page: <https://github.com/KDSheRick/desktop-stickers>

![Desktop stickers](docs/screenshot.png)
![Top-bar lyrics](docs/lyrics-bar.png)

## Stickers

| Sticker | Shows |
| --- | --- |
| 🕐 Clock | Time (seconds) + date / weekday |
| ⚙️ CPU | Usage ring, temperature, frequency (green → yellow → red) |
| 🧠 Memory | Usage ring, used / total, swap |
| 💾 Disk `/` | Usage bar, used / total, free space |
| 🌐 Network | Live download / upload rate + 40s dual-color sparkline |
| 🔋 Battery | Level bar, remaining time, ⚡ when charging |
| 🌡️ Temp / Fan | CPU temperature ring + fan speed |
| 🖥️ System | Hostname, distro, kernel, uptime, load (wide, two columns) |
| 🎵 Music | Cover art, title / artist, progress bar, **prev / play-pause / next**; bar is **clickable & draggable to seek** |
| 📄 Recent files | Frequently & recently opened files (two columns): double-click to open, right-click for more |
| 📊 Process Top 3 | Top CPU consumers with CPU + memory columns |

Layout: system monitors on the left, music / files / processes on the right —
both panels share the same width and are anchored to the screen corners.

## Requirements

- Ubuntu / GNOME with GTK 4.16+ (tested on Ubuntu 26.04, GNOME 50)
- `python3-gi`, `gir1.2-gtk-4.0`, `python3-psutil` (preinstalled on Ubuntu)
- Optional: `python3-pil` (rounded cover icons), `blur-my-shell` extension for real blur

## Run

```bash
cd desktop-stickers
python3 main.py
```

Drag any sticker to move it (positions are remembered). Right-click any sticker for
the menu: reset layout / toggle dark-light / toggle the top-bar lyrics / quit.

## Top-bar lyrics (standalone plugin)

```bash
python3 lyrics_tray.py --status   # running / stopped
python3 lyrics_tray.py --start    # enable
python3 lyrics_tray.py --stop     # disable
python3 lyrics_tray.py --toggle   # switch
```

- Shows the current lyric line next to the album cover in the GNOME top bar
- Click the tray item to play/pause; right-click for play/pause, prev, next, close
- Lyrics from NetEase Cloud Music (fallback: [lrclib.net](https://lrclib.net));
  fetched once per track and cached in `~/.cache/sysstickers/lyrics/`
- The on/off state is remembered and respected by autostart
- Requires Ubuntu's `ubuntu-appindicators` extension (enabled by default)

## Autostart & blur

```bash
bash install-autostart.sh          # add to startup + app menu shortcuts
bash enable-blur.sh                # real background blur (needs GNOME Rounded Blur)
bash tools/gnome-rounded-blur/rounded_blur_build.sh -i   # install that library (sudo)
```

## Customization

| What | Where |
| --- | --- |
| Card width | `widgets.py`: `CARD_WIDTH` / `WIDE_WIDTH` |
| Cover size / buttons | `widgets.py`: `MUSIC_COVER` / `PLAY_SIZE` / `SKIP_SIZE` |
| Margins / spacing | `main.py`: `GAP` / `MARGIN_X` / `MARGIN_TOP` |
| Colors, radius, fonts | `style.css` |
| Top-bar lyrics width | `lyrics_tray.py`: `MAX_CELLS` |

> Note: if you change the card corner radius in `style.css`, also set the
> Blur my Shell `applications corner-radius` to `card radius + 9` (run `enable-blur.sh`),
> so the blur outline stays concentric with the card.

## Styling & custom GTK themes

The stickers are entirely styled by `style.css`. One catch worth knowing:

Third-party GTK themes (MacTahoe, Orchis, …) ship a user stylesheet at
`~/.config/gtk-4.0/gtk.css`, loaded at the **USER priority**. It overrides *generic*
selectors such as `window`, `.card` and `button`, which makes the stickers look wrong:

- card backgrounds pick up the theme's colors instead of the glass design
- buttons fall back to the theme's default look (square corners, dated icons)
- custom classes like `.clock-time` are unaffected — so it looks inconsistent,
  and is hard to debug

The app therefore loads its own `style.css` at **USER + 1** priority
(see `_load_css` in `main.py`), so the stickers always render as designed,
no matter which theme is installed.

> Want to follow the theme instead? Write your overrides at USER+2 or higher,
> or simply edit `style.css` in this project.

## License

MIT — see [LICENSE](LICENSE).
`tools/gnome-rounded-blur/` is third-party code under GPL-3.0 (see its README).
