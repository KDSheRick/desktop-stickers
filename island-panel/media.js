/**
 * 媒体数据层：MPRIS 播放器状态 + 歌词 + 专辑封面。
 *
 * 从原「顶栏歌词」扩展（lyrics-panel）迁移而来，改成回调风格给灵动岛 UI 用：
 *   - 每 1 秒轮询 MPRIS，挑「正在播放」优先的播放器
 *   - 歌名/歌手变化时拉歌词（网易云优先，lrclib 兜底）与封面，结果落盘缓存
 *   - 提供 playPause / next / previous / seekTo 控制
 *
 * 位置（Position）由采样时间做外推，UI 每 100ms 读一次即可平滑滚动。
 */

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Soup from 'gi://Soup?version=3.0';

const MPRIS_PREFIX = 'org.mpris.MediaPlayer2.';
const MPRIS_PATH = '/org/mpris/MediaPlayer2';
const PLAYER_IFACE = 'org.mpris.MediaPlayer2.Player';

const STATUS_RANK = {Playing: 0, Paused: 1, Stopped: 2};

const POLL_INTERVAL_MS = 1000;

// 过滤歌词开头的制作人员信息（作词、作曲、编曲等）
const CREDIT_RE = new RegExp(
    '^(作词|作曲|词曲|编曲|改编词曲|制作人|监制|出品|发行|录音|录音师|混音|母带|' +
    '和声|合声|伴唱|配唱|吉他|贝斯|鼓|打击乐|键盘|弦乐|音乐总监|统筹|企划|封面|设计|人声编辑|' +
    'op|sp|producer|composer|lyricist|arranger|mixing|mastering)[^:：\\n]{0,10}[:：]', 'i');

// ---------------------------------------------------------------- LRC 解析

/** LRC 文本 → [{t: 秒, text}]，过滤制作人员行，合并同时间戳。 */
export function parseLrc(text) {
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

/** 取 position（秒）对应的歌词句；还没到第一句时返回空串。 */
export function lineAt(lines, position) {
    let text = '';
    for (const line of lines) {
        if (line.t <= position + 0.15)
            text = line.text;
        else
            break;
    }
    return text;
}

// ---------------------------------------------------------------- 工具


/** GJS 的 deep_unpack() 不会拆 a{sv} 里残留的 Variant，这里递归拆干净。 */
function unpack(value) {
    if (value instanceof GLib.Variant)
        return unpack(value.deep_unpack());
    if (Array.isArray(value))
        return value.map(item => unpack(item));
    if (value !== null && typeof value === 'object')
        return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, unpack(item)]));
    return value;
}

function sha1(text) {
    return GLib.compute_checksum_for_string(GLib.ChecksumType.SHA1, text, -1);
}

/** 网易云 picId → 专辑封面地址（经典的 XOR + MD5 + base64 算法）。 */
function neteaseCoverUrl(picId) {
    try {
        const key = '3go8&$8*3*3h0k(2)2';
        const text = String(picId);
        const bytes = new Uint8Array(text.length);
        for (let i = 0; i < text.length; i++)
            bytes[i] = text.charCodeAt(i) ^ key.charCodeAt(i % key.length);
        const digestHex = GLib.compute_checksum_for_data(GLib.ChecksumType.MD5, bytes);
        const digest = new Uint8Array(digestHex.length / 2);
        for (let i = 0; i < digest.length; i++)
            digest[i] = parseInt(digestHex.slice(i * 2, i * 2 + 2), 16);
        const encoded = GLib.base64_encode(digest)
            .replaceAll('/', '_').replaceAll('+', '-');
        return `https://p1.music.126.net/${encoded}/${text}.jpg`;
    } catch {
        return null;
    }
}

function normalizeTitle(text) {
    return String(text ?? '')
        .replace(/[\(\（\[【].*?[\)\）\]】]/g, '')
        .split(' - ')[0]
        .trim().toLowerCase();
}

function primaryArtist(artist) {
    let value = String(artist ?? '');
    for (const sep of ['/', '、', '&', ',', '，', ' feat.', ' ft.', ' with '])
        value = value.split(sep)[0];
    return value.trim();
}

/** 拆出歌名/歌手：浏览器常把「歌名 - 歌手」都塞进标题。 */
function splitMeta(title, artist) {
    let name = String(title ?? '');
    let singer = String(artist ?? '');
    if (!singer && name.includes(' - ')) {
        const parts = name.split(' - ');
        name = parts[0];
        singer = parts.slice(1).join(' - ');
    }
    return {title: name.trim(), artist: singer.trim()};
}

// ---------------------------------------------------------------- 观察器

export class MediaWatcher {
    /**
     * @param {object} callbacks
     * @param {Function} callbacks.onMusic  - (music|null) 播放器状态变化（每 1 秒）
     * @param {Function} callbacks.onLyrics - (lines) 歌词就绪
     * @param {Function} callbacks.onCover  - (path|null) 封面就绪（本地文件）
     */
    constructor({onMusic, onLyrics, onCover} = {}) {
        this._onMusic = onMusic ?? (() => {});
        this._onLyrics = onLyrics ?? (() => {});
        this._onCover = onCover ?? (() => {});

        this._cacheDir = GLib.build_filenamev([GLib.get_user_cache_dir(), 'sysstickers', 'lyrics']);
        this._coverDir = GLib.build_filenamev([GLib.get_user_cache_dir(), 'sysstickers', 'covers-js']);
        GLib.mkdir_with_parents(this._cacheDir, 0o755);
        GLib.mkdir_with_parents(this._coverDir, 0o755);

        this._session = null;
        this._pollTimer = 0;
        this._polling = false;
        this._generation = 0;

        this._music = null;        // {player, busName, status, title, artist, album, length, canNext, canPrev, trackId}
        this._lines = [];
        this._trackKey = '';
        this._position = 0;
        this._sampledAt = 0;
        this._hasPosition = false;
    }

    enable() {
        this._generation += 1;
        this._session = new Soup.Session({timeout: 8});
        this._pollPlayers();
        this._pollTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, POLL_INTERVAL_MS, () => {
            this._pollPlayers();
            return GLib.SOURCE_CONTINUE;
        });
    }

    disable() {
        this._generation += 1;
        if (this._pollTimer) {
            GLib.source_remove(this._pollTimer);
            this._pollTimer = 0;
        }
        if (this._session) {
            this._session.abort();
            this._session = null;
        }
        this._music = null;
        this._lines = [];
    }

    // ------------------------------------------------------------ 状态读取

    get music() {
        return this._music;
    }

    get lyrics() {
        return this._lines;
    }

    /** 外推后的播放位置（秒）。 */
    get position() {
        if (!this._music)
            return 0;
        if (this._music.status === 'Playing')
            return this._position + (GLib.get_monotonic_time() / 1e6 - this._sampledAt);
        return this._position;
    }

    // ------------------------------------------------------------ 控制

    _playerCall(method, params = null) {
        if (!this._music?.busName)
            return;
        try {
            Gio.DBus.session.call(
                this._music.busName, MPRIS_PATH, PLAYER_IFACE,
                method, params, null, Gio.DBusCallFlags.NONE, 800, null, null);
        } catch {
            // 播放器退出等竞态：忽略
        }
    }

    playPause() {
        this._playerCall('PlayPause');
        if (this._music) {
            this._music.status = this._music.status === 'Playing' ? 'Paused' : 'Playing';
            this._position = this.position;
            this._sampledAt = GLib.get_monotonic_time() / 1e6;
            this._onMusic(this._music);
        }
    }

    next() {
        this._playerCall('Next');
    }

    previous() {
        this._playerCall('Previous');
    }

    /** 定位到指定秒数（优先 SetPosition，播放器不支持时用相对 Seek）。 */
    seekTo(seconds) {
        if (!this._music)
            return;
        const target = Math.max(0, seconds);
        const delta = target - this._position;
        this._position = target;
        this._sampledAt = GLib.get_monotonic_time() / 1e6;
        if (this._music.trackId)
            this._playerCall('SetPosition', new GLib.Variant('(ox)', [this._music.trackId, Math.round(target * 1e6)]));
        else
            this._playerCall('Seek', new GLib.Variant('(x)', [Math.round(delta * 1e6)]));
    }

    // ------------------------------------------------------------ MPRIS 轮询

    _pollPlayers() {
        if (this._polling || !this._session)
            return;
        this._polling = true;
        const generation = this._generation;

        Gio.DBus.session.call(
            'org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus', 'ListNames',
            null, null, Gio.DBusCallFlags.NONE, 800, null,
            (conn, res) => {
                let names = [];
                try {
                    const [list] = conn.call_finish(res).deep_unpack();
                    names = list.filter(name => name.startsWith(MPRIS_PREFIX));
                } catch {
                    this._polling = false;
                    return;
                }
                if (generation !== this._generation)
                    return;

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
                            } catch {
                                // snap 的 AppArmor 策略拒绝等情况，忽略
                            }
                            if (--remaining === 0 && generation === this._generation) {
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
            const rankA = STATUS_RANK[unpack(a[1]['PlaybackStatus'])] ?? 3;
            const rankB = STATUS_RANK[unpack(b[1]['PlaybackStatus'])] ?? 3;
            if (rankA !== rankB)
                return rankA - rankB;
            // 状态相同时优先保持当前播放器，避免多播放器来回跳
            return (a[0] === this._music?.busName ? 0 : 1) - (b[0] === this._music?.busName ? 0 : 1);
        });
        const [name, props] = results[0];
        this._applyPlayer(name, props);
    }

    _applyPlayer(busName, props) {
        if (!busName || !props) {
            if (this._music) {
                this._music = null;
                this._trackKey = '';
                this._lines = [];
                this._onMusic(null);
            }
            return;
        }

        const meta = unpack(props['Metadata']) ?? {};
        const {title, artist} = splitMeta(meta['xesam:title'], meta['xesam:artist']);
        const album = String(meta['xesam:album'] ?? '');
        const status = String(unpack(props['PlaybackStatus']) ?? 'Stopped');
        const length = meta['mpris:length'] ? Number(meta['mpris:length']) / 1e6 : 0;

        const position = unpack(props['Position']);
        if (position !== undefined && position !== null) {
            this._position = Number(position) / 1e6;
            this._hasPosition = true;
        } else {
            this._hasPosition = false;
        }
        this._sampledAt = GLib.get_monotonic_time() / 1e6;

        this._music = {
            player: busName,
            busName,
            status,
            title,
            artist,
            album,
            length,
            position: this._position,
            canNext: unpack(props['CanGoNext']) !== false,
            canPrev: unpack(props['CanGoPrevious']) !== false,
            trackId: String(meta['mpris:trackid'] ?? ''),
            artUrl: String(meta['mpris:artUrl'] ?? ''),
        };
        this._onMusic(this._music);

        const key = `${title}|${artist}|${album}`;
        if (key !== this._trackKey) {
            this._trackKey = key;
            this._lines = [];
            this._onLyrics([]);
            if (title)
                this._loadTrack(title, artist, album, this._music.artUrl);
        }

        // 部分播放器（如 Firefox）的 GetAll 不含 Position，单独查一次
        if (!this._hasPosition && status !== 'Stopped')
            this._queryPosition(busName, this._generation);
    }

    _queryPosition(busName, generation) {
        try {
            Gio.DBus.session.call(
                busName, MPRIS_PATH, 'org.freedesktop.DBus.Properties', 'Get',
                new GLib.Variant('(ss)', [PLAYER_IFACE, 'Position']), null,
                Gio.DBusCallFlags.NONE, 800, null,
                (conn, res) => {
                    if (generation !== this._generation || !this._music || this._music.busName !== busName)
                        return;
                    try {
                        const [wrapped] = conn.call_finish(res).deep_unpack();
                        const position = Number(unpack(wrapped));
                        if (Number.isFinite(position)) {
                            this._position = position / 1e6;
                            this._sampledAt = GLib.get_monotonic_time() / 1e6;
                            this._music.position = this._position;
                        }
                    } catch {
                        // 拿不到就算了，按外推显示
                    }
                });
        } catch {
            // 忽略
        }
    }

    // ------------------------------------------------------------ 歌词 / 封面

    _loadTrack(title, artist, album, artUrl) {
        const generation = this._generation;

        // 封面：优先播放器给的 artUrl，其次网易云搜索结果
        if (artUrl)
            this._resolveCover(artUrl, generation);

        const cached = this._readCache(title, artist);
        if (cached) {
            this._lines = cached.lines;
            this._onLyrics(this._lines);
            if (artUrl) {
                this._resolveCover(artUrl, generation);
                return;
            }
            if (cached.cover) {
                this._resolveCover(cached.cover, generation);
                return;
            }
            // 缓存里只有歌词（例如早期版本没存封面）：继续往下走一次搜索补封面
        }

        const query = `${title} ${primaryArtist(artist)}`.trim();
        const searchUrl = 'https://music.163.com/api/search/get/web?csrf_token=&type=1&offset=0&limit=10&s=' +
            encodeURIComponent(query);

        this._httpGetText(searchUrl, 'https://music.163.com/').then(text => {
            if (generation !== this._generation)
                return null;

            let songId = null;
            let cover = null;
            try {
                const data = JSON.parse(text);
                const songs = data?.result?.songs ?? [];
                const wantedTitle = normalizeTitle(title);
                const wantedArtist = primaryArtist(artist).toLowerCase();
                let best = null;
                let bestScore = -1;
                for (const song of songs) {
                    const name = String(song.name ?? '');
                    if (normalizeTitle(name) !== wantedTitle)
                        continue;
                    const names = (song.artists ?? []).map(item => String(item.name ?? ''));
                    const joined = names.join(' ').toLowerCase();
                    if (wantedArtist && !joined.includes(wantedArtist))
                        continue;
                    let score = 0;
                    if (wantedArtist && names.length && names[0].toLowerCase().includes(wantedArtist))
                        score += 3;
                    if (name.trim() === title.trim())
                        score += 1;
                    if (!/翻自|翻唱|cover|remix/i.test(name))
                        score += 1;
                    if (score > bestScore) {
                        best = song;
                        bestScore = score;
                    }
                }
                songId = best?.id ?? null;
                if (best?.album?.picId)
                    cover = neteaseCoverUrl(best.album.picId);
            } catch {
                songId = null;
            }

            if (!artUrl && cover)
                this._resolveCover(cover, generation);

            if (!songId)
                throw new Error('netease search miss');

            const lyricUrl = `https://music.163.com/api/song/lyric?id=${songId}&lv=-1&kv=-1&tv=-1`;
            return this._httpGetText(lyricUrl, 'https://music.163.com/').then(lyricText => {
                if (generation !== this._generation)
                    return;
                let lines = [];
                try {
                    lines = parseLrc(JSON.parse(lyricText)?.lrc?.lyric ?? '');
                } catch {
                    lines = [];
                }
                if (lines.length === 0)
                    throw new Error('netease no synced lyrics');
                this._writeCache(title, artist, lines, cover);
                this._lines = lines;
                this._onLyrics(this._lines);
            });
        }).catch(() => {
            if (generation === this._generation)
                this._fetchFromLrclib(generation, title, artist, album, artUrl);
        });
    }

    _fetchFromLrclib(generation, title, artist, album, artUrl) {
        const url = 'https://lrclib.net/api/search?' + [
            `track_name=${encodeURIComponent(title)}`,
            `artist_name=${encodeURIComponent(primaryArtist(artist))}`,
            `album_name=${encodeURIComponent(album)}`,
        ].join('&');

        this._httpGetText(url, 'https://lrclib.net/').then(text => {
            if (generation !== this._generation)
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
            } catch {
                lines = [];
            }
            if (lines.length) {
                this._writeCache(title, artist, lines, null);
                this._lines = lines;
                this._onLyrics(this._lines);
            }
        }).catch(() => {});
    }

    // ------------------------------------------------------------ 封面缓存

    _resolveCover(url, generation) {
        if (!url)
            return;
        if (url.startsWith('file://')) {
            this._onCover(GLib.filename_from_uri(url)[0]);
            return;
        }
        const path = GLib.build_filenamev([this._coverDir, `${sha1(url)}.img`]);
        if (GLib.file_test(path, GLib.FileTest.EXISTS)) {
            this._onCover(path);
            return;
        }
        const target = url.includes('music.126.net') && !url.includes('?')
            ? `${url}?param=160y160` : url;
        this._httpGetBytes(target, 'https://music.163.com/').then(bytes => {
            if (generation !== this._generation || !bytes?.length)
                return;
            if (GLib.file_set_contents(path, bytes))
                this._onCover(path);
        }).catch(() => {});
    }

    // ------------------------------------------------------------ 歌词缓存

    _cachePath(title, artist) {
        const digest = sha1(`${title}|${artist}`);
        return GLib.build_filenamev([this._cacheDir, `${digest}.json`]);
    }

    _readCache(title, artist) {
        try {
            const [ok, contents] = GLib.file_get_contents(this._cachePath(title, artist));
            if (!ok)
                return null;
            const data = JSON.parse(new TextDecoder().decode(contents));
            if (Array.isArray(data) && data.length)   // 旧格式：只有歌词
                return {lines: data, cover: null};
            const lines = data?.lines;
            if (Array.isArray(lines) && lines.length)
                return {lines, cover: data?.cover ?? null};
        } catch {
            // 缓存损坏直接忽略
        }
        return null;
    }

    _writeCache(title, artist, lines, cover) {
        try {
            GLib.file_set_contents(this._cachePath(title, artist),
                JSON.stringify({lines, cover}));
        } catch {
            // 写不进去不影响显示
        }
    }

    // ------------------------------------------------------------ HTTP

    _httpRequest(url, referer) {
        const message = Soup.Message.new('GET', url);
        if (!message)
            throw new Error(`bad url: ${url}`);
        const headers = message.get_request_headers();
        headers.append('User-Agent', 'Mozilla/5.0 (X11; Linux x86_64) sysstickers-island/1.0');
        if (referer)
            headers.append('Referer', referer);
        return message;
    }

    _sendAndRead(message) {
        return new Promise((resolve, reject) => {
            this._session.send_and_read_async(message, GLib.PRIORITY_DEFAULT, null, (session, res) => {
                try {
                    const bytes = session.send_and_read_finish(res);
                    if (message.get_status() !== Soup.Status.OK) {
                        reject(new Error(`HTTP ${message.get_status()}`));
                        return;
                    }
                    resolve(bytes.get_data());
                } catch (error) {
                    reject(error);
                }
            });
        });
    }

    _httpGetText(url, referer) {
        return Promise.resolve()
            .then(() => this._sendAndRead(this._httpRequest(url, referer)))
            .then(data => new TextDecoder().decode(data));
    }

    _httpGetBytes(url, referer) {
        return Promise.resolve()
            .then(() => this._sendAndRead(this._httpRequest(url, referer)));
    }
}
