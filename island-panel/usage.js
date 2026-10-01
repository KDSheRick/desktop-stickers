/**
 * API 用量数据：定时执行 `python3 api_usage.py --json`（异步，不阻塞 shell）。
 *
 * 复用了项目里的 Python 模块（OpenCode 数据库统计 + 各厂商余额），
 * 余额本身在 Python 侧有 10 分钟磁盘缓存，所以这里每分钟跑一次也很轻。
 */

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

const REFRESH_SECONDS = 60;

export class UsageWatcher {
    /**
     * @param {object} options
     * @param {string} options.scriptPath - api_usage.py 的绝对路径
     * @param {Function} options.onData   - ({usage, balances}) 回调
     */
    constructor({scriptPath, onData} = {}) {
        this._scriptPath = scriptPath;
        this._onData = onData ?? (() => {});

        this._proc = null;
        this._timer = 0;
        this._generation = 0;
        this._warned = false;
    }

    enable() {
        this._generation += 1;
        this._fetch();
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, REFRESH_SECONDS, () => {
            this._fetch();
            return GLib.SOURCE_CONTINUE;
        });
    }

    disable() {
        this._generation += 1;
        if (this._timer) {
            GLib.source_remove(this._timer);
            this._timer = 0;
        }
        if (this._proc) {
            this._proc.force_exit();
            this._proc = null;
        }
    }

    _fetch() {
        if (this._proc)
            return;

        const python = GLib.find_program_in_path('python3');
        if (!python || !this._scriptPath) {
            this._warnOnce('没有找到 python3 或 api_usage.py，API 用量不可用');
            return;
        }

        let proc;
        try {
            proc = Gio.Subprocess.new(
                [python, this._scriptPath, '--json'],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE);
        } catch (error) {
            this._warnOnce(`启动 api_usage.py 失败: ${error.message}`);
            return;
        }

        this._proc = proc;
        const generation = this._generation;
        proc.communicate_utf8_async(null, null, (subprocess, res) => {
            this._proc = null;
            if (generation !== this._generation)
                return;
            try {
                const [, stdout] = subprocess.communicate_utf8_finish(res);
                if (!subprocess.get_successful()) {
                    this._warnOnce('api_usage.py 执行失败，API 用量暂不可用');
                    return;
                }
                const data = JSON.parse(stdout);
                this._onData(data);
            } catch (error) {
                this._warnOnce(`解析 API 用量失败: ${error.message}`);
            }
        });
    }

    _warnOnce(message) {
        if (this._warned)
            return;
        this._warned = true;
        console.warn(`[灵动岛] ${message}`);
    }
}
