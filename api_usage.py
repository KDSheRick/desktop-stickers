"""OpenCode 用量 / 花费 + 各厂商 API 余额（可配置，不写死厂商）。

数据来源：
  - OpenCode 用量：本地数据库 ~/.local/share/opencode/opencode.db 的 session_v2 表
    （每个会话记录了 cost / tokens_input / tokens_output）
  - 厂商余额：按设置里的厂商清单调用各家「余额查询」接口
    · 内置预设：deepseek / moonshot / siliconflow
    · 也可以用 settings.json 的 api_custom 自定义任意厂商（见 README）
  - API Key 查找顺序：settings.api_keys → OpenCode 凭据库（credential 表）→ 环境变量

可单独测试：python3 api_usage.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.request

OPENCODE_DB = os.environ.get(
    "OPENCODE_DB",
    os.path.join(os.path.expanduser("~"), ".local/share/opencode/opencode.db"))

# 内置余额预设：配置了对应 Key 就会显示
PROVIDER_PRESETS: dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek",
        "url": "https://api.deepseek.com/user/balance",
        "json_path": "balance_infos.0.total_balance",
        "currency_path": "balance_infos.0.currency",
    },
    "moonshot": {
        "label": "Moonshot",
        "url": "https://api.moonshot.cn/v1/users/me/balance",
        "json_path": "data.available_balance",
        "currency": "CNY",
    },
    "siliconflow": {
        "label": "SiliconFlow",
        "url": "https://api.siliconflow.cn/v1/user/info",
        "json_path": "data.totalBalance",
        "currency": "CNY",
    },
}

_CURRENCY_SYMBOLS = {"CNY": "¥", "RMB": "¥", "USD": "$", "EUR": "€"}

_USAGE_TTL = 8.0      # 秒：OpenCode 用量刷新间隔
_BALANCE_TTL = 600.0  # 秒：余额查询间隔（避免频繁请求）
_HTTP_TIMEOUT = 10.0

_usage_cache: dict = {"at": 0.0, "value": None}
_balance_cache: dict = {"at": 0.0, "value": None}


# ---------------------------------------------------------------- OpenCode 用量

def _query_usage() -> dict | None:
    if not os.path.exists(OPENCODE_DB):
        return None

    from datetime import datetime

    day_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
    week_start = day_start - 6 * 86400_000   # 近 7 天（含今天）
    try:
        conn = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True, timeout=2.0)
        try:
            cur = conn.cursor()
            # 优先按「逐条回复」统计（cost / tokens 带时间戳，今日口径更准确）
            # tokens 取 input + output（不把缓存命中算进使用量）
            try:
                cur.execute("""
                    SELECT COUNT(json_extract(data, '$.cost')),
                           COALESCE(SUM(json_extract(data, '$.cost')), 0),
                           COALESCE(SUM(json_extract(data, '$.tokens.input')), 0)
                             + COALESCE(SUM(json_extract(data, '$.tokens.output')), 0)
                    FROM session_message WHERE type = 'assistant'""")
                total_messages, total_cost, total_tokens = cur.fetchone()
                cur.execute("""
                    SELECT COUNT(json_extract(data, '$.cost')),
                           COALESCE(SUM(json_extract(data, '$.cost')), 0),
                           COALESCE(SUM(json_extract(data, '$.tokens.input')), 0),
                           COALESCE(SUM(json_extract(data, '$.tokens.output')), 0),
                           COALESCE(SUM(json_extract(data, '$.tokens.reasoning')), 0),
                           COALESCE(SUM(json_extract(data, '$.tokens.cache.read')), 0)
                    FROM session_message
                    WHERE type = 'assistant' AND time_created >= ?""", (day_start,))
                (today_messages, today_cost, today_in,
                 today_out, today_reasoning, today_cache) = cur.fetchone()
                today_tokens = int(today_in or 0) + int(today_out or 0)

                # 近 7 天逐日花费（画迷你柱状图）
                cur.execute("""
                    SELECT time_created, json_extract(data, '$.cost')
                    FROM session_message
                    WHERE type = 'assistant' AND time_created >= ?
                          AND json_extract(data, '$.cost') IS NOT NULL""", (week_start,))
                daily = [0.0] * 7
                for created, cost in cur.fetchall():
                    index = int((int(created) - week_start) // 86400_000)
                    if 0 <= index < 7:
                        daily[index] += float(cost or 0.0)
            except sqlite3.Error:
                # 老版本数据库：退回按会话统计
                cur.execute("""
                    SELECT COUNT(*),
                           COALESCE(SUM(cost), 0),
                           COALESCE(SUM(tokens_input), 0) + COALESCE(SUM(tokens_output), 0)
                    FROM session_v2""")
                total_messages, total_cost, total_tokens = cur.fetchone()
                cur.execute("""
                    SELECT COUNT(*), COALESCE(SUM(cost), 0),
                           COALESCE(SUM(tokens_input), 0) + COALESCE(SUM(tokens_output), 0)
                    FROM session_v2 WHERE time_created >= ?""", (day_start,))
                today_messages, today_cost, today_tokens = cur.fetchone()
                today_in = today_out = today_reasoning = today_cache = 0
                daily = [0.0] * 7
        finally:
            conn.close()
    except sqlite3.Error:
        return None

    return {
        "messages": int(total_messages or 0),
        "today_cost": float(today_cost or 0.0),
        "today_tokens": int(today_tokens or 0),
        "today_messages": int(today_messages or 0),
        "today_input": int(today_in or 0),
        "today_output": int(today_out or 0),
        "today_reasoning": int(today_reasoning or 0),
        "today_cache": int(today_cache or 0),
        "total_cost": float(total_cost or 0.0),
        "total_tokens": int(total_tokens or 0),
        "daily": daily,
    }


def opencode_usage(force: bool = False) -> dict | None:
    """OpenCode 花费 / token 统计（带缓存）。"""
    now = time.monotonic()
    if not force and _usage_cache["value"] is not None and now - _usage_cache["at"] < _USAGE_TTL:
        return _usage_cache["value"]
    value = _query_usage()
    _usage_cache.update(at=now, value=value)
    return value


# ---------------------------------------------------------------- Key 查找

def _key_from_opencode(provider_id: str) -> str | None:
    """OpenCode 凭据表里的 key（值形如 {"type": "key", "key": "sk-..."}）。"""
    if not os.path.exists(OPENCODE_DB):
        return None
    try:
        conn = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT value FROM credential WHERE integration_id = ? ORDER BY active DESC LIMIT 1",
                (provider_id,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    raw = str(row[0])
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            for key in ("key", "api_key", "apiKey", "token"):
                if isinstance(data.get(key), str):
                    return data[key]
        return None
    except ValueError:
        return raw if raw.startswith("sk-") else None


def provider_key(provider_id: str, settings_values: dict | None = None) -> str | None:
    """按 settings → OpenCode 凭据 → 环境变量 的顺序找 Key。"""
    if settings_values:
        keys = settings_values.get("api_keys") or {}
        if isinstance(keys, dict):
            value = keys.get(provider_id)
            if isinstance(value, str) and value.strip():
                return value.strip()

    value = _key_from_opencode(provider_id)
    if value:
        return value

    env_name = f"{provider_id.upper().replace('-', '_')}_API_KEY"
    return os.environ.get(env_name) or None


# ---------------------------------------------------------------- 厂商余额

def load_providers(settings_values: dict | None = None) -> list[dict]:
    """把设置里的预设名 / 自定义项合并成查询清单（只保留能找到 Key 的）。"""
    values = settings_values or {}
    providers: list[dict] = []

    for item in values.get("api_providers") or []:
        if not isinstance(item, str):
            continue
        preset = PROVIDER_PRESETS.get(item)
        if preset:
            providers.append({"id": item, **preset})

    for item in values.get("api_custom") or []:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        provider_id = str(item.get("id") or item.get("label") or "custom")
        providers.append({
            "id": provider_id,
            "label": str(item.get("label") or provider_id),
            "url": str(item["url"]),
            "json_path": str(item.get("json_path") or ""),
            "currency": str(item.get("currency") or ""),
            "currency_path": str(item.get("currency_path") or ""),
            "auth_header": str(item.get("auth_header") or "Authorization"),
            "auth_prefix": str(item.get("auth_prefix", "Bearer ")),
        })

    result = []
    for provider in providers:
        key = provider_key(provider["id"], values)
        if key:
            provider = dict(provider)
            provider["key"] = key
            result.append(provider)
    return result


def _dig(data, path: str):
    """按 "a.0.b" 这样的路径取值。"""
    node = data
    for part in [p for p in path.split(".") if p != ""]:
        if isinstance(node, list):
            index = int(part)
            node = node[index]
        elif isinstance(node, dict):
            node = node[part]
        else:
            raise KeyError(path)
    return node


def _fetch_one(provider: dict) -> dict:
    request = urllib.request.Request(provider["url"], headers={
        provider.get("auth_header", "Authorization"):
            f"{provider.get('auth_prefix', 'Bearer ')}{provider['key']}",
        "Accept": "application/json",
        "User-Agent": "sysstickers/1.0",
    })
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
        balance = _dig(data, provider.get("json_path") or "")
        currency = provider.get("currency") or ""
        if provider.get("currency_path"):
            try:
                currency = str(_dig(data, provider["currency_path"]))
            except (KeyError, IndexError, ValueError, TypeError):
                pass
        return {
            "id": provider["id"],
            "label": provider["label"],
            "balance": balance,
            "currency": currency,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - 网络/解析失败都当作查询失败
        return {
            "id": provider["id"],
            "label": provider["label"],
            "balance": None,
            "currency": provider.get("currency") or "",
            "error": f"{type(exc).__name__}: {exc}",
        }


def fetch_balances(providers: list[dict], force: bool = False) -> list[dict]:
    """查询各厂商余额（阻塞，请在后台线程里调用）；结果带缓存。"""
    now = time.monotonic()
    if not force and _balance_cache["value"] is not None and now - _balance_cache["at"] < _BALANCE_TTL:
        return _balance_cache["value"]
    value = [_fetch_one(provider) for provider in providers]
    _balance_cache.update(at=now, value=value)
    return value


def balances_cached() -> list[dict] | None:
    """只读缓存（不发起请求）。"""
    return _balance_cache["value"]


def balances_expired() -> bool:
    return _balance_cache["value"] is None or (
        time.monotonic() - _balance_cache["at"] >= _BALANCE_TTL)


def fmt_tokens(count: int) -> str:
    """token 数量格式化：1250 → 1.25k，2_300_000 → 2.30M。"""
    value = float(max(count, 0))
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return f"{int(value)}"


def fmt_balance(balance, currency: str) -> str:
    """余额格式化：8.07 CNY → ¥8.07。"""
    try:
        number = f"{float(balance):.2f}"
    except (TypeError, ValueError):
        number = str(balance)
    symbol = _CURRENCY_SYMBOLS.get(str(currency).upper(), "")
    if symbol:
        return f"{symbol}{number}"
    return f"{number} {currency}".strip()


if __name__ == "__main__":  # 自测：python3 api_usage.py
    print("OpenCode 用量:", json.dumps(opencode_usage(force=True), ensure_ascii=False))

    import settings as settings_module

    values = settings_module.load()
    providers = load_providers(values)
    print(f"已配置 {len(providers)} 个余额厂商:", [p["label"] for p in providers])
    for item in fetch_balances(providers, force=True):
        if item["error"]:
            print(f"  {item['label']}: 查询失败（{item['error'][:60]}）")
        else:
            print(f"  {item['label']}: {fmt_balance(item['balance'], item['currency'])}")
