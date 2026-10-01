/**
 * 灵动岛本体：顶栏正中间的黑色胶囊。
 *
 *   - 收起：闲置显示时间；播放音乐时显示迷你封面 + 跳动波形
 *   - 悬停展开（向下伸出，像 macOS）：音乐 / 歌词 / 时钟 / API 用量
 *   - 点击胶囊空白处「钉住」，移开 1.5 秒后自动收起；右键菜单
 *   - 启用时隐藏系统时钟（点击岛上的时间可打开日历/通知面板）
 *
 * 实现要点：作为 Shell 的 chrome actor 放在顶栏之上，用 Clutter 的
 * ease() 逐帧改变尺寸/位置；clip_to_allocation 保证动画中内容不溢出。
 */

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Pango from 'gi://Pango';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

import {MediaWatcher, lineAt} from './media.js';
import {UsageWatcher} from './usage.js';

try {
    // 调试截图用（Shell 内部 API，不走受限的 D-Bus 截图接口）
    Gio._promisify(Shell.Screenshot.prototype, 'screenshot_stage_to_content');
} catch {
    // 已经 promisify 过就忽略
}

const W_IDLE = 96;
const W_MUSIC = 320;      // 播放中的收起态：长条胶囊，里面滚动歌词
const W_PAUSED = 124;     // 暂停时缩回小胶囊（封面 + 静止波形）
// 歌词可视区宽度 = 长条 - 左右内边距(12×2) - 封面(22) - 两个间距(8×2) - 波形(20)
const MARQUEE_W = W_MUSIC - 24 - 22 - 16 - 20;
const MARQUEE_SPEED = 45;   // 滚动速度（逻辑像素/秒）
const MARQUEE_GAP = 28;     // 一圈滚完到下一圈之间的间隔
const W_EXPANDED = 424;
const H_COLLAPSED = 34;
const H_EXPANDED = 210;
const GAP_TOP = 3;              // 胶囊距屏幕顶部的距离（顶栏 40 高，34 高胶囊正好居中）
const COLLAPSE_DELAY_MS = 1500;
const TICK_MS = 100;

const WEEKDAYS = '一二三四五六日';

// 开发用开关：只有这个文件存在时，下面的 /tmp/island-shot、/tmp/island-state
// 才会生效（避免普通使用中因为遗留的临时文件把胶囊钉在某个状态）
const DEV_MARKER = '/tmp/island-dev';
const DEBUG_SHOT_MARKER = '/tmp/island-shot';
const DEBUG_STATE_MARKER = '/tmp/island-state';

// ---------------------------------------------------------------- 格式化

function fmtCost(value) {
    const number = Number(value ?? 0);
    if (number >= 0.01 || number <= 0)
        return `$${number.toFixed(2)}`;
    return `$${number.toFixed(4)}`;
}

function fmtTokens(count) {
    const value = Math.max(Number(count ?? 0), 0);
    if (value >= 1_000_000)
        return `${(value / 1_000_000).toFixed(2)}M`;
    if (value >= 1_000)
        return `${(value / 1_000).toFixed(1)}k`;
    return `${Math.round(value)}`;
}

function fmtClock(seconds) {
    if (seconds === null || seconds === undefined || !(seconds >= 0))
        return '--:--';
    const total = Math.floor(seconds);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    if (hours)
        return `${hours}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
    return `${minutes}:${String(secs).padStart(2, '0')}`;
}

function setText(label, text) {
    if (label && label.get_text() !== text)
        label.set_text(text);
}

function makeLabel(text, styleClass, {ellipsize = false} = {}) {
    const label = new St.Label({text, style_class: styleClass});
    if (ellipsize) {
        label.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        label.clutter_text.single_line_mode = true;
    }
    return label;
}

// ---------------------------------------------------------------- 灵动岛

export class Island {
    constructor(extension) {
        this._extension = extension;
        this._dir = extension.dir;

        this._expanded = false;
        this._pinned = false;
        this._hover = false;
        this._menuOpen = false;
        this._viewKey = null;
        this._morphTimer = 0;
        this._collapseTimer = 0;
        this._tickTimer = 0;

        this._music = null;
        this._lines = [];
        this._usage = null;
        this._balances = [];
        this._eqPhase = 0;
        this._scrubbing = false;
        this._miniLyricCycle = 0;
        this._marqueeTimer = 0;
        this._marqueeFrom = 0;
        this._marqueeTo = 0;
        this._marqueeStart = 0;
        this._shotting = false;

        this._pill = null;
        this._stack = null;
        this._activeView = null;
        this._views = {};
        this._media = null;
        this._usageWatcher = null;
        this._monitorsId = 0;
    }

    // ------------------------------------------------------------ 生命周期

    enable() {
        try {
            this._build();
            this._hideClock();

            this._media = new MediaWatcher({
                onMusic: music => this._applyMusic(music),
                onLyrics: lines => this._applyLyrics(lines),
                onCover: path => this._applyCover(path),
            });
            this._media.enable();

            const script = this._dir.get_child('api_usage.py');
            this._usageWatcher = new UsageWatcher({
                scriptPath: script.query_exists(null) ? script.get_path() : null,
                onData: data => this._applyUsage(data),
            });
            this._usageWatcher.enable();

            this._tickTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, TICK_MS, () => {
                this._tick();
                return GLib.SOURCE_CONTINUE;
            });

            this._monitorsId = Main.layoutManager.connect('monitors-changed', () => this._place(false));
            this._showView(this._stateKey(), 0);
            console.log(`[灵动岛] 已启用（${this._expanded ? '展开' : '收起'}态，${W_EXPANDED}x${H_EXPANDED}）`);
        } catch (error) {
            // 出错时把自己完整清理掉（包含恢复系统时钟），避免留下半成品
            try {
                this.disable();
            } catch {
                // 清理失败不掩盖原始错误
            }
            throw error;
        }
    }

    disable() {
        if (this._tickTimer) {
            GLib.source_remove(this._tickTimer);
            this._tickTimer = 0;
        }
        for (const timer of [this._morphTimer, this._collapseTimer, this._marqueeTimer]) {
            if (timer)
                GLib.source_remove(timer);
        }
        this._marqueeTimer = 0;
        this._morphTimer = 0;
        this._collapseTimer = 0;
        this._miniLyricCycle = 0;
        this._marqueeTimer = 0;
        this._marqueeFrom = 0;
        this._marqueeTo = 0;
        this._marqueeStart = 0;

        if (this._monitorsId) {
            Main.layoutManager.disconnect(this._monitorsId);
            this._monitorsId = 0;
        }

        this._media?.disable();
        this._usageWatcher?.disable();
        this._media = null;
        this._usageWatcher = null;

        this._restoreClock();

        if (this._menu) {
            this._menu.destroy();
            this._menu = null;
        }
        this._menuManager = null;

        if (this._pill) {
            Main.layoutManager.removeChrome(this._pill);
            this._pill.destroy();
            this._pill = null;
        }
        this._views = {};
    }

    // ------------------------------------------------------------ 界面构建

    _build() {
        this._pill = new St.BoxLayout({
            vertical: true,
            style_class: 'island',
            clip_to_allocation: true,
        });
        this._pill.reactive = true;
        this._pill.connect('enter-event', () => {
            this._setHover(true);
            return Clutter.EVENT_PROPAGATE;
        });
        this._pill.connect('leave-event', () => {
            this._setHover(false);
            return Clutter.EVENT_PROPAGATE;
        });
        this._pill.connect('button-press-event', (_actor, event) => this._onButtonPress(event));

        this._stack = new St.Widget({layout_manager: new Clutter.BinLayout()});
        this._pill.add_child(this._stack);

        this._buildMenu();
        this._views = {
            'false:idle': this._buildCollapsedIdle(),
            'false:playing': this._buildCollapsedMusic(),
            'false:paused': this._buildCollapsedPaused(),
            'true:playing': this._buildExpandedMusic(),
            'true:idle': this._buildExpandedIdle(),
        };
        // 展开态不区分播放/暂停（只是播放按钮图标不同）
        this._views['true:paused'] = this._views['true:playing'];
        // 四个视图常驻、只切换可见性：避免在动画过程中增删子节点
        for (const view of Object.values(this._views)) {
            view.visible = false;
            this._stack.add_child(view);
        }

        Main.layoutManager.addChrome(this._pill, {trackFullscreen: true});
    }

    _buildMenu() {
        this._menu = new PopupMenu.PopupMenu(this._pill, 0.5, St.Side.TOP);
        this._menu.actor.add_style_class_name('island-menu');

        const projectPath = this._readProjectPath();
        if (projectPath) {
            this._menu.addAction('打开贴纸设置…', () => {
                this._spawn(['python3', GLib.build_filenamev([projectPath, 'control.py'])]);
            });
        }
        this._menu.addAction('关闭灵动岛', () => this._disableSelf());

        this._menuManager = new PopupMenu.PopupMenuManager(this._pill);
        this._menuManager.addMenu(this._menu);
        this._menu.connect('open-state-changed', (_menu, open) => {
            this._menuOpen = open;
            if (open) {
                this._cancelCollapse();
                this._expand(true);
            } else {
                this._scheduleCollapse(400);
            }
        });
    }

    _readProjectPath() {
        try {
            const file = this._dir.get_child('project-path');
            if (!file.query_exists(null))
                return null;
            const [ok, contents] = GLib.file_get_contents(file.get_path());
            if (!ok)
                return null;
            const path = new TextDecoder().decode(contents).trim();
            return path && GLib.file_test(path, GLib.FileTest.IS_DIR) ? path : null;
        } catch {
            return null;
        }
    }

    _buildCollapsedIdle() {
        const box = new St.BoxLayout({style_class: 'island-collapsed'});
        box.x_expand = true;
        box.y_expand = true;
        this._miniClock = makeLabel('--:--', 'island-mini');
        this._miniClock.x_align = Clutter.ActorAlign.CENTER;
        this._miniClock.y_align = Clutter.ActorAlign.CENTER;
        box.add_child(this._miniClock);
        return box;
    }

    /** 暂停态：小胶囊（封面 + 静止波形），和从前的播放态一样 */
    _buildCollapsedPaused() {
        const box = new St.BoxLayout({
            style_class: 'island-collapsed',
            x_align: Clutter.ActorAlign.CENTER,
            y_align: Clutter.ActorAlign.CENTER,
        });
        box.x_expand = true;
        box.y_expand = true;

        this._pausedCover = new St.Bin({style_class: 'island-cover island-cover-mini'});
        this._pausedCoverNote = makeLabel('♪', 'island-cover-note');
        this._pausedCoverNote.x_align = Clutter.ActorAlign.CENTER;
        this._pausedCoverNote.y_align = Clutter.ActorAlign.CENTER;
        this._pausedCover.set_child(this._pausedCoverNote);
        box.add_child(this._pausedCover);

        const bars = new St.BoxLayout({style_class: 'island-eq', y_align: Clutter.ActorAlign.CENTER});
        const levels = [0.34, 0.58, 0.42, 0.66];
        for (const level of levels) {
            const bar = new St.Widget({style_class: 'island-eq-bar'});
            bar.set_size(3, Math.max(3, Math.round(14 * level)));
            bar.y_align = Clutter.ActorAlign.END;
            bars.add_child(bar);
        }
        box.add_child(bars);
        return box;
    }

    _buildCollapsedMusic() {
        const box = new St.BoxLayout({style_class: 'island-collapsed'});
        box.x_expand = true;
        box.y_expand = true;

        this._miniCover = new St.Bin({style_class: 'island-cover island-cover-mini'});
        this._miniCover.y_align = Clutter.ActorAlign.CENTER;
        this._miniCoverNote = makeLabel('♪', 'island-cover-note');
        this._miniCoverNote.x_align = Clutter.ActorAlign.CENTER;
        this._miniCoverNote.y_align = Clutter.ActorAlign.CENTER;
        this._miniCover.set_child(this._miniCoverNote);
        box.add_child(this._miniCover);

        // 滚动歌词：外面一层裁剪容器，里面一条不换行的文字
        this._marqueeBox = new St.Widget({style_class: 'island-marquee', clip_to_allocation: true, x_expand: true});
        this._marqueeBox.y_align = Clutter.ActorAlign.CENTER;
        this._miniLyric = new St.Label({text: '', style_class: 'island-mini-lyric'});
        this._miniLyric.clutter_text.single_line_mode = true;
        // 固定成可视区宽度：否则标签的首选宽度会把裁剪容器一起撑大
        this._miniLyric.set_width(MARQUEE_W);
        this._miniLyric.y_align = Clutter.ActorAlign.CENTER;
        this._miniLyricText = '';
        this._marqueeBox.add_child(this._miniLyric);
        box.add_child(this._marqueeBox);

        // 隐藏探针：不参与分配 → 内部 Pango 布局不受宽度约束，能量到真实文字宽度
        this._lyricProbe = new St.Label({text: '', style_class: 'island-mini-lyric'});
        this._lyricProbe.clutter_text.single_line_mode = true;
        this._lyricProbe.visible = false;
        box.add_child(this._lyricProbe);


        const bars = new St.BoxLayout({style_class: 'island-eq', y_align: Clutter.ActorAlign.CENTER});
        this._eqBars = [];
        for (let i = 0; i < 4; i++) {
            const bar = new St.Widget({style_class: 'island-eq-bar'});
            bar.y_align = Clutter.ActorAlign.END;
            this._eqBars.push(bar);
            bars.add_child(bar);
        }
        box.add_child(bars);
        return box;
    }

    // ------------------------------------------------------------ 滚动歌词

    _cancelLyricAnim() {
        for (const timer of [this._marqueeTimer, this._measureId]) {
            if (timer)
                GLib.source_remove(timer);
        }
        this._marqueeTimer = 0;
        this._measureId = 0;
        this._lyricToken = (this._lyricToken ?? 0) + 1;
        this._miniLyric?.remove_transition('translation-x');
        this._miniLyric?.remove_transition('opacity');
        this._miniLyricCycle = 0;
    }

    /** 收起态·播放中 这个视图当前是否可见（不可见时不要动动画，ease 会立即完成） */
    _lyricViewVisible() {
        return this._activeView === this._views['false:playing'] && this._miniLyric?.visible;
    }

    /**
     * 换句：先**居中固定**显示；如果这行放不下，停 1 秒后从居中位置向左滚动。
     */
    _updateMiniLyric(text) {
        if (!this._lyricViewVisible()) {
            this._miniLyricText = '';        // 等视图可见时重新处理
            return;
        }
        if (text === this._miniLyricText)
            return;
        this._miniLyricText = text;
        this._cancelLyricAnim();
        this._miniLyric.set_text(text);
        this._miniLyric.x = 0;
        this._miniLyric.translation_x = 0;
        this._miniLyric.opacity = 255;

        // 等一帧再量（刚 set_text 时布局可能还没更新）
        const token = ++this._lyricToken;
        this._measureId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 50, () => {
            this._measureId = 0;
            if (token === this._lyricToken)
                this._layoutLyric();
            return GLib.SOURCE_REMOVE;
        });
    }

    /** 量宽 → 居中摆放；太长则停 1 秒后开始滚动 */
    _layoutLyric() {
        if (!this._lyricViewVisible())
            return;
        const textWidth = this._measureLyricWidth();
        this._miniLyric.translation_x = Math.round((MARQUEE_W - textWidth) / 2);
        if (GLib.file_test(DEV_MARKER, GLib.FileTest.EXISTS))
            console.log(`[灵动岛][dbg] 歌词 "${this._miniLyricText}" 宽=${textWidth} ` +
                `可视=${MARQUEE_W} 居中=${this._miniLyric.translation_x}`);
        if (textWidth <= MARQUEE_W)
            return;
        this._marqueeTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 1000, () => {
            this._marqueeTimer = 0;
            this._startMarquee(false);
            return GLib.SOURCE_REMOVE;
        });
    }

    /** 滚动：fromRight=false 时从当前位置（居中）开始；之后的圈从右侧进入 */
    _startMarquee(fromRight) {
        if (!this._lyricViewVisible())
            return;
        const textWidth = this._measureLyricWidth();
        if (textWidth <= MARQUEE_W)
            return;
        if (this._marqueeTimer) {
            GLib.source_remove(this._marqueeTimer);
            this._marqueeTimer = 0;
        }
        this._miniLyricCycle = (this._miniLyricCycle ?? 0) + 1;
        this._marqueeFrom = fromRight ? MARQUEE_W + MARQUEE_GAP : this._miniLyric.translation_x;
        this._marqueeTo = -textWidth;
        this._marqueeStart = GLib.get_monotonic_time() / 1e6;
        this._marqueeTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 16, () => this._marqueeTick());
    }

    _marqueeTick() {
        if (!this._lyricViewVisible()) {
            this._marqueeTimer = 0;
            return GLib.SOURCE_REMOVE;
        }
        const elapsed = GLib.get_monotonic_time() / 1e6 - this._marqueeStart;
        const total = (this._marqueeFrom - this._marqueeTo) / MARQUEE_SPEED;
        if (elapsed >= total) {
            this._marqueeTimer = 0;
            // 下一圈从右侧接上；用 idle 避免同步递归
            GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
                this._startMarquee(true);
                return GLib.SOURCE_REMOVE;
            });
            return GLib.SOURCE_REMOVE;
        }
        this._miniLyric.translation_x = this._marqueeFrom - MARQUEE_SPEED * elapsed;
        return GLib.SOURCE_CONTINUE;
    }

    /** 文字真实宽度：用隐藏探针量（可见标签的布局会被固定宽度约束，量不准） */
    _measureLyricWidth() {
        if (!this._lyricProbe)
            return 0;
        try {
            if (this._lyricProbe.get_text() !== this._miniLyricText)
                this._lyricProbe.set_text(this._miniLyricText);
            return this._lyricProbe.get_preferred_width(-1)[1];
        } catch {
            return 0;
        }
    }

    _buildExpandedMusic() {
        const box = new St.BoxLayout({vertical: true, style_class: 'island-expanded'});
        box.x_expand = true;
        box.y_expand = true;

        // 第一行：封面 + 歌名 / 歌手 + 控制按钮
        const row1 = new St.BoxLayout({style_class: 'island-row'});
        this._cover = new St.Bin({style_class: 'island-cover island-cover-large'});
        this._coverNote = makeLabel('♪', 'island-cover-note');
        this._coverNote.x_align = Clutter.ActorAlign.CENTER;
        this._coverNote.y_align = Clutter.ActorAlign.CENTER;
        this._cover.set_child(this._coverNote);

        const meta = new St.BoxLayout({vertical: true, style_class: 'island-meta', x_expand: true, y_align: Clutter.ActorAlign.CENTER});
        this._title = makeLabel('', 'island-title', {ellipsize: true});
        this._artist = makeLabel('', 'island-sub', {ellipsize: true});
        meta.add_child(this._title);
        meta.add_child(this._artist);

        const controls = new St.BoxLayout({style_class: 'island-controls', y_align: Clutter.ActorAlign.CENTER});
        this._prevBtn = this._makeButton('media-skip-backward-symbolic', 'island-btn', () => this._media?.previous());
        this._playBtn = this._makeButton('media-playback-start-symbolic', 'island-play', () => this._media?.playPause());
        this._playIcon = this._playBtn.child;
        this._nextBtn = this._makeButton('media-skip-forward-symbolic', 'island-btn', () => this._media?.next());
        controls.add_child(this._prevBtn);
        controls.add_child(this._playBtn);
        controls.add_child(this._nextBtn);

        row1.add_child(this._cover);
        row1.add_child(meta);
        row1.add_child(controls);

        // 第二行：进度条 + 时间
        const row2 = new St.BoxLayout({style_class: 'island-row'});
        this._posLabel = makeLabel('--:--', 'island-foot island-time');
        this._posLabel.x_align = Clutter.ActorAlign.END;
        this._lenLabel = makeLabel('--:--', 'island-foot island-time');
        this._lenLabel.x_align = Clutter.ActorAlign.START;
        this._seekTrack = new St.DrawingArea({
            style_class: 'island-seek',
            reactive: true,
            x_expand: true,
        });
        this._seekTrack.set_height(14);
        this._seekFraction = 0;
        this._seekTrack.connect('repaint', area => this._paintSeek(area));
        this._seekTrack.connect('button-press-event', (_actor, event) => this._onSeekPress(event));
        this._seekTrack.connect('motion-event', (_actor, event) => this._onSeekMotion(event));
        this._seekTrack.connect('button-release-event', (_actor, event) => this._onSeekRelease(event));
        row2.add_child(this._posLabel);
        row2.add_child(this._seekTrack);
        row2.add_child(this._lenLabel);

        // 第三行：当前歌词
        const row3 = new St.BoxLayout({style_class: 'island-row'});
        const note = makeLabel('♪', 'island-lyric-icon');
        note.y_align = Clutter.ActorAlign.CENTER;
        this._lyric = makeLabel('', 'island-lyric', {ellipsize: true});
        this._lyric.x_expand = true;
        this._lyric.y_align = Clutter.ActorAlign.CENTER;
        row3.add_child(note);
        row3.add_child(this._lyric);

        // 第四行：时钟 + 今日花费 / 余额
        const footer = new St.BoxLayout({style_class: 'island-row'});
        this._footClock = makeLabel('', 'island-foot');
        this._footClock.reactive = true;
        this._footClock.connect('button-release-event', () => {
            this._openDateMenu();
            return Clutter.EVENT_STOP;
        });
        this._footUsage = makeLabel('', 'island-foot', {ellipsize: true});
        this._footUsage.x_expand = true;
        this._footUsage.x_align = Clutter.ActorAlign.END;
        this._footBalance = makeLabel('', 'island-balance');
        footer.add_child(this._footClock);
        footer.add_child(this._footUsage);
        footer.add_child(this._footBalance);

        box.add_child(row1);
        box.add_child(row2);
        box.add_child(row3);
        box.add_child(footer);
        return box;
    }

    _buildExpandedIdle() {
        const box = new St.BoxLayout({vertical: true, style_class: 'island-expanded island-idle'});
        box.x_expand = true;
        box.y_expand = true;

        // 第一行：大时钟 + 日期 ｜ 今日花费
        const row1 = new St.BoxLayout({style_class: 'island-row island-gap-16'});
        const left = new St.BoxLayout({vertical: true, x_expand: true, y_align: Clutter.ActorAlign.CENTER});
        this._bigClock = makeLabel('--:--', 'island-clock');
        this._bigClock.reactive = true;
        this._bigClock.connect('button-release-event', () => {
            this._openDateMenu();
            return Clutter.EVENT_STOP;
        });
        this._bigDate = makeLabel('', 'island-date');
        left.add_child(this._bigClock);
        left.add_child(this._bigDate);

        const right = new St.BoxLayout({vertical: true, style_class: 'island-api', y_align: Clutter.ActorAlign.CENTER});
        this._apiToday = makeLabel('今日 —', 'island-api-today');
        this._apiToday.x_align = Clutter.ActorAlign.END;
        this._apiTokens = makeLabel('', 'island-sub');
        this._apiTokens.x_align = Clutter.ActorAlign.END;
        this._apiTotal = makeLabel('', 'island-sub');
        this._apiTotal.x_align = Clutter.ActorAlign.END;
        right.add_child(this._apiToday);
        right.add_child(this._apiTokens);
        right.add_child(this._apiTotal);

        row1.add_child(left);
        row1.add_child(right);

        // 第二行：近 7 天柱状图
        const row2 = new St.BoxLayout({style_class: 'island-row'});
        const barsLabel = makeLabel('近 7 天', 'island-foot');
        barsLabel.y_align = Clutter.ActorAlign.CENTER;
        const bars = new St.BoxLayout({style_class: 'island-bars'});
        this._usageBars = [];
        for (let i = 0; i < 7; i++) {
            const bar = new St.Widget({style_class: 'island-bar'});
            bar.y_align = Clutter.ActorAlign.END;
            this._usageBars.push(bar);
            bars.add_child(bar);
        }
        row2.add_child(barsLabel);
        row2.add_child(bars);

        // 第三行：各厂商余额
        const row3 = new St.BoxLayout({style_class: 'island-row'});
        this._idleBalance = makeLabel('', 'island-balance', {ellipsize: true});
        this._idleBalance.x_expand = true;
        row3.add_child(this._idleBalance);

        box.add_child(row1);
        box.add_child(row2);
        box.add_child(row3);
        return box;
    }

    _makeButton(iconName, styleClass, callback) {
        const icon = new St.Icon({icon_name: iconName, icon_size: 14});
        const button = new St.Button({style_class: styleClass, child: icon, reactive: true});
        button.connect('clicked', () => callback());
        return button;
    }

    // ------------------------------------------------------------ 尺寸 / 动画

    /** 当前该显示哪个状态：idle（没播放）/ playing（播放中）/ paused（暂停） */
    _stateKind() {
        if (!this._music)
            return 'idle';
        return this._music.status === 'Playing' ? 'playing' : 'paused';
    }

    _stateKey() {
        return `${this._expanded}:${this._stateKind()}`;
    }

    _sizeFor(key) {
        const [expanded, kind] = key.split(':');
        if (expanded === 'true')
            return [W_EXPANDED, H_EXPANDED];
        if (kind === 'playing')
            return [W_MUSIC, H_COLLAPSED];
        if (kind === 'paused')
            return [W_PAUSED, H_COLLAPSED];
        return [W_IDLE, H_COLLAPSED];
    }

    _pillPosition(key, width, height) {
        const monitor = Main.layoutManager.primaryMonitor;
        const panelHeight = Main.layoutManager.panelBox.height;
        const expanded = key.startsWith('true');
        const x = monitor.x + Math.round((monitor.width - width) / 2);
        const y = monitor.y + (expanded ? GAP_TOP : Math.round((panelHeight - height) / 2));
        return [x, y];
    }

    _showView(key, duration = 260) {
        if (!this._pill)
            return;

        const old = this._activeView;
        const [width, height] = this._sizeFor(key);
        const [x, y] = this._pillPosition(key, width, height);

        if (this._morphTimer) {
            GLib.source_remove(this._morphTimer);
            this._morphTimer = 0;
        }

        if (key === this._viewKey) {
            this._pill.set_position(x, y);
            this._pill.set_size(width, height);
            return;
        }

        if (key !== 'false:playing')
            this._cancelLyricAnim();

        const fade = Math.min(90, duration);
        if (old && duration > 0)
            old.ease({opacity: 0, duration: fade});

        if (duration > 0) {
            this._pill.ease({
                x, y, width, height, duration,
                mode: Clutter.AnimationMode.EASE_OUT_CUBIC,
            });
        } else {
            this._pill.set_position(x, y);
            this._pill.set_size(width, height);
        }

        const swap = () => {
            this._morphTimer = 0;
            this._setView(key);
            this._activeView?.ease({opacity: 255, duration: 180});
            return GLib.SOURCE_REMOVE;
        };
        this._morphTimer = fade > 0 ? GLib.timeout_add(GLib.PRIORITY_DEFAULT, fade, swap) : (swap(), 0);
        this._viewKey = key;
    }

    _setView(key) {
        const view = this._views[key];
        if (!view)
            return;
        for (const candidate of new Set(Object.values(this._views)))
            candidate.visible = candidate === view;
        view.opacity = 0;
        this._activeView = view;
    }

    _place(expanded) {
        const key = `${expanded}:${this._music !== null}`;
        const [width, height] = this._sizeFor(key);
        const [x, y] = this._pillPosition(key, width, height);
        this._pill.set_position(x, y);
        this._pill.set_size(width, height);
    }

    // ------------------------------------------------------------ 交互

    _setHover(hovering) {
        this._hover = hovering;
        if (hovering) {
            this._cancelCollapse();
            this._expand(true);
        } else {
            this._scheduleCollapse();
        }
    }

    _expand(expanded) {
        this._expanded = expanded;
        this._showView(this._stateKey());
    }

    _scheduleCollapse(delay = COLLAPSE_DELAY_MS) {
        this._cancelCollapse();
        if (this._pinned || this._menuOpen)
            return;
        this._collapseTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, delay, () => {
            this._collapseTimer = 0;
            if (!this._pinned && !this._hover && !this._menuOpen)
                this._expand(false);
            return GLib.SOURCE_REMOVE;
        });
    }

    _cancelCollapse() {
        if (this._collapseTimer) {
            GLib.source_remove(this._collapseTimer);
            this._collapseTimer = 0;
        }
    }

    _onButtonPress(event) {
        const button = event.get_button();
        if (button === 3) {
            this._menu?.toggle();
            return Clutter.EVENT_STOP;
        }
        if (button === 1 && event.get_source() === this._pill) {
            // 单击胶囊空白处：钉住 / 取消钉住
            this._pinned = !this._pinned;
            if (this._pinned) {
                this._cancelCollapse();
                this._expand(true);
            } else {
                this._scheduleCollapse(600);
            }
            return Clutter.EVENT_STOP;
        }
        return Clutter.EVENT_PROPAGATE;
    }

    _onSeekPress(event) {
        if (!this._music || !(this._music.length > 0))
            return Clutter.EVENT_PROPAGATE;
        this._scrubbing = true;
        this._updateScrub(event);
        return Clutter.EVENT_STOP;
    }

    _onSeekMotion(event) {
        if (!this._scrubbing)
            return Clutter.EVENT_PROPAGATE;
        this._updateScrub(event);
        return Clutter.EVENT_STOP;
    }

    _onSeekRelease(event) {
        if (!this._scrubbing)
            return Clutter.EVENT_PROPAGATE;
        this._scrubbing = false;
        const fraction = this._fractionFor(event);
        if (fraction !== null && this._music?.length > 0)
            this._media?.seekTo(fraction * this._music.length);
        return Clutter.EVENT_STOP;
    }

    _fractionFor(event) {
        const trackWidth = this._seekTrack.width;
        if (!trackWidth)
            return null;
        const [x] = event.get_coords();
        const [trackX] = this._seekTrack.get_transformed_position();
        return Math.max(0, Math.min(1, (x - trackX) / trackWidth));
    }

    _updateScrub(event) {
        const fraction = this._fractionFor(event);
        if (fraction === null || !(this._music?.length > 0))
            return;
        this._setSeekFill(fraction);
        setText(this._posLabel, fmtClock(fraction * this._music.length));
    }

    _setSeekFill(fraction) {
        this._seekFraction = Math.max(0, Math.min(1, Number.isFinite(fraction) ? fraction : 0));
        this._seekTrack?.queue_repaint();
    }

    /** 用 cairo 画进度条：底槽 + 已完成段（避免子控件反馈进首选宽度）。 */
    _paintSeek(area) {
        try {
            const cr = area.get_context();
            const [width, height] = area.get_surface_size();
            const barHeight = 6;
            const y = (height - barHeight) / 2;

            const rounded = (x, y0, w, h, r) => {
                r = Math.max(0, Math.min(r, w / 2, h / 2));
                cr.newSubPath();
                cr.arc(x + w - r, y0 + r, r, -Math.PI / 2, 0);
                cr.arc(x + w - r, y0 + h - r, r, 0, Math.PI / 2);
                cr.arc(x + r, y0 + h - r, r, Math.PI / 2, Math.PI);
                cr.arc(x + r, y0 + r, r, Math.PI, 3 * Math.PI / 2);
                cr.closePath();
            };

            cr.setSourceRGBA(0.55, 0.57, 0.63, 0.22);
            rounded(0, y, width, barHeight, barHeight / 2);
            cr.fill();

            if (this._seekFraction > 0.004) {
                cr.setSourceRGBA(1, 1, 1, 1);
                rounded(0, y, Math.max(barHeight, width * this._seekFraction), barHeight, barHeight / 2);
                cr.fill();
            }
            cr.$dispose();
        } catch (error) {
            console.warn(`[灵动岛] 画进度条失败: ${error.message}`);
        }
    }

    // ------------------------------------------------------------ 数据回调

    _applyMusic(music) {
        const hadMusic = this._music !== null;
        this._music = music ?? null;

        if (music) {
            setText(this._title, music.title || '未知歌曲');
            const subtitle = [music.artist, music.album].filter(Boolean).join(' · ');
            setText(this._artist, subtitle || '未知艺术家');
            this._playIcon.icon_name = music.status === 'Playing'
                ? 'media-playback-pause-symbolic' : 'media-playback-start-symbolic';
            for (const [button, enabled] of [[this._prevBtn, music.canPrev], [this._nextBtn, music.canNext]]) {
                button.reactive = enabled;
                button.opacity = enabled ? 255 : 110;
            }
        }

        if (!music) {
            this._cancelLyricAnim();
            this._miniLyricText = '';
            this._miniLyric?.set_text('');
        }

        const key = this._stateKey();
        if (hadMusic !== (music !== null) || key !== this._viewKey)
            this._showView(key);
    }

    _applyLyrics(lines) {
        this._lines = lines ?? [];
    }

    _applyCover(path) {
        const uri = path ? GLib.filename_to_uri(path, null) : null;
        for (const bin of [this._cover, this._miniCover, this._pausedCover]) {
            if (!bin)
                continue;
            const note = bin === this._cover ? this._coverNote
                : bin === this._miniCover ? this._miniCoverNote : this._pausedCoverNote;
            if (uri) {
                bin.set_style(`background-image: url("${uri}");`);
                note.visible = false;
            } else {
                bin.set_style(null);
                note.visible = true;
            }
        }
    }

    _applyUsage(data) {
        this._usage = data?.usage ?? null;
        this._balances = data?.balances ?? [];
        this._refreshUsageLabels();
    }

    _refreshUsageLabels() {
        const usage = this._usage ?? {};
        const today = fmtCost(usage.today_cost);
        const tokens = fmtTokens(usage.today_tokens);
        setText(this._apiToday, `今日 ${today}`);
        setText(this._apiTokens, `${tokens} tokens · ${usage.today_messages ?? 0} 条回复`);
        setText(this._apiTotal, `累计 ${fmtCost(usage.total_cost)} · ${fmtTokens(usage.total_tokens)} tokens`);
        setText(this._footUsage, `今日 ${today} · ${tokens} tok`);

        const balanceText = this._balances.length
            ? this._balances.map(item => item.error
                ? `${item.label} —`
                : `${item.label} ${this._formatBalance(item)}`).join(' · ')
            : '';
        setText(this._footBalance, balanceText);
        setText(this._idleBalance, balanceText);

        const daily = Array.isArray(usage.daily) ? usage.daily.slice(-7) : [];
        const peak = Math.max(1e-9, ...daily.map(value => Number(value) || 0));
        this._usageBars?.forEach((bar, index) => {
            const value = Number(daily[index] ?? 0);
            const maxHeight = 30;
            let height = daily.length ? (value / peak) * maxHeight : 2;
            if (!Number.isFinite(height)) {
                console.warn(`[灵动岛] 柱状图非法高度: value=${value} peak=${peak}`);
                height = 2;
            }
            height = Math.max(2, height);
            bar.set_size(22, height);
            bar.y_align = Clutter.ActorAlign.END;
            bar.remove_style_class_name('island-bar-today');
            if (index === daily.length - 1)
                bar.add_style_class_name('island-bar-today');
        });
    }

    _formatBalance(item) {
        const symbols = {CNY: '¥', RMB: '¥', USD: '$', EUR: '€'};
        const symbol = symbols[String(item.currency ?? '').toUpperCase()] ?? '';
        const number = Number(item.balance);
        const text = Number.isFinite(number) ? number.toFixed(2) : String(item.balance ?? '—');
        return symbol ? `${symbol}${text}` : `${text} ${item.currency ?? ''}`.trim();
    }

    // ------------------------------------------------------------ 每帧刷新

    _tick() {
        const now = new Date();
        const clock = `${now.getHours()}:${String(now.getMinutes()).padStart(2, '0')}`;
        setText(this._miniClock, clock);
        setText(this._footClock, clock);
        setText(this._bigClock, clock);
        setText(this._bigDate, `${now.getMonth() + 1}月${now.getDate()}日 周${WEEKDAYS[now.getDay() === 0 ? 6 : now.getDay() - 1]}`);

        if (this._music) {
            const position = this._media?.position ?? 0;
            const length = this._music.length ?? 0;

            if (!this._scrubbing) {
                setText(this._posLabel, fmtClock(position));
                setText(this._lenLabel, fmtClock(length));
                this._setSeekFill(length > 0 ? position / length : 0);
            }

            let line = lineAt(this._lines, position) ||
                [this._music.title, this._music.artist].filter(Boolean).join(' · ');
            // 开发用：/tmp/island-test-lyric 的内容会覆盖当前歌词（测滚动用）
            if (GLib.file_test(DEV_MARKER, GLib.FileTest.EXISTS) &&
                GLib.file_test('/tmp/island-test-lyric', GLib.FileTest.EXISTS)) {
                const [ok, contents] = GLib.file_get_contents('/tmp/island-test-lyric');
                if (ok)
                    line = new TextDecoder().decode(contents).trim() || line;
            }
            setText(this._lyric, line);
            if (this._music.status === 'Playing')
                this._updateMiniLyric(line);
            else
                this._cancelLyricAnim();

            const playing = this._music.status === 'Playing';
            this._eqPhase += playing ? 0.35 : 0;
            this._eqBars?.forEach((bar, index) => {
                const level = playing
                    ? 0.30 + 0.70 * (0.5 + 0.5 * Math.sin(this._eqPhase * 2.6 + index * 1.15))
                    : [0.34, 0.58, 0.42, 0.66][index % 4];
                let value = level;
                if (!Number.isFinite(value)) {
                    this._eqPhase = 0;
                    console.warn(`[灵动岛] 波形非法高度: ${value}`);
                    value = 0.5;
                }
                bar.set_size(3, Math.max(3, Math.round(14 * value)));
                bar.y_align = Clutter.ActorAlign.END;
            });
        }

        if (GLib.file_test(DEV_MARKER, GLib.FileTest.EXISTS)) {
            if (GLib.file_test(DEBUG_SHOT_MARKER, GLib.FileTest.EXISTS))
                this._debugScreenshot();
            if (GLib.file_test(DEBUG_STATE_MARKER, GLib.FileTest.EXISTS))
                this._debugState();
        }
    }

    /** 开发用：按 /tmp/island-state 的内容强制展开 / 收起。 */
    _debugState() {
        try {
            const [ok, contents] = GLib.file_get_contents(DEBUG_STATE_MARKER);
            if (!ok)
                return;
            const state = new TextDecoder().decode(contents).trim();
            if (state === 'expanded' && !this._expanded) {
                this._pinned = true;
                this._expand(true);
            } else if (state === 'collapsed' && this._expanded) {
                this._pinned = false;
                this._expand(false);
            }
        } catch {
            // 忽略
        }
    }

    // ------------------------------------------------------------ 时钟 / 系统

    _hideClock() {
        this._dateMenu = Main.panel.statusArea.dateMenu ?? null;
        this._clockWasVisible = this._dateMenu?.container?.visible ?? false;
        if (this._clockWasVisible)
            this._dateMenu.container.hide();
    }

    _restoreClock() {
        if (this._dateMenu && this._clockWasVisible)
            this._dateMenu.container.show();
        this._dateMenu = null;
    }

    _openDateMenu() {
        try {
            this._dateMenu?.menu?.open();
        } catch (error) {
            console.warn(`[灵动岛] 打开日历失败: ${error.message}`);
        }
    }

    _spawn(argv) {
        try {
            Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE);
        } catch (error) {
            console.warn(`[灵动岛] 启动失败: ${error.message}`);
        }
    }

    _disableSelf() {
        try {
            Main.extensionManager.disableExtension(this._extension.uuid);
        } catch (error) {
            console.warn(`[灵动岛] 关闭扩展失败: ${error.message}`);
        }
    }

    // ------------------------------------------------------------ 调试截图

    /** 开发用：/tmp/island-shot 里写路径 → 直接在 Shell 内部截一张全屏图。 */
    async _debugScreenshot() {
        if (this._shotting)
            return;
        this._shotting = true;

        let path = '/tmp/island-shot.png';
        try {
            const [ok, contents] = GLib.file_get_contents(DEBUG_SHOT_MARKER);
            if (ok) {
                const text = new TextDecoder().decode(contents).trim();
                if (text)
                    path = text;
            }
        } catch {
            // 用默认路径
        }
        try {
            Gio.File.new_for_path(DEBUG_SHOT_MARKER).delete(null);
        } catch {
            // 删除失败也没关系
        }

        try {
            const shooter = new Shell.Screenshot();
            const [content] = await shooter.screenshot_stage_to_content();
            const stream = Gio.MemoryOutputStream.new_resizable();
            await Shell.Screenshot.composite_to_stream(
                content.get_texture(), 0, 0, -1, -1, 1, null, 0, 0, 1, stream);
            stream.close(null);

            const file = Gio.File.new_for_path(path);
            file.replace_contents_bytes_async(stream.steal_as_bytes(), null, false,
                Gio.FileCreateFlags.REPLACE_DESTINATION, null, (target, res) => {
                    try {
                        target.replace_contents_finish(res);
                        console.log(`[灵动岛] 截图已保存: ${path}`);
                    } catch (error) {
                        console.error(`[灵动岛] 截图保存失败: ${error.message}`);
                    }
                });
        } catch (error) {
            console.error(`[灵动岛] 截图失败: ${error.message}`);
        } finally {
            this._shotting = false;
        }
    }
}
