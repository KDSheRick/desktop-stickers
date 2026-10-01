/**
 * 灵动岛（GNOME Shell 扩展）入口。
 *
 * 背景：GNOME 45+ 把扩展当 ES Module 加载，而且每个 Shell 进程只 import
 * 一次（extensionSystem.js 里写死了这个行为），所以改完代码后
 * disable/enable 不会重新加载 —— 官方建议注销重登。
 *
 * 这里做了一个很小的「开发加载器」：每次启用时把实现文件复制到一个新
 * 目录再 import（URL 变化绕开模块缓存），于是改完代码 disable/enable
 * 就能看到效果；stylesheet.css 本来就会在每次启用时重新读取。
 */

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const IMPLEMENTATION_FILES = ['island.js', 'media.js', 'usage.js'];
const CACHE_SUBDIR = 'sysstickers/island-dev';
const ENTRY = IMPLEMENTATION_FILES[0];

export default class IslandExtension extends Extension {
    async enable() {
        this._generation = (this._generation ?? 0) + 1;
        const generation = this._generation;

        let entryUrl;
        try {
            entryUrl = this._stageSources();
        } catch (error) {
            console.error(`[灵动岛] 准备实现文件失败: ${error.message}`);
            return;
        }

        try {
            const module = await import(entryUrl);
            if (generation !== this._generation)   // 期间被 disable 了
                return;
            this._impl = new module.Island(this);
            await this._impl.enable();
        } catch (error) {
            console.error(`[灵动岛] 加载失败: ${error.message}\n${error.stack}`);
        }
    }

    disable() {
        this._generation = (this._generation ?? 0) + 1;
        try {
            this._impl?.disable();
        } catch (error) {
            console.error(`[灵动岛] 卸载出错: ${error.message}\n${error.stack}`);
        }
        this._impl = null;
        this._cleanup();
    }

    /** 复制实现文件到带时间戳的新目录，返回入口模块 URL。 */
    _stageSources() {
        const base = Gio.File.new_for_path(GLib.build_filenamev([
            GLib.get_user_cache_dir(), CACHE_SUBDIR]));
        const dir = base.get_child(`gen-${GLib.get_monotonic_time()}`);
        GLib.mkdir_with_parents(dir.get_path(), 0o755);

        for (const name of IMPLEMENTATION_FILES) {
            const source = this.dir.get_child(name);
            if (!source.query_exists(null))
                throw new Error(`缺少实现文件: ${name}`);
            source.copy(dir.get_child(name), Gio.FileCopyFlags.OVERWRITE, null, null);
        }

        this._stagedDir = dir;
        return dir.get_child(ENTRY).get_uri();
    }

    /** 清掉旧的暂存目录（保留当前这次加载用的）。 */
    _cleanup() {
        const staged = this._stagedDir;
        try {
            const base = staged?.get_parent() ?? Gio.File.new_for_path(GLib.build_filenamev([
                GLib.get_user_cache_dir(), CACHE_SUBDIR]));
            const iter = base.enumerate_children('standard::name', Gio.FileQueryInfoFlags.NONE, null);
            let info;
            while ((info = iter.next_file(null)) !== null) {
                const child = base.get_child(info.get_name());
                if (staged && child.equal(staged))
                    continue;
                this._removeRecursive(child);
            }
        } catch (error) {
            console.warn(`[灵动岛] 清理暂存目录失败: ${error.message}`);
        }
        this._stagedDir = null;
    }

    _removeRecursive(file) {
        try {
            if (file.query_file_type(Gio.FileQueryInfoFlags.NONE, null) === Gio.FileType.DIRECTORY) {
                const iter = file.enumerate_children('standard::name', Gio.FileQueryInfoFlags.NONE, null);
                let info;
                while ((info = iter.next_file(null)) !== null)
                    this._removeRecursive(file.get_child(info.get_name()));
            }
            file.delete(null);
        } catch {
            // 删不掉就留着，不影响功能
        }
    }
}
