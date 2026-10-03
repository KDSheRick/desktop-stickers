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
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';
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
const NOTIFY_HOLD_MS = 5000; // 通知在胶囊里停留的时长
const W_ACTIVITY = 190;     // 收起态·实时活动（计时器/闹钟/秒表）的宽度
const ACTIVITY_FILE = GLib.build_filenamev([
    GLib.get_user_config_dir(), 'sysstickers', 'activities.json']);
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
        this._activity = null;          // 计时器 / 闹钟 / 秒表（同一时刻只保留一个）
        this._activityExpanded = false; // 响铃时自动展开
        this._dbusRetryAt = 0;
        this._activityRingSource = null;
        this._notifySource = null;
        this._dbusId = 0;
        this._notificationActive = false;
        this._notifyExpanded = false;
        this._notifyQueue = [];
        this._notifyItem = null;
        this._notifyHoldTimer = 0;
        this._sourceSignals = new Map();
        this._traySignals = [];
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
            this._watchNotifications();
            this._restoreActivities();
            this._exportDbus();
            this._refreshActivityView();
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

    /** 停掉一个计时器/回调但不让异常中断后续清理 */
    _safe(fn, what) {
        try {
            fn();
        } catch (error) {
            console.warn(`[灵动岛] 清理 ${what} 失败: ${error.message}`);
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
        this._activity = null;          // 计时器 / 闹钟 / 秒表（同一时刻只保留一个）
        this._activityExpanded = false; // 响铃时自动展开
        this._dbusRetryAt = 0;
        this._activityRingSource = null;
        this._notifySource = null;
        this._dbusId = 0;
        this._notificationActive = false;
        this._notifyExpanded = false;
        this._notifyQueue = [];
        this._notifyItem = null;
        this._notifyHoldTimer = 0;
        this._sourceSignals = new Map();
        this._traySignals = [];
        this._marqueeTimer = 0;
        this._marqueeFrom = 0;
        this._marqueeTo = 0;
        this._marqueeStart = 0;

        if (this._monitorsId) {
            Main.layoutManager.disconnect(this._monitorsId);
            this._monitorsId = 0;
        }

        this._safe(() => this._unwatchNotifications(), '通知监听');
        if (this._dbusId) {
            try {
                Gio.DBus.session.unregister_object(this._dbusId);
            } catch (error) {
                console.warn(`[灵动岛] 注销计时器接口失败: ${error.message}`);
            }
            this._dbusId = 0;
        }
        if (this._activityRingSource) {
            GLib.source_remove(this._activityRingSource);
            this._activityRingSource = 0;
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
            'false:notify': this._buildNotifyView(),
            'false:activity': this._buildCollapsedActivity(),
            'true:playing': this._buildExpandedMusic(),
            'true:idle': this._buildExpandedIdle(),
        };
        this._views['true:notify'] = this._buildNotifyCard();
        this._views['true:activity'] = this._buildExpandedActivity();
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

        // 计时器 / 闹钟 / 秒表（每次弹出时重建，带当前状态）
        const timerMenu = new PopupMenu.PopupSubMenuMenuItem('计时器');
        for (const minutes of [5, 10, 15, 25, 45])
            timerMenu.menu.addAction(`${minutes} 分钟`, () => this.setTimer(minutes));

        const alarmMenu = new PopupMenu.PopupSubMenuMenuItem('闹钟');
        alarmMenu.menu.addAction('30 分钟后', () => this._setAlarmIn(30));
        alarmMenu.menu.addAction('1 小时后', () => this._setAlarmIn(60));
        alarmMenu.menu.addAction('明天 9:00', () => this.setAlarm('09:00'));

        const stopwatchMenu = new PopupMenu.PopupSubMenuMenuItem('秒表');
        stopwatchMenu.menu.addAction('开始 / 暂停', () => {
            if (this._activity?.kind === 'stopwatch')
                this.toggleStopwatch();
            else
                this.startStopwatch();
        });
        stopwatchMenu.menu.addAction('重置', () => this.resetStopwatch());

        this._menu.addMenuItem(timerMenu);
        this._menu.addMenuItem(alarmMenu);
        this._menu.addMenuItem(stopwatchMenu);
        if (this._activity) {
            const cancel = new PopupMenu.PopupMenuItem(
                this._activity.ringing ? '停止响铃' : '取消计时 / 闹钟 / 秒表');
            cancel.connect('activate', () => this.clearActivity());
            this._menu.addMenuItem(cancel);
        }
        this._menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

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
        // 横向 BoxLayout 里 x_align 不会居中，用两侧弹性空白顶到中间
        box.add_child(new St.Widget({x_expand: true}));
        this._miniClock = makeLabel('--:--', 'island-mini');
        this._miniClock.y_align = Clutter.ActorAlign.CENTER;
        box.add_child(this._miniClock);
        box.add_child(new St.Widget({x_expand: true}));
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
        box.add_child(new St.Widget({x_expand: true}));
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
        box.add_child(new St.Widget({x_expand: true}));
        return box;
    }

    /** 收起态·实时活动：⏱ 24:59 */
    _buildCollapsedActivity() {
        const box = new St.BoxLayout({style_class: 'island-collapsed'});
        box.x_expand = true;
        box.y_expand = true;
        box.add_child(new St.Widget({x_expand: true}));
        this._actIcon = new St.Icon({style_class: 'island-act-icon', icon_size: 16});
        this._actIcon.y_align = Clutter.ActorAlign.CENTER;
        box.add_child(this._actIcon);
        this._actText = makeLabel('', 'island-act-text');
        this._actText.y_align = Clutter.ActorAlign.CENTER;
        box.add_child(this._actText);
        box.add_child(new St.Widget({x_expand: true}));
        return box;
    }

    /** 展开态·实时活动卡片：大号倒计时 + 说明 + 三个按钮 */
    _buildExpandedActivity() {
        const body = new St.BoxLayout({vertical: true, style_class: 'island-expanded'});
        body.x_expand = true;
        body.y_expand = true;

        this._actBig = makeLabel('', 'island-act-big');
        this._actBig.x_align = Clutter.ActorAlign.CENTER;
        this._actSub = makeLabel('', 'island-sub');
        this._actSub.x_align = Clutter.ActorAlign.CENTER;

        const row = new St.BoxLayout({style_class: 'island-row', y_align: Clutter.ActorAlign.CENTER});
        row.x_expand = true;
        row.add_child(new St.Widget({x_expand: true}));
        this._actBtn1 = this._activityButton('', 1);
        this._actBtn2 = this._activityButton('', 2);
        this._actBtn3 = this._activityButton('', 3);
        row.add_child(this._actBtn1);
        row.add_child(this._actBtn2);
        row.add_child(this._actBtn3);
        row.add_child(new St.Widget({x_expand: true}));

        body.add_child(new St.Widget({y_expand: true}));
        body.add_child(this._actBig);
        body.add_child(this._actSub);
        body.add_child(new St.Widget({y_expand: true}));
        body.add_child(row);
        return body;
    }

    _activityButton(text, index) {
        const button = new St.Button({style_class: 'island-act-btn'});
        button.set_child(makeLabel(text, 'island-act-btn-label'));
        button.connect('clicked', () => this._activityAction(index));
        return button;
    }

    /** 展开卡片的按钮：按当前活动状态决定文案与动作 */
    _activityAction(index) {
        const act = this._activity;
        if (!act)
            return;
        if (act.kind === 'timer') {
            if (index === 1)
                act.ringing ? this.clearActivity() : this.toggleTimer();
            else if (index === 2)
                this.addTimerMinute();
            else
                this.clearActivity();
        } else if (act.kind === 'alarm') {
            if (index === 1)
                this.snoozeAlarm(5);
            else if (index === 2)
                this.clearActivity();
        } else if (act.kind === 'stopwatch') {
            if (index === 1)
                this.toggleStopwatch();
            else if (index === 2)
                this.resetStopwatch();
            else
                this.clearActivity();
        }
    }

    _refreshActivityView() {
        const act = this._activity;
        if (!this._actText)
            return;

        setText(this._actText, act ? this.activityText() : '');
        if (this._actIcon)
            this._actIcon.icon_name = this.activityIcon();
        setText(this._actBig, act ? this.activityText() : '');
        setText(this._actSub, act ? this.activitySubtitle() : '');

        const specs = [];
        if (act?.kind === 'timer') {
            if (act.ringing)
                specs.push(['停止', true], ['+1 分钟', true], ['', false]);
            else
                specs.push([act.paused ? '继续' : '暂停', true], ['+1 分钟', true], ['取消', true]);
        } else if (act?.kind === 'alarm') {
            specs.push(['贪睡 5 分钟', true], ['关闭', true], ['', false]);
        } else if (act?.kind === 'stopwatch') {
            specs.push([act.running ? '暂停' : '继续', true], ['重置', true], ['取消', true]);
        } else {
            specs.push(['', false], ['', false], ['', false]);
        }
        for (const [index, button] of [this._actBtn1, this._actBtn2, this._actBtn3].entries()) {
            const [text, visible] = specs[index];
            button.visible = visible;
            if (visible)
                setText(button.get_child(), text);
        }
    }

    /** 收起态·系统通知：[应用图标] 标题 正文 */
    _buildNotifyView() {
        const box = new St.BoxLayout({style_class: 'island-collapsed'});
        box.x_expand = true;
        box.y_expand = true;

        this._notifyIcon = new St.Icon({style_class: 'island-notify-icon', icon_size: 16});
        this._notifyIcon.y_align = Clutter.ActorAlign.CENTER;
        this._notifyIcon.visible = false;
        box.add_child(this._notifyIcon);

        this._notifyTitle = makeLabel('', 'island-notify-title', {ellipsize: true});
        this._notifyTitle.y_align = Clutter.ActorAlign.CENTER;
        box.add_child(this._notifyTitle);

        this._notifyBody = makeLabel('', 'island-notify-body', {ellipsize: true});
        this._notifyBody.y_align = Clutter.ActorAlign.CENTER;
        this._notifyBody.x_expand = true;
        box.add_child(this._notifyBody);
        return box;
    }

    /** 展开态·系统通知大卡片：图标 + 标题 / 应用 + 正文 + 提示 */
    _buildNotifyCard() {
        const body = new St.BoxLayout({vertical: true, style_class: 'island-expanded'});
        body.x_expand = true;
        body.y_expand = true;

        const row1 = new St.BoxLayout({style_class: 'island-row'});
        this._notifyIconBig = new St.Icon({style_class: 'island-notify-icon-big', icon_size: 28});
        this._notifyIconBig.y_align = Clutter.ActorAlign.CENTER;
        const head = new St.BoxLayout({vertical: true, style_class: 'island-meta', x_expand: true,
            y_align: Clutter.ActorAlign.CENTER});
        this._notifyTitleBig = makeLabel('', 'island-notify-title-big', {ellipsize: true});
        this._notifyAppBig = makeLabel('', 'island-sub', {ellipsize: true});
        head.add_child(this._notifyTitleBig);
        head.add_child(this._notifyAppBig);
        row1.add_child(this._notifyIconBig);
        row1.add_child(head);

        this._notifyBodyBig = makeLabel('', 'island-notify-body-big');
        this._notifyBodyBig.x_expand = true;
        this._notifyBodyBig.y_expand = true;
        this._notifyBodyBig.clutter_text.line_wrap = true;
        this._notifyBodyBig.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;

        const footer = new St.BoxLayout({style_class: 'island-row'});
        this._notifyHint = makeLabel('点击打开通知中心', 'island-foot');
        this._notifyHint.x_expand = true;
        this._notifyHint.x_align = Clutter.ActorAlign.END;
        footer.add_child(this._notifyHint);

        body.add_child(row1);
        body.add_child(this._notifyBodyBig);
        body.add_child(footer);
        return body;
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

        // 滚动歌词：裁剪容器（BinLayout）+ 居中对齐的标签。
        // 居中交给布局做（精确），滚动用 translation-x（不受布局影响）。
        this._marqueeBox = new St.Widget({
            style_class: 'island-marquee',
            clip_to_allocation: true,
            x_expand: true,
        });
        this._marqueeBox.y_align = Clutter.ActorAlign.CENTER;
        // 标签保持自然宽度（内部布局不被约束），位置/滚动全部用 translation-x 控制
        this._miniLyric = new St.Label({text: '', style_class: 'island-mini-lyric'});
        this._miniLyric.clutter_text.single_line_mode = true;
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

    // ------------------------------------------------- 计时器 / 闹钟 / 秒表

    _now() {
        return Date.now() / 1000;
    }

    _fmtClock(seconds) {
        const total = Math.max(0, Math.ceil(seconds));
        const h = Math.floor(total / 3600);
        const m = Math.floor((total % 3600) / 60);
        const s = total % 60;
        if (h > 0)
            return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
        return `${m}:${String(s).padStart(2, '0')}`;
    }

    _fmtStopwatch(seconds) {
        const total = Math.max(0, seconds);
        const m = Math.floor(total / 60);
        const s = total % 60;
        return `${String(m).padStart(2, '0')}:${s.toFixed(1).padStart(4, '0')}`;
    }

    hasActivity() {
        return this._activity !== null;
    }

    /** 当前活动的显示文本（收起态用） */
    activityText() {
        const act = this._activity;
        if (!act)
            return '';
        if (act.kind === 'timer')
            return act.ringing ? '时间到' : this._fmtClock(act.remaining ?? 0);
        if (act.kind === 'alarm')
            return act.ringing ? '时间到' : act.label;
        if (act.kind === 'stopwatch')
            return this._fmtStopwatch(act.elapsed ?? 0);
        return '';
    }

    activityIcon() {
        const act = this._activity;
        if (!act)
            return 'alarm-symbolic';
        if (act.kind === 'timer')
            return 'timer-symbolic';
        if (act.kind === 'alarm')
            return 'alarm-symbolic';
        return 'stopwatch-symbolic';
    }

    activitySubtitle() {
        const act = this._activity;
        if (!act)
            return '';
        if (act.kind === 'timer')
            return act.ringing ? '计时器时间到' : `计时器 · 共 ${this._fmtClock(act.total)}`;
        if (act.kind === 'alarm')
            return act.ringing ? '闹钟响了' : `闹钟 · ${act.label}`;
        return act.running ? '秒表 · 计时中' : '秒表 · 已暂停';
    }

    // ---- 设置 ----

    setTimer(minutes) {
        const total = Math.max(1, Math.round(Number(minutes) || 1)) * 60;
        this._activityExpanded = false;
        this._activity = {
            kind: 'timer', total,
            endsAt: this._now() + total,
            remaining: total,
            paused: false,
            ringing: false,
        };
        this._activityChanged();
    }

    toggleTimer() {
        const act = this._activity;
        if (!act || act.kind !== 'timer' || act.ringing)
            return;
        if (act.paused) {
            act.paused = false;
            act.endsAt = this._now() + (act.remaining ?? 0);
        } else {
            act.paused = true;
            act.remaining = Math.max(0, act.endsAt - this._now());
        }
        this._activityChanged();
    }

    addTimerMinute() {
        const act = this._activity;
        if (!act || act.kind !== 'timer')
            return;
        if (act.ringing) {
            act.ringing = false;
            this._activityExpanded = false;
            act.paused = false;
            act.total = 60;
            act.remaining = 60;
            act.endsAt = this._now() + 60;
        } else if (act.paused) {
            act.remaining = (act.remaining ?? 0) + 60;
            act.total += 60;
        } else {
            act.endsAt += 60;
            act.total += 60;
        }
        this._activityChanged();
    }

    clearActivity() {
        if (this._activityRingSource) {
            GLib.source_remove(this._activityRingSource);
            this._activityRingSource = 0;
        }
        this._activity = null;
        this._activityExpanded = false;
        this._activityChanged();
    }

    /** 设置闹钟：time 形如 "HH:MM"，已过则顺延到明天 */
    setAlarm(time) {
        const match = /^(\d{1,2}):(\d{2})$/.exec(String(time ?? '').trim());
        if (!match)
            return false;
        const now = new Date();
        const at = new Date(now.getFullYear(), now.getMonth(), now.getDate(),
            Number(match[1]), Number(match[2]), 0, 0);
        if (at.getTime() <= now.getTime())
            at.setDate(at.getDate() + 1);
        this._activityExpanded = false;
        this._activity = {
            kind: 'alarm',
            at: at.getTime() / 1000,
            label: `${String(match[1]).padStart(2, '0')}:${match[2]}`,
            ringing: false,
        };
        this._activityChanged();
        return true;
    }

    /** 相对当前时间设置闹钟（N 分钟后） */
    _setAlarmIn(minutes) {
        const at = new Date(Date.now() + Math.max(1, minutes) * 60000);
        this._activityExpanded = false;
        this._activity = {
            kind: 'alarm',
            at: at.getTime() / 1000,
            label: `${String(at.getHours()).padStart(2, '0')}:${String(at.getMinutes()).padStart(2, '0')}`,
            ringing: false,
        };
        this._activityChanged();
    }

    snoozeAlarm(minutes = 5) {
        const act = this._activity;
        if (!act || act.kind !== 'alarm')
            return;
        act.ringing = false;
        this._activityExpanded = false;
        act.at = this._now() + Math.max(1, minutes) * 60;
        const d = new Date(act.at * 1000);
        act.label = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
        this._activityChanged();
    }

    startStopwatch() {
        if (this._activity?.kind === 'stopwatch' && this._activity.running)
            return;
        const acc = this._activity?.kind === 'stopwatch' ? (this._activity.accumulated ?? 0) : 0;
        this._activityExpanded = false;
        this._activity = {kind: 'stopwatch', running: true, startedAt: this._now(), accumulated: acc, elapsed: acc};
        this._activityChanged();
    }

    toggleStopwatch() {
        const act = this._activity;
        if (!act || act.kind !== 'stopwatch')
            return;
        if (act.running) {
            act.accumulated = act.elapsed ?? 0;
            act.running = false;
        } else {
            act.running = true;
            act.startedAt = this._now();
        }
        this._activityChanged();
    }

    resetStopwatch() {
        if (this._activity?.kind !== 'stopwatch')
            return;
        this._activity.accumulated = 0;
        this._activity.elapsed = 0;
        this._activity.startedAt = this._now();
        this._activityChanged();
    }

    // ---- 每次 tick 更新 / 触发 ----

    _activityTick() {
        const act = this._activity;
        if (!act)
            return;
        const now = this._now();
        if (act.kind === 'timer') {
            if (!act.paused)
                act.remaining = Math.max(0, act.endsAt - now);
            if (!act.paused && act.remaining <= 0 && !act.ringing)
                this._activityRing();
        } else if (act.kind === 'alarm') {
            if (now >= act.at && !act.ringing)
                this._activityRing();
        } else if (act.kind === 'stopwatch') {
            act.elapsed = (act.accumulated ?? 0) + (act.running ? now - act.startedAt : 0);
        }
        this._refreshActivityView();
    }

    /** 时间到：发系统通知（岛里也会显示）+ 响一声 + 自动展开 */
    _activityRing() {
        const act = this._activity;
        if (!act)
            return;
        act.ringing = true;
        this._activityExpanded = true;
        const title = act.kind === 'timer' ? '计时器时间到' : '闹钟响了';
        const body = act.kind === 'timer'
            ? `已过去 ${this._fmtClock(act.total)}`
            : `${act.label} 的闹钟`;
        this._postNotification(title, body, 'alarm-symbolic');
        try {
            global.display.get_sound_player().play_from_theme(
                'alarm-clock-elapsed', 'Timer', null);
        } catch (error) {
            console.log(`[灵动岛] 播放铃声失败: ${error.message}`);
        }
        this._activityChanged();
    }

    _activityChanged() {
        this._refreshActivityView();
        this._showView(this._stateKey());
        this._saveActivities();
    }

    /** 用 MessageTray 发一条系统通知（会经过岛自己的通知流程） */
    _postNotification(title, body, iconName = 'dialog-information-symbolic') {
        try {
            if (!this._notifySource) {
                this._notifySource = new MessageTray.Source({title: '灵动岛', iconName});
                Main.messageTray.add(this._notifySource);
            }
            const notification = new MessageTray.Notification({
                source: this._notifySource,
                title,
                body,
            });
            this._notifySource.addNotification(notification);
        } catch (error) {
            console.warn(`[灵动岛] 发通知失败: ${error.message}`);
        }
    }

    // ---- 持久化（重启扩展后仍在）----

    _saveActivities() {
        try {
            const dir = GLib.path_get_dirname(ACTIVITY_FILE);
            GLib.mkdir_with_parents(dir, 0o755);
            const data = this._activity ? {...this._activity} : null;
            GLib.file_set_contents(ACTIVITY_FILE, JSON.stringify(data));
        } catch (error) {
            console.warn(`[灵动岛] 保存活动失败: ${error.message}`);
        }
    }

    _restoreActivities() {
        try {
            const [ok, contents] = GLib.file_get_contents(ACTIVITY_FILE);
            if (!ok)
                return;
            const data = JSON.parse(new TextDecoder().decode(contents));
            if (!data || typeof data !== 'object' || !data.kind)
                return;
            // 时间已过：计时器/闹钟直接进入“响了”状态
            const now = this._now();
            if (data.kind === 'timer') {
                data.remaining = data.paused ? (data.remaining ?? 0) : Math.max(0, (data.endsAt ?? 0) - now);
                if (!data.paused && data.remaining <= 0)
                    data.ringing = true;
            } else if (data.kind === 'alarm') {
                if (now >= (data.at ?? 0))
                    data.ringing = true;
            }
            this._activity = data;
        } catch {
            // 没有记录或损坏：忽略
        }
    }

    // ---- D-Bus 控制接口（方便命令行/脚本设置）----

    _exportDbus() {
        const xml = `<node><interface name="com.loong.IslandActivities">
          <method name="SetTimer"><arg type="u" direction="in" name="minutes"/></method>
          <method name="ToggleTimer"/>
          <method name="AddMinute"/>
          <method name="SetAlarm"><arg type="s" direction="in" name="time"/></method>
          <method name="SnoozeAlarm"/>
          <method name="StartStopwatch"/>
          <method name="ToggleStopwatch"/>
          <method name="ResetStopwatch"/>
          <method name="ClearAll"/>
          <method name="GetState"><arg type="s" direction="out" name="json"/></method>
        </interface></node>`;
        try {
            const node = Gio.DBusNodeInfo.new_for_xml(xml);
            this._dbusId = Gio.DBus.session.register_object(
                '/com/loong/IslandActivities', node.interfaces[0],
                (_conn, _sender, _path, _iface, method, params, invocation) => {
                    try {
                        this._handleDbus(method, params, invocation);
                    } catch (error) {
                        invocation.return_dbus_error('com.loong.Island.Error', error.message);
                    }
                }, null, null);
            Gio.bus_own_name(Gio.BusType.SESSION, 'com.loong.IslandActivities',
                Gio.BusNameOwnerFlags.NONE, null, null, null);
            this._dbusRetryAt = 0;
            this._dbusRetries = 0;
        } catch (error) {
            console.warn(`[灵动岛] 计时器接口暂时不可用（可能是上一个扩展实例还没释放，`
                + `下次登录后恢复）：${error.message}`);
            // 30 秒后重试，最多 3 次（正常情况 disable 已注销，不会走到这里）
            this._dbusRetries = (this._dbusRetries ?? 0) + 1;
            this._dbusRetryAt = this._dbusRetries <= 3 ? this._now() + 30 : 0;
        }
    }

    _handleDbus(method, params, invocation) {
        const args = params ? params.deep_unpack() : [];
        switch (method) {
        case 'SetTimer': this.setTimer(args[0]); break;
        case 'ToggleTimer': this.toggleTimer(); break;
        case 'AddMinute': this.addTimerMinute(); break;
        case 'SetAlarm': this.setAlarm(args[0]); break;
        case 'SnoozeAlarm': this.snoozeAlarm(5); break;
        case 'StartStopwatch': this.startStopwatch(); break;
        case 'ToggleStopwatch': this.toggleStopwatch(); break;
        case 'ResetStopwatch': this.resetStopwatch(); break;
        case 'ClearAll': this.clearActivity(); break;
        case 'GetState': {
            const act = this._activity;
            const state = act ? {
                kind: act.kind,
                text: this.activityText(),
                subtitle: this.activitySubtitle(),
                ringing: !!act.ringing,
            } : null;
            invocation.return_value(new GLib.Variant('(s)', [JSON.stringify(state)]));
            return;
        }
        default: break;
        }
        invocation.return_value(null);
    }

    // ------------------------------------------------------------ 系统通知

    /** 监听 MessageTray：新通知时在胶囊里显示几秒 */
    _watchNotifications() {
        const tray = Main.messageTray;
        if (!tray)
            return;
        this._traySignals.push(tray.connect('source-added', (_tray, source) => this._watchSource(source)));
        this._traySignals.push(tray.connect('source-removed', (_tray, source) => {
            const id = this._sourceSignals.get(source);
            if (id) {
                source.disconnect(id);
                this._sourceSignals.delete(source);
            }
        }));
        if (typeof tray.getSources === 'function') {
            for (const source of tray.getSources())
                this._watchSource(source);
        }
    }

    _watchSource(source) {
        if (!source || this._sourceSignals.has(source))
            return;
        const id = source.connect('notification-added', (_source, notification) => {
            this._onNotification(source, notification);
        });
        this._sourceSignals.set(source, id);
    }

    _onNotification(source, notification) {
        this._notifyQueue.push({source, notification});
        // 用户正在看展开卡片时不打断操作，等收起后再展示（系统横幅仍然会弹）
        if (!this._notificationActive && !this._expanded && !this._hover && !this.pinned)
            this._showNextNotification();
    }

    /** 从队列取一条展示：自动展开成大卡片，停留几秒后切下一条 */
    _showNextNotification() {
        const item = this._notifyQueue.shift();
        if (!item) {
            this._notificationActive = false;
            this._notifyExpanded = false;
            this._notifyItem = null;
            this._showView(this._stateKey());
            return;
        }
        this._notifyItem = item;
        const {source, notification} = item;
        this._notifyApp = String(source?.title ?? '');
        this._notifyTitleText = String(notification?.title ?? '') || this._notifyApp || '通知';
        this._notifyBodyText = String(notification?.body ?? '');
        let gicon = null;
        try {
            gicon = notification?.gicon ?? null;
        } catch {
            gicon = null;
        }
        if (!gicon) {
            try {
                gicon = source?.icon ?? null;
            } catch {
                gicon = null;
            }
        }
        this._notifyGicon = gicon;

        this._notificationActive = true;
        this._notifyExpanded = true;
        this._refreshNotificationView();
        this._showView(this._stateKey());
        this._armNotificationTimer();
    }

    _armNotificationTimer(delay = NOTIFY_HOLD_MS) {
        if (this._notifyHoldTimer)
            GLib.source_remove(this._notifyHoldTimer);
        this._notifyHoldTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, delay, () => {
            this._notifyHoldTimer = 0;
            if (this._hover) {   // 用户正在看，等移开再说
                this._armNotificationTimer(2000);
                return GLib.SOURCE_REMOVE;
            }
            this._notificationActive = false;
            this._notifyExpanded = false;
            this._notifyItem = null;
            this._showNextNotification();
            return GLib.SOURCE_REMOVE;
        });
    }

    _refreshNotificationView() {
        const title = this._notifyTitleText || '通知';
        const body = this._notifyBodyText || '';
        setText(this._notifyTitle, title);
        setText(this._notifyBody, body);
        setText(this._notifyTitleBig, title);
        setText(this._notifyAppBig, this._notifyApp || '');
        setText(this._notifyBodyBig, body);

        const gicon = this._notifyGicon;
        this._notifyIcon.visible = !!gicon;
        this._notifyIconBig.visible = !!gicon;
        if (gicon) {
            this._notifyIcon.gicon = gicon;
            this._notifyIconBig.gicon = gicon;
        }
    }

    _unwatchNotifications() {
        for (const [source, id] of this._sourceSignals) {
            try {
                source.disconnect(id);
            } catch {
                // source 可能已销毁
            }
        }
        this._sourceSignals.clear();
        for (const id of this._traySignals) {
            try {
                Main.messageTray.disconnect(id);
            } catch {
                // 忽略
            }
        }
        this._traySignals = [];
        if (this._notifyHoldTimer) {
            GLib.source_remove(this._notifyHoldTimer);
            this._notifyHoldTimer = 0;
        }
        this._activity = null;          // 计时器 / 闹钟 / 秒表（同一时刻只保留一个）
        this._activityExpanded = false; // 响铃时自动展开
        this._dbusRetryAt = 0;
        this._activityRingSource = null;
        this._notifySource = null;
        this._dbusId = 0;
        this._notificationActive = false;
        this._notifyExpanded = false;
        this._notifyQueue = [];
        this._notifyItem = null;
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
        // 先隐藏：等下一帧量好宽度、摆到正确位置再显示，避免「先左对齐再跳」的一帧
        this._miniLyric.opacity = 0;
        this._miniLyric.set_text(text);
        this._miniLyric.x = 0;
        this._miniLyric.translation_x = 0;

        // 等一帧再量（刚 set_text 时布局可能还没更新）
        const token = ++this._lyricToken;
        this._measureId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 50, () => {
            this._measureId = 0;
            if (token === this._lyricToken)
                this._layoutLyric();
            return GLib.SOURCE_REMOVE;
        });
    }

    /**
     * 量宽 → 居中摆放 → 显示；太长则停 1 秒后从**居中位置**开始向左滚。
     * 短句直接用标签的实际宽度（精确）；长句用探针测（探针字号偏大，误差只影响滚动距离）。
     */
    _layoutLyric() {
        if (!this._lyricViewVisible())
            return;
        const boxWidth = this._marqueeBox?.width || MARQUEE_W;
        const labelWidth = this._miniLyric?.width ?? 0;
        const measured = this._measureLyricWidth();
        // 长短判断用探针：标签的分配宽度会被容器夹住，永远「放得下」
        const long = measured > boxWidth;
        // 居中：短句用标签实际宽度（精确），长句用探针（只影响滚动距离的精度）
        const textWidth = long ? measured : labelWidth;

        this._lyricTextWidth = textWidth;
        this._miniLyric.x = 0;
        this._miniLyric.translation_x = Math.round((boxWidth - textWidth) / 2);
        this._miniLyric.opacity = 255;      // 摆好位置再显示
        if (GLib.file_test(DEV_MARKER, GLib.FileTest.EXISTS))
            console.log(`[灵动岛][dbg] 歌词 "${this._miniLyricText}" 标签宽=${labelWidth} 探针=${measured} ` +
                `采用=${textWidth} 容器=${boxWidth} 偏移=${this._miniLyric.translation_x} long=${long}`);
        if (!long)
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
        const textWidth = this._lyricTextWidth || this._measureLyricWidth();
        const boxWidth = this._marqueeBox?.width || MARQUEE_W;
        if (textWidth <= boxWidth)
            return;
        if (this._marqueeTimer) {
            GLib.source_remove(this._marqueeTimer);
            this._marqueeTimer = 0;
        }
        this._miniLyricCycle = (this._miniLyricCycle ?? 0) + 1;
        this._marqueeFrom = fromRight ? boxWidth + MARQUEE_GAP : this._miniLyric.translation_x;
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
            GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
                this._startMarquee(true);
                return GLib.SOURCE_REMOVE;
            });
            return GLib.SOURCE_REMOVE;
        }
        this._miniLyric.translation_x = this._marqueeFrom - MARQUEE_SPEED * elapsed;
        return GLib.SOURCE_CONTINUE;
    }

    /** 主题缩放（可能是 1.25 这种小数） */
    _themeScale() {
        try {
            return St.ThemeContext.get_for_stage(global.stage).scale_factor || 1;
        } catch {
            return 1;
        }
    }

    /**
     * 文字真实宽度（逻辑像素）。
     * 用隐藏探针量：可见标签的内部分布局会被固定宽度约束，量出来永远等于容器宽度；
     * 探针不参与分配，但返回的是**设备像素**，要除以主题缩放。
     */
    _measureLyricWidth() {
        if (!this._lyricProbe)
            return 0;
        try {
            if (this._lyricProbe.get_text() !== this._miniLyricText)
                this._lyricProbe.set_text(this._miniLyricText);
            return this._lyricProbe.get_preferred_width(-1)[1] / this._themeScale();
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
        if (this._notificationActive) {
            const expanded = this._expanded || this._notifyExpanded;
            return `${expanded}:notify`;
        }
        if (this._activity) {
            const expanded = this._expanded || this._activityExpanded;
            return `${expanded}:activity`;
        }
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
        if (kind === 'notify')
            return [W_MUSIC, H_COLLAPSED];
        if (kind === 'activity')
            return [W_ACTIVITY, H_COLLAPSED];
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
        if (button === 1 && this._notificationActive) {
            this._openDateMenu();          // 通知中心（日期菜单里的通知列表）
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

        this._activityTick();

        // D-Bus 注册失败后的重试
        if (!this._dbusId && this._dbusRetryAt && this._now() >= this._dbusRetryAt) {
            this._dbusRetryAt = 0;
            this._exportDbus();
        }

        // 开发用：/tmp/island-activity 写 "timer 1" / "alarm 07:30" / "stopwatch" / "clear"
        if (GLib.file_test(DEV_MARKER, GLib.FileTest.EXISTS) &&
            GLib.file_test('/tmp/island-activity', GLib.FileTest.EXISTS)) {
            try {
                const [ok, contents] = GLib.file_get_contents('/tmp/island-activity');
                Gio.File.new_for_path('/tmp/island-activity').delete(null);
                if (ok) {
                    const cmd = new TextDecoder().decode(contents).trim();
                    const [what, arg] = cmd.split(/\s+/);
                    if (what === 'timer') this.setTimer(Number(arg) || 1);
                    else if (what === 'alarm') this.setAlarm(arg);
                    else if (what === 'stopwatch') this.startStopwatch();
                    else if (what === 'clear') this.clearActivity();
                }
            } catch {
                // 忽略
            }
        }

        if (!this._notificationActive && this._notifyQueue.length &&
            !this._expanded && !this._hover && !this.pinned)
            this._showNextNotification();

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
