/**
 * 歌词顶栏（lyrics-panel@loong）
 *
 * 播放音乐时在 GNOME 顶栏（默认左侧 Activities 之后）显示当前歌词句：
 *   1. 通过 MPRIS 读取当前播放器（Firefox / 本地播放器等），拿到歌名、歌手、播放进度；
 *   2. 优先从网易云音乐搜索歌词（LRC），失败时用 lrclib.net 兜底；
 *   3. 按进度高亮当前句，每 250ms 刷新，暂停时定格。
 *
 * 无播放器或停止播放时自动隐藏，不占顶栏空间。
 */

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Pango from 'gi://Pango';
import Soup from 'gi://Soup?version=3.0';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const MPRIS_PREFIX = 'org.mpris.MediaPlayer2.';
const MPRIS_PATH = '/org/mpris/MediaPlayer2';
const PLAYER_IFACE = 'org.mpris.MediaPlayer2.Player';

const STATUS_RANK = {Playing: 0, Paused: 1, Stopped: 2};

// 顶栏文字最大宽度（以半角字符计，中文/全角算 2 个），超出省略
const MAX_CELLS = 48;
const POLL_INTERVAL_MS = 1000;
const TICK_INTERVAL_MS = 250;

// 过滤歌词开头的制作人员信息（作词、作曲、编曲等）
const CREDIT_RE = new RegExp(
    '^(作词|作曲|词曲|编曲|改编词曲|制作人|监制|出品|发行|录音|录音师|混音|母带|' +
    '和声|合声|配唱|吉他|贝斯|鼓|键盘|弦乐|音乐总监|统筹|企划|封面|设计|人声编辑|' +
    'op|sp|producer|composer|lyricist|arranger|mixing|mastering)\\s*[:：]', 'i');

// ---------------------------------------------------------------- LRC 解析

/** 判断字符占用的显示宽度（全角 2，半角 1）。 */
function charCells(char) {
    const code = char.codePointAt(0);
    const wide = (code >= 0x1100 && code <= 0x115f) ||
        (code >= 0x2e80 && code <= 0xa4cf) ||
        (code >= 0xac00 && code <= 0xd7a3) ||
        (code >= 0xf900 && code <= 0xfaff) ||
        (code >= 0xfe30 && code <= 0xfe6f) ||
        (code >= 0xff00 && code <= 0xff60) ||
        (code >= 0xffe0 && code <= 0xffe6) ||
        (code >= 0x20000 && code <= 0x3fffd);
    return wide ? 2 : 1;
}

/** 按显示宽度截断文本，末尾加省略号。 */
function truncateCells(text, maxCells) {
    let total = 0;
    for (const char of text)
        total += charCells(char);
    if (total <= maxCells)
        return text;

    let out = '';
    let cells = 0;
    for (const char of text) {
        const width = charCells(char);
        if (cells + width > maxCells - 1)
            break;
        out += char;
        cells += width;
    }
    return `${out}…`;
}

/** LRC 文本 → [{t: 秒, text}]，过滤制作人员行，合并同时间戳。 */
function parseLrc(text) {
    const lines = [];
    let offsetMs = 0;
    const stampRe = /\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]/g;

    for (const raw of text.split('\n')) {
        const offsetMatch = raw.match(/^\s*\[offset:\s*(-?\d+)\s*\]\s*$/i);
        if (offsetMatch) {
            offsetMs = parseInt(offsetMatch[1], 10) || 0;
            continue;
        }

        const stamps = [...raw.matchAll(stampRe)];
        if (stamps.length === 0)
            continue;

        const content = raw.replace(/\[[^\]]*\]/g, '').trim();
        if (!content || CREDIT_RE.test(content))
            continue;

        for (const stamp of stamps) {
            const minutes = parseInt(stamp[1], 10);
            const seconds = parseInt(stamp[2], 10);
            const fracText = stamp[3] ?? '0';
            const frac = parseInt(fracText, 10) / 10 ** fracText.length;
            lines.push({t: minutes * 60 + seconds + frac, text: content});
        }
    }

    lines.sort((a, b) => a.t - b.t);
    if (offsetMs)
        for (const line of lines)
            line.t += offsetMs / 1000;

    const merged = [];
    for (const line of lines) {
        const last = merged[merged.length - 1];
        if (last && Math.abs(last.t - line.t) < 0.01)
            last.text += ` / ${line.text}`;
        else
            merged.push({...line});
    }
    return merged;
}

// ---------------------------------------------------------------- 扩展本体

export default class LyricsPanelExtension extends Extension {
    enable() {
        this._cacheDir = GLib.build_filenamev([GLib.get_user_cache_dir(), 'sysstickers', 'lyrics']);
        GLib.mkdir_with_parents(this._cacheDir, 0o755);

        this._session = new Soup.Session({timeout: 8});

        this._player = null;
        this._status = 'Stopped';
        this._title = '';
        this._artist = '';
        this._trackKey = '';
        this._lines = [];
        this._positionSec = 0;
        this._sampledAt = 0;
        this._fetchToken = 0;
        this._polling = false;

        this._indicator = new PanelMenu.Button(0.0, 'LyricsPanel', true);
        this._indicator.reactive = false;
        this._indicator.can_focus = false;
        this._indicator.track_hover = false;
        this._indicator.visible = false;

        this._label = new St.Label({
            text: '',
            style_class: 'lyrics-panel-label',
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._label.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        this._indicator.add_child(this._label);

        // 左侧：Activities 之后
        Main.panel.addToStatusArea(this.uuid, this._indicator, 1, 'left');

        this._pollPlayers();
        this._pollTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, POLL_INTERVAL_MS, () => {
            this._pollPlayers();
            return GLib.SOURCE_CONTINUE;
        });
        this._tickTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, TICK_INTERVAL_MS, () => {
            this._updateLabel();
            return GLib.SOURCE_CONTINUE;
        });
    }

    disable() {
        if (this._pollTimer) {
            GLib.source_remove(this._pollTimer);
            this._pollTimer = 0;
        }
        if (this._tickTimer) {
            GLib.source_remove(this._tickTimer);
            this._tickTimer = 0;
        }
        this._fetchToken += 1;
        if (this._session) {
            this._session.abort();
            this._session = null;
        }
        if (this._indicator) {
            this._indicator.destroy();
            this._indicator = null;
        }
        this._label = null;
        this._lines = [];
    }

    // ------------------------------------------------------------ MPRIS 轮询

    _pollPlayers() {
        if (this._polling)
            return;
        this._polling = true;

        Gio.DBus.session.call(
            'org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus', 'ListNames',
            null, null, Gio.DBusCallFlags.NONE, 800, null,
            (conn, res) => {
                let names = [];
                try {
                    const [list] = conn.call_finish(res).deep_unpack();
                    names = list.filter(name => name.startsWith(MPRIS_PREFIX));
                } catch (error) {
                    this._polling = false;
                    return;
                }

                if (names.length === 0) {
                    this._polling = false;
                    this._applyPlayer(null, null);
                    return;
                }

                const results = [];
                let remaining = names.length;
                for (const name of names) {
                    conn.call(name, MPRIS_PATH, 'org.freedesktop.DBus.Properties', 'GetAll',
                        new GLib.Variant('(s)', [PLAYER_IFACE]), null, Gio.DBusCallFlags.NONE, 800, null,
                        (c, r) => {
                            try {
                                const [props] = c.call_finish(r).deep_unpack();
                                results.push([name, props]);
                            } catch (error) {
                                // 例：snap 的 AppArmor 策略拒绝，忽略即可
                            }
                            if (--remaining === 0) {
                                this._polling = false;
                                this._pickPlayer(results);
                            }
                        });
                }
            });
    }

    _pickPlayer(results) {
        if (results.length === 0) {
            this._applyPlayer(null, null);
            return;
        }
        results.sort((a, b) => {
            const rankA = STATUS_RANK[a[1]['PlaybackStatus']] ?? 3;
            const rankB = STATUS_RANK[b[1]['PlaybackStatus']] ?? 3;
            if (rankA !== rankB)
                return rankA - rankB;
            // 状态相同时，优先保持当前播放器，避免多播放器来回跳
            return (a[0] === this._player ? 0 : 1) - (b[0] === this._player ? 0 : 1);
        });
        const [name, props] = results[0];
        this._applyPlayer(name, props);
    }

    _applyPlayer(name, props) {
        if (!name || !props) {
            this._player = null;
            this._status = 'Stopped';
            this._indicator.visible = false;
            return;
        }

        const meta = props['Metadata'] ?? {};
        let title = String(meta['xesam:title'] ?? '');
        let artist = meta['xesam:artist'];
        artist = Array.isArray(artist) ? artist.join(' / ') : String(artist ?? '');
        // 浏览器有时把「歌名 - 歌手」都塞进标题
        if (!artist && title.includes(' - ')) {
            const parts = title.split(' - ');
            title = parts[0];
            artist = parts.slice(1).join(' - ');
        }
        const album = String(meta['xesam:album'] ?? '');
        const status = String(props['PlaybackStatus'] ?? 'Stopped');

        this._player = name;
        this._status = status;
        this._title = title;
        this._artist = artist;

        if (props['Position'] !== undefined && props['Position'] !== null)
            this._positionSec = Number(props['Position']) / 1e6;
        this._sampledAt = GLib.get_monotonic_time() / 1e6;

        const key = `${title}|${artist}|${album}`;
        if (key !== this._trackKey) {
            this._trackKey = key;
            this._lines = [];
            if (title)
                this._fetchLyrics(title, artist, album);
        }

        this._indicator.visible = status !== 'Stopped' && title !== '';
        this._updateLabel();
    }

    // ------------------------------------------------------------ 歌词获取

    _fetchLyrics(title, artist, album) {
        const token = ++this._fetchToken;

        const cached = this._readCache(title, artist);
        if (cached) {
            this._lines = cached;
            this._updateLabel();
            return;
        }

        const query = `${title} ${artist}`.trim();
        const searchUrl = 'https://music.163.com/api/search/get/web?csrf_token=&type=1&offset=0&limit=5&s=' +
            encodeURIComponent(query);

        this._httpGet(searchUrl, 'https://music.163.com/').then(text => {
            if (token !== this._fetchToken)
                return null;

            let songId = null;
            try {
                const data = JSON.parse(text);
                const songs = data?.result?.songs ?? [];
                const exact = songs.find(song => song.name === title) ?? songs[0];
                songId = exact?.id ?? null;
            } catch (error) {
                songId = null;
            }
            if (!songId)
                throw new Error('netease search miss');

            const lyricUrl = `https://music.163.com/api/song/lyric?id=${songId}&lv=-1&kv=-1&tv=-1`;
            return this._httpGet(lyricUrl, 'https://music.163.com/').then(lyricText => {
                if (token !== this._fetchToken)
                    return;
                let lines = [];
                try {
                    const data = JSON.parse(lyricText);
                    lines = parseLrc(data?.lrc?.lyric ?? '');
                } catch (error) {
                    lines = [];
                }
                if (lines.length === 0)
                    throw new Error('netease no synced lyrics');
                this._writeCache(title, artist, lines);
                this._lines = lines;
                this._updateLabel();
            });
        }).catch(() => {
            if (token === this._fetchToken)
                this._fetchFromLrclib(token, title, artist, album);
        });
    }

    _fetchFromLrclib(token, title, artist, album) {
        const url = 'https://lrclib.net/api/search?' + [
            `track_name=${encodeURIComponent(title)}`,
            `artist_name=${encodeURIComponent(artist)}`,
            `album_name=${encodeURIComponent(album)}`,
        ].join('&');

        this._httpGet(url, 'https://lrclib.net/').then(text => {
            if (token !== this._fetchToken)
                return;
            let lines = [];
            try {
                for (const item of JSON.parse(text)) {
                    if (!item.syncedLyrics)
                        continue;
                    lines = parseLrc(item.syncedLyrics);
                    if (lines.length)
                        break;
                }
            } catch (error) {
                lines = [];
            }
            if (lines.length) {
                this._writeCache(title, artist, lines);
                this._lines = lines;
                this._updateLabel();
            }
        }).catch(() => {});
    }

    _httpGet(url, referer) {
        return new Promise((resolve, reject) => {
            let message;
            try {
                message = Soup.Message.new('GET', url);
            } catch (error) {
                reject(error);
                return;
            }
            if (!message) {
                reject(new Error('bad url'));
                return;
            }
            const headers = message.get_request_headers();
            headers.append('User-Agent', 'Mozilla/5.0 (X11; Linux x86_64) sysstickers-lyrics/1.0');
            if (referer)
                headers.append('Referer', referer);

            this._session.send_and_read_async(message, GLib.PRIORITY_DEFAULT, null, (session, res) => {
                try {
                    const bytes = session.send_and_read_finish(res);
                    if (message.get_status() !== Soup.Status.OK) {
                        reject(new Error(`HTTP ${message.get_status()}`));
                        return;
                    }
                    resolve(new TextDecoder().decode(bytes.get_data()));
                } catch (error) {
                    reject(error);
                }
            });
        });
    }

    // ------------------------------------------------------------ 歌词缓存

    _cachePath(title, artist) {
        const digest = GLib.compute_checksum_for_string(GLib.ChecksumType.SHA256,
            `${title}|${artist}`, -1);
        return GLib.build_filenamev([this._cacheDir, `${digest}.json`]);
    }

    _readCache(title, artist) {
        try {
            const [ok, contents] = GLib.file_get_contents(this._cachePath(title, artist));
            if (!ok)
                return null;
            const lines = JSON.parse(new TextDecoder().decode(contents));
            if (Array.isArray(lines) && lines.length)
                return lines;
        } catch (error) {
            // 缓存损坏直接忽略
        }
        return null;
    }

    _writeCache(title, artist, lines) {
        try {
            GLib.file_set_contents(this._cachePath(title, artist), JSON.stringify(lines));
        } catch (error) {
            // 写不进去也不影响显示
        }
    }

    // ------------------------------------------------------------ 顶栏刷新

    _currentLine() {
        if (this._lines.length === 0)
            return '';
        let position = this._positionSec;
        if (this._status === 'Playing')
            position += GLib.get_monotonic_time() / 1e6 - this._sampledAt;

        let index = -1;
        for (let i = 0; i < this._lines.length; i++) {
            if (this._lines[i].t <= position + 0.15)
                index = i;
            else
                break;
        }
        return index >= 0 ? this._lines[index].text : '';
    }

    _updateLabel() {
        if (!this._indicator || !this._label)
            return;

        let text = this._currentLine();
        if (text)
            text = `♪ ${text}`;
        else if (this._title)
            text = `♪ ${this._title}${this._artist ? ` · ${this._artist}` : ''}`;

        text = truncateCells(text, MAX_CELLS);
        if (this._label.text !== text)
            this._label.text = text;
    }
}
