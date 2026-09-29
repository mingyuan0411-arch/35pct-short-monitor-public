#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
3~5% Short-Term Long Monitor FINAL 2026-09-28
=================================

三組雷達：
1. CRYPTO
2. US-STOCK-PERP
3. HK-STOCK

狀態：
NO_SIGNAL
WATCH
READY
ENTRY
BREAKOUT_WATCH
BREAKOUT_CHECK
BREAKOUT_ENTRY
WAIT_HISTORY
NOT_FOUND

核心：
1H  -> 主趨勢
15m -> READY
5m  -> ENTRY

v2.1 保留 v2.0 全部邏輯，修正「目標會跟著價格上移」誤區：
- READY / BREAKOUT_WATCH 第一次成立時鎖定 SETUP_TARGET
- 後續每1分鐘快掃只更新目前價，不重新上移目標
- 正式 ENTRY / BREAKOUT_ENTRY 必須用固定 SETUP_TARGET 計算剩餘空間 >= 2%
- 若等候期間只剩 1.x%，自動降為 ENTRY_CHECK / BREAKOUT_CHECK，不追價
- TREND 與 BREAKOUT 各自保存獨立固定目標

v2.0 已新增第二條「爆量突破」通道：
- 原 TREND ENTRY 不變：1H 主趨勢 + 15m READY + 5m ENTRY
- 新 BREAKOUT ENTRY：1H 不可明顯偏空 + 15m 突破近期壓力 + 5m 爆量大陽
- BREAKOUT ENTRY 一樣要求剩餘預估空間至少 2%
- BREAKOUT_WATCH 接近 15m 壓力時可進 1 分鐘快掃
- BREAKOUT_CHECK 代表突破成立但剩餘空間不足 2%，不冒充正式 ENTRY
- 15m 突破判斷使用最新形成中的 15m K，降低突然起漲漏訊號
- 5m ENTRY / BREAKOUT 使用最新形成中的 5m K
- READY / ENTRY_CHECK / BREAKOUT_WATCH / BREAKOUT_CHECK 後每 60 秒快速重掃
- v2.3：SETUP TARGET 在第一次成立時立即鎖定，快掃永遠用固定目標計算剩餘空間
- v2.3：每輪 log 強制輸出 FIXED_TARGET / FIXED_LEFT，方便驗證目標沒有上移
- v2.3：BREAKOUT_WATCH 僅在距壓力 <=0.60% 時進入快掃，避免候選過多拖慢整輪
- ENTRY 通知保留成交額、量能比、Spread、流動性與固定目標追蹤
- HK-STOCK 僅於香港交易時段 09:30-12:00、13:00-16:00 監控
- CRYPTO / US-STOCK-PERP 維持 24H

FINAL 2026-09-28 統一修正：
- HK-STOCK 不再進入 /futures/usdt/contracts，永續與港股股票資料源徹底分流
- 港股啟動時先用 Gate /stock/symbols?exchange=hk 驗證「股票交易區真正可交易」白名單
- 港股 K 線改抓真實港股現貨 Yahoo .HK 行情，不再拿 HK USDT/PERP/FUTURES 代替
- 港股 Spread 改讀 Gate /stock/market/{symbol}/orderbook
- 正式 ENTRY 同步計算扣除買入+賣出手續費後的損益兩平、淨利3%、淨利5%出場價
- 港股股票預設單邊費率 0.10%；美股永續/加密永續預設單邊 0.05%
- 費率均可由環境變數覆寫，避免平台日後調整費率時硬改程式

只做趨勢多。
不自動下單。
"""

import json
import os
import re
import time
import urllib.parse
import urllib.request

from datetime import datetime, timezone, time as dt_time
from pathlib import Path
from email.header import Header
from zoneinfo import ZoneInfo



def load_2560_context():
    """
    讀取 2560 最新狀態，供 35 作上級方向濾網。
    優先讀 2560_latest.json；若不存在則回空字典。
    只做方向加權，不把 2560 當成 35 的硬性必要條件。
    """
    candidates = [
        Path("2560_latest.json"),
        Path(".monitor_state/2560_latest.json"),
    ]
    for p in candidates:
        if not p.exists():
            continue
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
            out = {}
            for r in payload.get("results", []):
                base = str(r.get("base") or r.get("symbol") or "").replace("_USDT","").replace("USDT","")
                if not base:
                    continue
                out[base] = {
                    "status": r.get("status", "UNKNOWN"),
                    "1d_strategy": r.get("1d_strategy"),
                    "1d_soft": r.get("1d_soft"),
                    "1d_confirm": r.get("1d_confirm"),
                }
            return out
        except Exception as e:
            print("2560 CONTEXT WARN:", e)
    return {}


def apply_2560_filter(result, ctx):
    """
    2560 僅作方向濾網，不直接否決 35。
    - TREND_READY / PRE-STRICT / STRICT: 加權正面
    - WATCH: 中性偏正
    - NO_SIGNAL 且日K明顯未放行: 35 BREAKOUT/ENTRY 降級觀察
    """
    base = str(result.get("symbol") or result.get("base") or "")
    base = base.replace("_USDT","").replace("USDT","")
    info = ctx.get(base) or ctx.get(base.zfill(4))
    result["2560_status"] = (info or {}).get("status", "N/A")
    if not info:
        return result

    status = str(info.get("status") or "")
    daily_ok = info.get("1d_strategy")
    if daily_ok is None:
        daily_ok = info.get("1d_confirm")
    if daily_ok is None:
        daily_ok = info.get("1d_soft")

    result["2560_daily_ok"] = daily_ok

    if status in ("TREND_READY", "PRE-STRICT", "STRICT"):
        result["2560_bias"] = "POSITIVE"
    elif status == "WATCH":
        result["2560_bias"] = "NEUTRAL_POSITIVE"
    elif status == "NO_SIGNAL" and daily_ok is False:
        result["2560_bias"] = "NEGATIVE"
        if result.get("status") in ("ENTRY", "BREAKOUT_ENTRY"):
            result["status"] = "ENTRY_CHECK"
        elif result.get("status") in ("READY", "BREAKOUT_WATCH"):
            result["status"] = "WATCH"
    else:
        result["2560_bias"] = "NEUTRAL"

    return result

# ============================================================
# Gate API
# ============================================================

BASE = "https://api.gateio.ws/api/v4"


# ============================================================
# 分組
# ============================================================

CRYPTO = [
    "BTC",
    "ETH",
    "XRP",
    "SOL",
    "BNB",
    "ADA",
    "LTC",
    "LINK",
    "DOGE",
    "SUI",
    "HYPE",
]

US_STOCK_PERP = [
    "MU",
    "VRT",
    "DELL",
    "NVDA",
    "TSM",
    "BRKB",
]

HK_STOCK = [
    "0700",
    "1810",
    "3690",
    "1024",
    "0981",
    "3750",
    "9988",
    "0388",
    "2318",
    "0005",
    "1398",
    "3988",
    "0016",
    "0941",
    "0762",
    "0883",
    "1211",
    "0175",
    "6869",
    "2899",
    "6618",
    "1093",
    "0291",
    "9633",
    "0027",
]


HK_FIXED_CODES = {
    "0700": "騰訊控股",
    "1810": "小米集團-W",
    "3690": "美團-W",
    "1024": "快手-W",
    "0981": "中芯國際",
    "3750": "寧德時代",
    "9988": "阿里巴巴-W",
    "0388": "香港交易所",
    "2318": "中國平安",
    "0005": "滙豐控股",
    "1398": "工商銀行",
    "3988": "中國銀行",
    "0016": "新鴻基地產",
    "0941": "中國移動",
    "0762": "中國聯通",
    "0883": "中國海洋石油",
    "1211": "比亞迪股份",
    "0175": "吉利汽車",
    "6869": "長飛光纖光纜",
    "2899": "紫金礦業",
    "6618": "京東健康",
    "1093": "石藥集團",
    "0291": "華潤啤酒",
    "9633": "農夫山泉",
    "0027": "銀河娛樂",
}

GROUPS = {
    "CRYPTO": CRYPTO,
    "US-STOCK-PERP": US_STOCK_PERP,
    "HK-STOCK": HK_STOCK,
}


# ============================================================
# 別名
# ============================================================

ALIASES = {
    "BRKB": ["BRKB", "BRKBG", "BRK.B"],
    "TSM": ["TSM", "TSMUS"],
    "TENCENT": ["TENCENT", "700", "0700"],
    "XIAOMI": ["XIAOMI", "1810"],
    "MEITUAN": ["MEITUAN", "3690"],
    "KUAISHOU": ["KUAISHOU", "1024"],
    "HKEX": ["HKEX", "0388", "388"],
    "SMIC": ["SMIC", "0981", "981"],
    "BYD": ["BYD", "1211"],
    "CATL": ["CATL", "3750"],
    "YOFC": ["YOFC", "6869"],
    "ZJINNOLIGHT": ["ZJINNOLIGHT"],
    "GIGADEV": ["GIGADEV"],
    "HHGRACE": ["HHGRACE"],
    "ZIJINMINING": ["ZIJINMINING", "2899"],
}


# 只有永續組允許進入 /futures/usdt/contracts。
# HK-STOCK 必須走 Gate 股票交易區白名單，禁止混入 USDT/PERP/FUTURES。
FUTURES_SYMBOLS = CRYPTO + US_STOCK_PERP
ALL_SYMBOLS = list(dict.fromkeys(FUTURES_SYMBOLS))

# Yahoo 真實港股現貨代號，只在 Gate /stock/symbols?exchange=hk 驗證通過後才使用。
HK_YAHOO = {
    "TENCENT": "0700.HK",
    "XIAOMI": "1810.HK",
    "MEITUAN": "3690.HK",
    "KUAISHOU": "1024.HK",
    "HKEX": "0388.HK",
    "SMIC": "0981.HK",
    "BYD": "1211.HK",
    "CATL": "3750.HK",
    "YOFC": "6869.HK",
    "ZIJINMINING": "2899.HK",
    # 下列名稱只有 Gate 股票白名單能驗證到時才啟用；找不到就 NOT_FOUND。
    "ZJINNOLIGHT": None,
    "GIGADEV": None,
    "HHGRACE": None,
}


# ============================================================
# 週期
# ============================================================

ONE_D = 24 * 60 * 60
ONE_H = 60 * 60
FOUR_H = 4 * 60 * 60
FIFTEEN_M = 15 * 60
FIVE_M = 5 * 60

LIMIT_1D = 180
LIMIT_1H = 220
LIMIT_4H = 180
LIMIT_15M = 220
LIMIT_5M = 220

SUMMARY_INTERVAL = 30 * 60

# 正式 ENTRY 最低剩餘預估空間

MIN_ENTRY_SPACE_PCT = 2.0

# 交易成本：單邊費率。可用 GitHub Secrets / env 覆寫。
# 已確認 Gate 港股股票買/賣單邊皆約 0.10%。
# 美股 USDT 永續回測採單邊 0.05%；加密永續預設同值，可自行覆寫。
FEE_RATE_HK_STOCK = float(os.getenv("SHORT35_FEE_HK_STOCK", "0.001"))
FEE_RATE_US_STOCK_PERP = float(os.getenv("SHORT35_FEE_US_STOCK_PERP", "0.0005"))
FEE_RATE_CRYPTO = float(os.getenv("SHORT35_FEE_CRYPTO", "0.0005"))

def fee_rate_for_group(group):
    if group == "HK-STOCK":
        return FEE_RATE_HK_STOCK
    if group == "US-STOCK-PERP":
        return FEE_RATE_US_STOCK_PERP
    return FEE_RATE_CRYPTO

def net_exit_price(entry_price, group, desired_net_pct):
    if entry_price is None or entry_price <= 0:
        return None
    f = fee_rate_for_group(group)
    target = desired_net_pct / 100.0
    # 買入實付 = entry*(1+f)，賣出實收 = exit*(1-f)
    # 要求：賣出實收 / 買入實付 - 1 = target
    return entry_price * (1.0 + f) * (1.0 + target) / (1.0 - f)


# BREAKOUT_ENTRY 追價風險門檻：距15m突破位超過1%視為高追價風險
BREAKOUT_CHASE_RISK_PCT = 1.0

# READY 階段先保留 0.5% 緩衝。
# 只有目前預估空間 >= 2.5% 才進入 1 分鐘快掃。
MIN_READY_SPACE_PCT = 2.5

# BREAKOUT 通道
# 1H 不要求完整多頭排列，但不能明顯偏空
BREAKOUT_1H_MA20_FLOOR = 0.990
BREAKOUT_1H_MA5_MA10_FLOOR = 0.990
BREAKOUT_1H_MA20_SLOPE_FLOOR = 0.995

# 15m 參考最近 20 根已完成 K 的壓力
BREAKOUT_15M_LOOKBACK = 20
BREAKOUT_CONFIRM_PCT = 0.10

# 接近壓力 1% 內，啟動 BREAKOUT_WATCH 快掃
BREAKOUT_WATCH_DISTANCE_PCT = 1.00

# v2.3：只有真正接近突破的 BREAKOUT_WATCH 才進 1 分鐘快掃。
# 狀態仍保留 BREAKOUT_WATCH，但距離 > 0.60% 不佔用快掃資源。
FAST_SCAN_BREAKOUT_DISTANCE_PCT = 0.60

# 5m 爆量大陽條件
BREAKOUT_5M_MIN_BODY_PCT = 0.25
BREAKOUT_5M_MIN_VOLUME_RATIO = 1.30
BREAKOUT_5M_MIN_CLOSE_LOCATION = 0.65

# READY / ENTRY_CHECK / BREAKOUT_WATCH / BREAKOUT_CHECK 後的快速巡查
FAST_SCAN_SECONDS = 60
FAST_SCAN_ROUNDS = 5

# 香港交易時段
HK_TZ = ZoneInfo("Asia/Hong_Kong")
HK_AM_START = dt_time(9, 30)
HK_AM_END = dt_time(12, 0)
HK_PM_START = dt_time(13, 0)
HK_PM_END = dt_time(16, 0)



def hk_short35_market_mode(now_utc=None):
    """
    35 港股：
    - 09:30-12:00 / 13:00-16:00 主輪正常掃描
    - READY / ENTRY_CHECK / BREAKOUT_WATCH / BREAKOUT_CHECK 才啟動 1 分鐘快掃
    - 午休/收市後/週末不做港股快掃
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    hk_now = now_utc.astimezone(HK_TZ)

    if hk_now.weekday() >= 5:
        return "CLOSED"

    t = hk_now.time().replace(tzinfo=None)
    if HK_AM_START <= t < HK_AM_END or HK_PM_START <= t < HK_PM_END:
        return "OPEN"
    if HK_AM_END <= t < HK_PM_START:
        return "LUNCH"
    return "CLOSED"

# ============================================================
# NTFY
# ============================================================

NTFY_SERVER = os.getenv(
    "NTFY_SERVER",
    "https://ntfy.sh"
).rstrip("/")

NTFY_TOPIC = os.getenv(
    "NTFY_TOPIC_SHORT35",
    ""
).strip()


# ============================================================
# GitHub
# ============================================================

GITHUB_EVENT_NAME = os.getenv(
    "GITHUB_EVENT_NAME",
    ""
).strip()

MANUAL_RUN = (
    GITHUB_EVENT_NAME == "workflow_dispatch"
)


# ============================================================
# State
# ============================================================

STATE_DIR = Path(".short35_state")
STATE_FILE = STATE_DIR / "state.json"



def hk_display_name(symbol):
    code = str(symbol).zfill(4)
    return HK_FIXED_CODES.get(code, code)

def hk_display_label(symbol):
    code = str(symbol).zfill(4)
    return f"{code} {hk_display_name(code)}"

# ============================================================
# 基礎工具
# ============================================================

def now_iso():

    return datetime.now(
        timezone.utc
    ).isoformat()


def norm(s):

    return re.sub(
        r"[^A-Z0-9]",
        "",
        str(s).upper()
    )


def get_group(symbol):

    for group, symbols in GROUPS.items():

        if symbol in symbols:
            return group

    return "OTHER"


def pct_change(
    base_price,
    current_price
):

    if (
        base_price is None
        or current_price is None
        or base_price <= 0
    ):
        return None

    return (
        current_price
        / base_price
        - 1
    ) * 100


def price_text(v):

    if v is None:
        return "N/A"

    if abs(v) >= 100:
        return f"{v:.2f}"

    if abs(v) >= 10:
        return f"{v:.3f}"

    if abs(v) >= 1:
        return f"{v:.4f}"

    return f"{v:.6f}"


def pct_text(v):

    if v is None:
        return "N/A"

    return f"{v:+.2f}%"


def money_text(v):

    if v is None:
        return "N/A"

    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.2f}M U"

    if abs(v) >= 1_000:
        return f"{v / 1_000:.2f}K U"

    return f"{v:.2f} U"


def hk_market_open(now_utc=None):

    now_utc = now_utc or datetime.now(timezone.utc)
    hk_now = now_utc.astimezone(HK_TZ)

    # Monday=0 ... Sunday=6
    if hk_now.weekday() >= 5:
        return False

    t = hk_now.time().replace(tzinfo=None)

    return (
        HK_AM_START <= t < HK_AM_END
        or
        HK_PM_START <= t < HK_PM_END
    )


def hk_trade_date(now_utc=None):

    now_utc = now_utc or datetime.now(timezone.utc)
    return now_utc.astimezone(HK_TZ).date().isoformat()


def parse_book_price(level):

    if level is None:
        return None

    if isinstance(level, dict):
        value = level.get("p")
        if value is None:
            value = level.get("price")
    elif isinstance(level, (list, tuple)) and level:
        value = level[0]
    else:
        value = None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_spread_pct(contract):

    try:
        book = gate_get(
            "/futures/usdt/order_book",
            {
                "contract": contract,
                "limit": 1,
            }
        )

        asks = book.get("asks") or []
        bids = book.get("bids") or []

        ask = parse_book_price(asks[0]) if asks else None
        bid = parse_book_price(bids[0]) if bids else None

        if (
            ask is None
            or bid is None
            or ask <= 0
            or bid <= 0
            or ask < bid
        ):
            return None

        mid = (ask + bid) / 2.0

        return (
            (ask - bid)
            / mid
            * 100
        )

    except Exception as e:

        print(
            f"SPREAD WARN {contract}: {e}"
        )

        return None


def liquidity_label(
    turnover_5m,
    spread_pct
):

    # 只做資訊提示，不作為 ENTRY 硬門檻
    if turnover_5m is None:
        return "N/A"

    if (
        turnover_5m >= 30_000
        and (
            spread_pct is None
            or spread_pct <= 0.15
        )
    ):
        return "高"

    if (
        turnover_5m >= 10_000
        and (
            spread_pct is None
            or spread_pct <= 0.30
        )
    ):
        return "正常"

    if turnover_5m >= 3_000:
        return "偏低"

    return "很低"


# ============================================================
# Gate API
# ============================================================

def gate_get(
    path,
    params=None,
    retries=5
):

    params = params or {}

    query = urllib.parse.urlencode(
        params
    )

    url = BASE + path

    if query:
        url += "?" + query

    last_error = None

    for attempt in range(retries):

        try:

            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "short35-monitor/2.4",
                }
            )

            with urllib.request.urlopen(
                req,
                timeout=30
            ) as resp:

                return json.load(resp)

        except Exception as e:

            last_error = e

            time.sleep(
                min(
                    2 ** attempt,
                    8
                )
            )

    raise RuntimeError(
        f"Gate request failed: {last_error}"
    )


# ============================================================
# 合約搜尋
# ============================================================

def discover_contracts():

    data = gate_get(
        "/futures/usdt/contracts"
    )

    names = [
        x.get("name", "")
        for x in data
        if x.get("name")
    ]

    normalized_names = {
        norm(name): name
        for name in names
    }

    mapping = {}

    for symbol in ALL_SYMBOLS:

        candidates = [symbol]

        candidates.extend(
            ALIASES.get(
                symbol,
                []
            )
        )

        found = None

        for candidate in candidates:

            possible = [
                candidate,
                candidate + "USDT",
                candidate + "_USDT",
            ]

            for p in possible:

                key = norm(p)

                if key in normalized_names:

                    found = normalized_names[key]
                    break

            if found:
                break

        mapping[symbol] = found

    return mapping



# ============================================================
# Gate 港股股票白名單 + Yahoo 真實港股 K 線
# ============================================================

def discover_hk_stock_symbols():
    """只接受 Gate 股票交易區 exchange=hk 回傳的真實港股。

    Gate /stock/symbols 為分頁 API；這裡用 page_size=500 並依 total_page
    自動抓完，避免只讀第一頁造成全部 NOT_FOUND。
    """
    items = []
    page = 1
    page_size = 500

    try:
        while True:
            payload = gate_get(
                "/stock/symbols",
                {
                    "exchange": "hk",
                    "page": page,
                    "page_size": page_size,
                    "with_desc_i18n": "false",
                }
            )

            data = payload.get("data", {}) if isinstance(payload, dict) else {}
            batch = data.get("list", []) if isinstance(data, dict) else []
            items.extend(batch)

            total_page = data.get("total_page") if isinstance(data, dict) else None
            try:
                total_page = int(total_page) if total_page is not None else 1
            except (TypeError, ValueError):
                total_page = 1

            if page >= total_page or not batch:
                break

            page += 1
            if page > 100:
                break

    except Exception as e:
        print(f"HK STOCK WHITELIST ERROR: {e}")
        return {symbol: None for symbol in HK_STOCK}

    available = {}
    for item in items:
        raw_symbol = str(item.get("symbol", "") or "")
        if not raw_symbol:
            continue
        available.setdefault(norm(raw_symbol), raw_symbol)

    mapping = {}
    for symbol in HK_STOCK:
        candidates = [symbol] + ALIASES.get(symbol, [])
        found = None

        for candidate in candidates:
            keys = [norm(candidate)]
            digits = re.sub(r"\D", "", str(candidate))
            if digits:
                keys.extend([
                    digits.lstrip("0"),
                    digits.zfill(4),
                    digits.zfill(5),
                ])

            for key in keys:
                if not key:
                    continue

                if key in available:
                    found = available[key]
                    break

                for av_key, raw_symbol in available.items():
                    if av_key.isdigit() and key.isdigit():
                        if av_key.lstrip("0") == key.lstrip("0"):
                            found = raw_symbol
                            break
                if found:
                    break

            if found:
                break

        if found and HK_YAHOO.get(symbol):
            mapping[symbol] = found
        else:
            mapping[symbol] = None

    matched = sum(1 for v in mapping.values() if v)
    print(
        f"HK Gate stock whitelist rows={len(items)} "
        f"matched={matched}/{len(HK_STOCK)} pages={page}"
    )

    return mapping


YAHOO_HOSTS = [
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",
]

def yahoo_get(symbol, interval, range_text, retries=4):
    params = urllib.parse.urlencode({
        "interval": interval,
        "range": range_text,
        "includePrePost": "false",
        "events": "div,splits",
    })

    last_error = None

    for attempt in range(retries):
        host = YAHOO_HOSTS[attempt % len(YAHOO_HOSTS)]
        url = (
            f"{host}/v8/finance/chart/"
            f"{urllib.parse.quote(symbol)}?{params}"
        )

        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/124 Safari/537.36"
                    ),
                }
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.load(resp)

            result = data.get("chart", {}).get("result")
            if not result:
                raise RuntimeError(f"Yahoo no result: {data.get('chart', {}).get('error')}")
            return result[0]

        except Exception as e:
            last_error = e
            time.sleep(min(2 ** attempt, 6))

    raise RuntimeError(f"Yahoo request failed: {last_error}")


def fetch_hk_stock(symbol, interval):
    yahoo_symbol = HK_YAHOO.get(symbol)
    if not yahoo_symbol:
        raise RuntimeError(f"HK Yahoo symbol missing: {symbol}")

    # 港股交易日只有約 5.5 小時。4H 不作獨立波段資料源，
    # 以 60m 現貨 K 作波段結構/壓力代理，避免一日只剩一根 4H K。
    if interval == "4h":
        yahoo_interval, range_text = "60m", "3mo"
    elif interval == "1h":
        yahoo_interval, range_text = "60m", "3mo"
    elif interval == "15m":
        yahoo_interval, range_text = "15m", "60d"
    elif interval == "5m":
        yahoo_interval, range_text = "5m", "10d"
    elif interval == "1d":
        yahoo_interval, range_text = "1d", "1y"
    else:
        raise RuntimeError(f"unsupported HK interval: {interval}")

    data = yahoo_get(yahoo_symbol, yahoo_interval, range_text)
    timestamps = data.get("timestamp") or []
    quotes = (data.get("indicators", {}).get("quote") or [{}])[0]

    opens = quotes.get("open") or []
    highs = quotes.get("high") or []
    lows = quotes.get("low") or []
    closes = quotes.get("close") or []
    volumes = quotes.get("volume") or []

    rows = []
    for i, ts in enumerate(timestamps):
        try:
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            v = volumes[i] if i < len(volumes) else 0
        except IndexError:
            continue

        if None in (o, h, l, c):
            continue

        close = float(c)
        volume = float(v or 0)
        rows.append({
            "t": int(ts),
            "o": float(o),
            "h": float(h),
            "l": float(l),
            "c": close,
            "v": volume,
            "q": abs(volume * close),  # HKD 成交額估算
        })

    rows.sort(key=lambda z: z["t"])
    return rows


def get_hk_stock_spread_pct(stock_symbol):
    try:
        book = gate_get(
            f"/stock/market/{urllib.parse.quote(str(stock_symbol))}/orderbook",
            {}
        )
        data = book.get("data", {}) if isinstance(book, dict) else {}
        asks = data.get("asks") or []
        bids = data.get("bids") or []

        ask = parse_book_price(asks[0]) if asks else None
        bid = parse_book_price(bids[0]) if bids else None

        if ask is None or bid is None or ask <= 0 or bid <= 0 or ask < bid:
            return None

        mid = (ask + bid) / 2.0
        return (ask - bid) / mid * 100.0
    except Exception as e:
        print(f"HK STOCK SPREAD WARN {stock_symbol}: {e}")
        return None


def fetch_market(symbol, instrument, interval, limit):
    if get_group(symbol) == "HK-STOCK":
        return fetch_hk_stock(symbol, interval)
    return fetch(instrument, interval, limit)


# ============================================================
# K線
# ============================================================

def fetch(
    contract,
    interval,
    limit
):

    raw = gate_get(
        "/futures/usdt/candlesticks",
        {
            "contract": contract,
            "interval": interval,
            "limit": limit,
        }
    )

    rows = []

    for x in raw:

        volume = float(x.get("v", 0) or 0)
        close = float(x["c"])

        raw_sum = x.get("sum")

        try:
            quote_turnover = float(raw_sum) if raw_sum is not None else None
        except (TypeError, ValueError):
            quote_turnover = None

        # Gate 若沒有提供 sum，保留 v*c 作為近似值。
        # 通知中仍以 U 顯示，主要供人工比較流量大小。
        if quote_turnover is None:
            quote_turnover = abs(volume * close)

        rows.append({
            "t": int(x["t"]),
            "o": float(x["o"]),
            "h": float(x["h"]),
            "l": float(x["l"]),
            "c": close,
            "v": volume,
            "q": quote_turnover,
        })

    rows.sort(
        key=lambda z: z["t"]
    )

    return rows


def completed_only(
    rows,
    step,
    now_ts
):

    return [
        r
        for r in rows
        if r["t"] + step <= now_ts
    ]


def current_5m_turnover(r5):

    if not r5:
        return None

    return r5[-1].get("q")


def rolling_1h_turnover(r5):

    if not r5:
        return None

    values = [
        r.get("q")
        for r in r5[-12:]
        if r.get("q") is not None
    ]

    return sum(values) if values else None


# ============================================================
# 指標
# ============================================================

def sma(
    values,
    n,
    i
):

    if i + 1 < n:
        return None

    return sum(
        values[
            i - n + 1:
            i + 1
        ]
    ) / n


def true_range(
    current,
    previous
):

    if previous is None:

        return (
            current["h"]
            - current["l"]
        )

    return max(
        current["h"] - current["l"],
        abs(
            current["h"]
            - previous["c"]
        ),
        abs(
            current["l"]
            - previous["c"]
        )
    )


def add_indicators(
    rows,
    step
):

    closes = [
        r["c"]
        for r in rows
    ]

    volumes = [
        r["v"]
        for r in rows
    ]

    trs = []

    for i, r in enumerate(rows):

        previous = (
            rows[i - 1]
            if i > 0
            else None
        )

        trs.append(
            true_range(
                r,
                previous
            )
        )

    for i, r in enumerate(rows):

        r["i"] = i

        r["ma5"] = sma(
            closes,
            5,
            i
        )

        r["ma10"] = sma(
            closes,
            10,
            i
        )

        r["ma20"] = sma(
            closes,
            20,
            i
        )

        r["ma25"] = sma(
            closes,
            25,
            i
        )

        r["ma25_prev"] = (
            sma(closes, 25, i - 1)
            if i >= 25
            else None
        )

        r["ma60"] = sma(
            closes,
            60,
            i
        )

        r["ma20_prev"] = (
            sma(
                closes,
                20,
                i - 1
            )
            if i >= 20
            else None
        )

        r["ma120"] = sma(
            closes,
            120,
            i
        )

        r["ma120_prev"] = (
            sma(closes, 120, i - 1)
            if i >= 120
            else None
        )

        r["vma5"] = sma(
            volumes,
            5,
            i
        )

        r["vma20"] = sma(
            volumes,
            20,
            i
        )

        r["vma60"] = sma(
            volumes,
            60,
            i
        )

        r["vma5_prev"] = (
            sma(volumes, 5, i - 1)
            if i >= 5
            else None
        )

        r["vma60_prev"] = (
            sma(volumes, 60, i - 1)
            if i >= 60
            else None
        )

        r["atr14"] = sma(
            trs,
            14,
            i
        )

        r["close_t"] = (
            r["t"] + step
        )


# ============================================================
# 訊號條件
# ============================================================

def daily_long_allowed(r):
    """日K只決定戰略方向，不要求短週期同步。"""
    need=[r.get("ma20"),r.get("ma20_prev"),r.get("ma5"),r.get("ma10")]
    if any(x is None for x in need): return False
    return (r["c"] >= r["ma20"]*0.98 and r["ma20"] >= r["ma20_prev"]*0.995) or (r["c"] > r["ma20"] and r["ma5"] >= r["ma10"]*0.98)

def wave_structure_ok(r):
    """4H負責波段結構/回檔，不要求完整多頭排列。港股以60m代理。"""
    need=[r.get("ma10"),r.get("ma20"),r.get("ma20_prev")]
    if any(x is None for x in need): return False
    return r["c"] >= r["ma20"]*0.985 and r["ma20"] >= r["ma20_prev"]*0.992 and r["ma10"] >= r["ma20"]*0.975

def one_hour_trend(r):

    needed = [
        r.get("ma5"),
        r.get("ma10"),
        r.get("ma20"),
        r.get("ma60"),
        r.get("ma20_prev"),
    ]

    if any(
        x is None
        for x in needed
    ):
        return False

    # 1H只負責進場窗口，不再要求所有均線完美同向。
    return (
        r["c"] >= r["ma20"] * 0.995
        and r["ma20"] >= r["ma20_prev"] * 0.998
        and r["ma5"] >= r["ma10"] * 0.990
    )


def fifteen_min_ready(r):

    needed = [
        r.get("ma5"),
        r.get("ma10"),
        r.get("ma20"),
    ]

    if any(
        x is None
        for x in needed
    ):
        return False

    return (
        r["c"]
        >= r["ma20"] * 0.995

        and

        r["ma5"]
        >= r["ma10"] * 0.995
    )


def five_min_entry(
    current,
    previous
):

    needed = [
        current.get("ma5"),
        current.get("ma10"),
        current.get("ma20"),
        current.get("vma5"),
        current.get("vma20"),
        previous.get("ma5"),
        previous.get("ma10"),
    ]

    if any(
        x is None
        for x in needed
    ):
        return False

    cross_up = (
        current["ma5"]
        > current["ma10"]

        and

        previous["ma5"]
        <= previous["ma10"]
    )

    already_strong = (
        current["ma5"]
        > current["ma10"]

        and

        current["c"]
        > current["ma20"]
    )

    volume_ok = (
        current["vma5"]
        > current["vma20"]
    )

    return (
        (
            cross_up
            or
            already_strong
        )

        and

        volume_ok
    )



# ============================================================
# BREAKOUT 第二通道
# ============================================================

def one_hour_not_bearish(r):

    needed = [
        r.get("ma5"),
        r.get("ma10"),
        r.get("ma20"),
        r.get("ma20_prev"),
    ]

    if any(
        x is None
        for x in needed
    ):
        return False

    return (
        r["c"]
        >= r["ma20"] * BREAKOUT_1H_MA20_FLOOR

        and

        r["ma5"]
        >= r["ma10"] * BREAKOUT_1H_MA5_MA10_FLOOR

        and

        r["ma20"]
        >= r["ma20_prev"] * BREAKOUT_1H_MA20_SLOPE_FLOOR
    )


def breakout_reference_resistance(
    live_15m,
    completed_15m
):

    prior = [
        r
        for r in completed_15m
        if r["t"] < live_15m["t"]
    ]

    if len(prior) < BREAKOUT_15M_LOOKBACK:
        return None

    window = prior[
        -BREAKOUT_15M_LOOKBACK:
    ]

    return max(
        r["h"]
        for r in window
    )


def fifteen_min_breakout(
    live_15m,
    completed_15m
):

    resistance = breakout_reference_resistance(
        live_15m,
        completed_15m
    )

    if resistance is None or resistance <= 0:
        return {
            "ok": False,
            "resistance": resistance,
            "breakout_pct": None,
            "distance_pct": None,
        }

    breakout_pct = (
        live_15m["c"]
        / resistance
        - 1
    ) * 100

    distance_pct = (
        resistance
        / live_15m["c"]
        - 1
    ) * 100

    ok = (
        live_15m["c"]
        >= resistance
        * (
            1
            + BREAKOUT_CONFIRM_PCT / 100
        )

        and

        live_15m["c"]
        > live_15m["o"]
    )

    return {
        "ok": ok,
        "resistance": resistance,
        "breakout_pct": breakout_pct,
        "distance_pct": distance_pct,
    }


def five_min_breakout_impulse(
    current,
    previous
):

    needed = [
        current.get("ma20"),
        current.get("vma5"),
        current.get("vma20"),
    ]

    if any(
        x is None
        for x in needed
    ):
        return {
            "ok": False,
            "body_pct": None,
            "volume_ratio": None,
            "close_location": None,
        }

    if (
        current["o"] <= 0
        or current["vma20"] <= 0
    ):
        return {
            "ok": False,
            "body_pct": None,
            "volume_ratio": None,
            "close_location": None,
        }

    body_pct = (
        current["c"]
        / current["o"]
        - 1
    ) * 100

    volume_ratio = (
        current["vma5"]
        / current["vma20"]
    )

    candle_range = (
        current["h"]
        - current["l"]
    )

    if candle_range > 0:

        close_location = (
            current["c"]
            - current["l"]
        ) / candle_range

    else:

        close_location = 1.0

    previous_close_ok = (
        previous is None
        or current["c"] > previous["c"]
    )

    ok = (
        current["c"] > current["o"]

        and

        body_pct
        >= BREAKOUT_5M_MIN_BODY_PCT

        and

        volume_ratio
        >= BREAKOUT_5M_MIN_VOLUME_RATIO

        and

        close_location
        >= BREAKOUT_5M_MIN_CLOSE_LOCATION

        and

        current["c"]
        > current["ma20"]

        and

        previous_close_ok
    )

    return {
        "ok": ok,
        "body_pct": body_pct,
        "volume_ratio": volume_ratio,
        "close_location": close_location,
    }


def breakout_watch_ok(
    h1_not_bearish,
    breakout_info
):

    if not h1_not_bearish:
        return False

    distance = breakout_info.get(
        "distance_pct"
    )

    if distance is None:
        return False

    return (
        -BREAKOUT_CONFIRM_PCT
        <= distance
        <= BREAKOUT_WATCH_DISTANCE_PCT
    )


# ============================================================
# 最近壓力
# ============================================================

def recent_resistance(
    current_price,
    r1,
    r4
):

    candidates = []

    for r in r1[-48:]:

        if r["h"] > current_price:
            candidates.append(
                r["h"]
            )

    for r in r4[-30:]:

        if r["h"] > current_price:
            candidates.append(
                r["h"]
            )

    if not candidates:
        return None

    candidates.sort()

    return candidates[0]


# ============================================================
# ENTRY 原始潛力
# ============================================================

def estimate_entry_potential(
    r1,
    r4,
    latest5
):

    latest1 = r1[-1]

    entry_price = latest5["c"]

    resistance = recent_resistance(
        entry_price,
        r1,
        r4
    )

    atr = latest1.get(
        "atr14"
    )

    atr_pct = None

    if (
        atr is not None
        and
        entry_price > 0
    ):

        atr_pct = (
            atr
            / entry_price
        ) * 100


    resistance_pct = None

    if (
        resistance is not None
        and
        resistance > entry_price
    ):

        resistance_pct = (
            resistance
            / entry_price
            - 1
        ) * 100


    atr_target_pct = None

    if atr_pct is not None:

        atr_target_pct = (
            atr_pct * 2.0
        )


    volume_ratio = None

    if (
        latest5.get("vma5")
        and
        latest5.get("vma20")
    ):

        volume_ratio = (
            latest5["vma5"]
            /
            latest5["vma20"]
        )


    trend_bonus = 0.0

    if (
        latest1.get("ma5")
        is not None
        and latest1.get("ma10")
        is not None
        and latest1.get("ma20")
        is not None
    ):

        if (
            latest1["ma5"]
            > latest1["ma10"]
            > latest1["ma20"]
        ):

            trend_bonus += 0.5


    if volume_ratio is not None:

        if volume_ratio >= 1.5:

            trend_bonus += 0.8

        elif volume_ratio >= 1.2:

            trend_bonus += 0.4


    candidates = []

    if resistance_pct is not None:

        candidates.append(
            resistance_pct
        )

    if atr_target_pct is not None:

        candidates.append(
            atr_target_pct
        )


    if candidates:

        base_potential = min(
            candidates
        )

    else:

        base_potential = 3.0


    potential_pct = max(
        0.5,
        base_potential
        + trend_bonus
    )

    potential_pct = min(
        potential_pct,
        8.0
    )


    target_price = (
        entry_price
        * (
            1
            + potential_pct / 100
        )
    )


    # TP1 先取預估空間約 60%，TP2 取完整目標（最多 5%）
    # 避免 potential < 3% 時 TP1 / TP2 完全相同。
    tp1_pct = min(
        potential_pct * 0.60,
        3.0
    )

    tp2_pct = min(
        potential_pct,
        5.0
    )

    tp1 = (
        entry_price
        * (
            1
            + tp1_pct / 100
        )
    )

    tp2 = (
        entry_price
        * (
            1
            + tp2_pct / 100
        )
    )


    if potential_pct >= 4.5:

        grade = "HIGH"

    elif potential_pct >= 3.0:

        grade = "OK"

    else:

        grade = "LOW"


    return {
        "entry_price":
            entry_price,

        "target_price":
            target_price,

        "potential_pct":
            potential_pct,

        "grade":
            grade,

        "resistance":
            resistance,

        "resistance_pct":
            resistance_pct,

        "atr_pct":
            atr_pct,

        "tp1":
            tp1,

        "tp2":
            tp2,

        "tp1_pct":
            tp1_pct,

        "tp2_pct":
            tp2_pct,

        "volume_ratio":
            volume_ratio,
    }



# ============================================================
# 內建 2560 上級趨勢判斷
# ============================================================

EMBED_2560_RELAXED_VOL_RATIO = 0.90
EMBED_2560_RELAXED_VOL_GROWTH = 1.02
EMBED_2560_DAILY_SOFT_FLOOR = 0.97


def embedded_2560_structure_4h_ok(r):
    need = [
        r.get("ma25"),
        r.get("ma25_prev"),
    ]
    if any(x is None for x in need):
        return False
    return (
        r["c"] > r["ma25"]
        and r["ma25"] > r["ma25_prev"]
    )


def embedded_2560_relaxed_volume_ok(r):
    need = [
        r.get("vma5"),
        r.get("vma60"),
        r.get("vma5_prev"),
    ]
    if any(x is None for x in need):
        return False
    return (
        r["vma5"] >= r["vma60"] * EMBED_2560_RELAXED_VOL_RATIO
        or r["vma5"] > r["vma5_prev"] * EMBED_2560_RELAXED_VOL_GROWTH
    )


def embedded_2560_core_ok(r):
    need = [
        r.get("ma25"),
        r.get("ma25_prev"),
        r.get("vma5"),
        r.get("vma60"),
        r.get("vma5_prev"),
        r.get("vma60_prev"),
    ]
    if any(x is None for x in need):
        return False
    return (
        r["ma25"] > r["ma25_prev"]
        and r["c"] > r["ma25"]
        and r["vma5_prev"] <= r["vma60_prev"]
        and r["vma5"] > r["vma60"]
    )


def embedded_2560_daily_soft_ok(r):
    need = [r.get("ma25"), r.get("ma25_prev")]
    if any(x is None for x in need):
        return False
    return (
        r["c"] >= r["ma25"] * EMBED_2560_DAILY_SOFT_FLOOR
        and r["ma25"] >= r["ma25_prev"]
    )


def embedded_2560_daily_confirm_ok(r):
    need = [
        r.get("ma25"),
        r.get("ma25_prev"),
        r.get("vma5"),
        r.get("vma60"),
    ]
    if any(x is None for x in need):
        return False
    return (
        r["c"] > r["ma25"]
        and r["ma25"] > r["ma25_prev"]
        and r["vma5"] > r["vma60"]
    )


def embedded_2560_lower_timing(r1, r15):
    need1 = [
        r1.get("ma5"),
        r1.get("ma10"),
        r1.get("ma20"),
        r1.get("ma20_prev"),
    ]
    need15 = [
        r15.get("ma5"),
        r15.get("ma10"),
        r15.get("ma20"),
    ]
    if any(x is None for x in need1 + need15):
        return False

    h1 = (
        r1["c"] >= r1["ma20"] * 0.990
        and r1["ma20"] >= r1["ma20_prev"] * 0.995
        and r1["ma5"] >= r1["ma10"] * 0.985
    )
    m15 = (
        r15["c"] >= r15["ma20"] * 0.992
        and r15["ma5"] >= r15["ma10"] * 0.985
    )
    return h1 and m15


def embedded_2560_hk_status(d1, h1, m15):
    """與港股 2560 的 1D + 1H + 15m 分工一致。"""
    need_d = [
        d1.get("ma25"),
        d1.get("ma25_prev"),
        d1.get("vma5"),
        d1.get("vma60"),
    ]
    need_h = [
        h1.get("ma25"),
        h1.get("ma25_prev"),
        h1.get("vma5"),
        h1.get("vma60"),
    ]
    need_m = [
        m15.get("ma25"),
        m15.get("ma25_prev"),
        m15.get("vma5"),
        m15.get("vma60"),
    ]

    strategic = False
    wave = False
    timing = False

    if not any(x is None for x in need_d):
        strategic = (
            d1["c"] > d1["ma25"]
            and d1["ma25"] >= d1["ma25_prev"]
            and d1["vma5"] >= d1["vma60"] * 0.90
        )

    if not any(x is None for x in need_h):
        wave = (
            h1["c"] >= h1["ma25"] * 0.995
            and h1["ma25"] >= h1["ma25_prev"] * 0.995
            and (
                h1["vma5"] >= h1["vma60"] * 0.90
                or h1["vma5"] > (h1.get("vma5_prev") or 0)
            )
        )

    if not any(x is None for x in need_m):
        timing = (
            m15["c"] >= m15["ma25"] * 0.992
            and m15["ma25"] >= m15["ma25_prev"] * 0.992
            and m15["vma5"] >= m15["vma60"] * 0.85
        )

    if strategic and wave and timing:
        status = "STRICT"
    elif strategic and wave:
        status = "PRE-STRICT"
    elif strategic:
        status = "TREND_READY"
    elif wave:
        status = "WATCH"
    else:
        status = "NO_SIGNAL"

    return {
        "status": status,
        "1d_strategy": strategic,
        "1h_wave": wave,
        "15m_entry": timing,
    }


def embedded_2560_status(symbol, r1d, r4, r1, r15):
    group = get_group(symbol)

    if group == "HK-STOCK":
        return embedded_2560_hk_status(
            r1d[-1],
            r1[-1],
            r15[-1],
        )

    latest4 = r4[-1]
    latest1d = r1d[-1]

    s4 = embedded_2560_structure_4h_ok(latest4)
    rv = embedded_2560_relaxed_volume_ok(latest4)
    dsoft = embedded_2560_daily_soft_ok(latest1d)
    dconfirm = embedded_2560_daily_confirm_ok(latest1d)
    core = embedded_2560_core_ok(latest4)
    timing = embedded_2560_lower_timing(r1[-1], r15[-1])

    # 35 內建版不做 20-bar canonical 去重，因為目的只是當作方向濾網。
    # core + 完整日K確認視為 STRICT 候選；若下級 timing 未到，降為 TREND_READY。
    if core and dconfirm:
        status = "STRICT"
    elif s4 and rv and dsoft:
        status = "PRE-STRICT"
    elif s4 and dsoft:
        status = "TREND_READY"
    elif s4 or dsoft:
        status = "WATCH"
    else:
        status = "NO_SIGNAL"

    if status in ("PRE-STRICT", "STRICT") and not timing:
        status = "TREND_READY"

    return {
        "status": status,
        "4h_structure": s4,
        "4h_relaxed_volume": rv,
        "4h_core": core,
        "1d_soft": dsoft,
        "1d_confirm": dconfirm,
        "lower_entry_timing": timing,
    }


def apply_embedded_2560_filter(result):
    status = result.get("2560_status", "N/A")
    daily_ok = result.get("2560_daily_ok")

    if status in ("TREND_READY", "PRE-STRICT", "STRICT"):
        result["2560_bias"] = "POSITIVE"
    elif status == "WATCH":
        result["2560_bias"] = "NEUTRAL_POSITIVE"
    elif status == "NO_SIGNAL" and daily_ok is False:
        result["2560_bias"] = "NEGATIVE"
        if result.get("status") in ("ENTRY", "BREAKOUT_ENTRY"):
            result["status"] = "ENTRY_CHECK"
        elif result.get("status") in ("READY", "BREAKOUT_WATCH"):
            result["status"] = "WATCH"
    else:
        result["2560_bias"] = "NEUTRAL"

    return result


# ============================================================
# 單一標的分析
# ============================================================

def analyze(
    symbol,
    contract
):

    now_ts = int(
        datetime.now(
            timezone.utc
        ).timestamp()
    )

    r1d = completed_only(
        fetch_market(symbol, contract, "1d", LIMIT_1D),
        ONE_D, now_ts
    )

    r1 = completed_only(
        fetch_market(
            symbol,
            contract,
            "1h",
            LIMIT_1H
        ),
        ONE_H,
        now_ts
    )

    r4 = completed_only(
        fetch_market(
            symbol,
            contract,
            "4h",
            LIMIT_4H
        ),
        FOUR_H,
        now_ts
    )

    # 15m 同時保留 completed 與 live。
    # 原 TREND READY 使用 completed 15m；
    # BREAKOUT 通道使用最新形成中的 15m。
    r15_raw = fetch_market(
        symbol,
        contract,
        "15m",
        LIMIT_15M
    )

    r15 = completed_only(
        r15_raw,
        FIFTEEN_M,
        now_ts
    )

    # 5m 保留最新形成中的 K。
    r5 = fetch_market(
        symbol,
        contract,
        "5m",
        LIMIT_5M
    )

    if (
        len(r1d) < 30
        or
        len(r1) < 65
        or
        len(r4) < 30
        or
        len(r15) < 65
        or
        len(r15_raw) < 65
        or
        len(r5) < 65
    ):

        return {
            "base": symbol,
            "group": get_group(symbol),
            "contract": contract,
            "status": "WAIT_HISTORY",
        }

    add_indicators(r1d, ONE_D)

    add_indicators(
        r1,
        ONE_H
    )

    add_indicators(
        r4,
        FOUR_H
    )

    add_indicators(
        r15,
        FIFTEEN_M
    )

    add_indicators(
        r15_raw,
        FIFTEEN_M
    )

    add_indicators(
        r5,
        FIVE_M
    )

    latest1d = r1d[-1]
    latest1 = r1[-1]
    latest4 = r4[-1]
    latest15 = r15[-1]
    latest15_live = r15_raw[-1]
    latest5 = r5[-1]
    prev5 = r5[-2]

    # --------------------------------------------------------
    # 原 TREND 通道
    # --------------------------------------------------------

    d1_allowed = daily_long_allowed(latest1d)
    h4_wave = wave_structure_ok(latest4)

    h1 = one_hour_trend(
        latest1
    )

    m15 = fifteen_min_ready(
        latest15
    )

    m5 = five_min_entry(
        latest5,
        prev5
    )

    # --------------------------------------------------------
    # BREAKOUT 通道
    # --------------------------------------------------------

    h1_not_bearish = (
        one_hour_not_bearish(
            latest1
        )
    )

    breakout15 = (
        fifteen_min_breakout(
            latest15_live,
            r15
        )
    )

    breakout5 = (
        five_min_breakout_impulse(
            latest5,
            prev5
        )
    )

    breakout_signal = (
        d1_allowed
        and h4_wave
        and h1_not_bearish
        and breakout15.get("ok")
        and breakout5.get("ok")
    )

    breakout_watch = (
        d1_allowed
        and h4_wave
        and breakout_watch_ok(
            h1_not_bearish,
            breakout15
        )
    )

    # --------------------------------------------------------
    # 共用潛力 / 流動性
    # --------------------------------------------------------

    potential = None
    spread_pct = None

    turnover_5m = current_5m_turnover(
        r5
    )

    turnover_1h = rolling_1h_turnover(
        r5
    )

    trend_setup = (
        d1_allowed
        and h4_wave
        and h1
        and m15
    )

    need_potential = (
        trend_setup
        or
        breakout_signal
        or
        breakout_watch
    )

    if need_potential:

        potential = estimate_entry_potential(
            r1,
            r4,
            latest5
        )

        potential[
            "turnover_5m"
        ] = turnover_5m

        potential[
            "turnover_1h"
        ] = turnover_1h

        need_spread = (
            m5
            or
            breakout_signal
        )

        if need_spread:

            spread_pct = (
                get_hk_stock_spread_pct(contract)
                if get_group(symbol) == "HK-STOCK"
                else get_spread_pct(contract)
            )

        potential[
            "spread_pct"
        ] = spread_pct

        potential[
            "liquidity"
        ] = liquidity_label(
            turnover_5m,
            spread_pct
        )

        current_space = potential.get(
            "potential_pct"
        )

    else:

        current_space = None

    # --------------------------------------------------------
    # 內建 2560 上級趨勢狀態
    # --------------------------------------------------------
    embedded2560 = embedded_2560_status(
        symbol,
        r1d,
        r4,
        r1,
        r15,
    )

    # --------------------------------------------------------
    # 狀態優先順序
    #
    # 1. 原 TREND ENTRY 保持最高優先
    # 2. BREAKOUT ENTRY 補抓突然爆量突破
    # 3. 原 READY / WATCH
    # 4. BREAKOUT_WATCH 供 1 分鐘快掃
    # --------------------------------------------------------

    entry_mode = None

    if trend_setup and m5:

        entry_mode = "TREND"

        if (
            current_space is not None
            and
            current_space
            >= MIN_ENTRY_SPACE_PCT
        ):

            status = "ENTRY"

        else:

            status = "ENTRY_CHECK"

    elif breakout_signal:

        entry_mode = "BREAKOUT"

        if (
            current_space is not None
            and
            current_space
            >= MIN_ENTRY_SPACE_PCT
        ):

            status = "BREAKOUT_ENTRY"

        else:

            status = "BREAKOUT_CHECK"

    elif trend_setup:

        if (
            current_space is not None
            and
            current_space
            >= MIN_READY_SPACE_PCT
        ):

            status = "READY"

        else:

            status = "WATCH"

    elif h1:

        status = "WATCH"

    elif breakout_watch:

        status = "BREAKOUT_WATCH"

    else:

        status = "NO_SIGNAL"

    return {
        "base": symbol,
        "group": get_group(symbol),
        "contract": contract,
        "status": status,
        "daily_long_allowed": d1_allowed,
        "4h_wave_ok": h4_wave,

        "price": latest5["c"],
        "price_1h": latest1["c"],
        "price_15m": latest15_live["c"],
        "price_5m": latest5["c"],

        "1h_trend": h1,
        "15m_ready": m15,
        "5m_entry": m5,

        "1h_not_bearish":
            h1_not_bearish,

        "15m_breakout":
            breakout15.get("ok"),

        "15m_breakout_resistance":
            breakout15.get(
                "resistance"
            ),

        "15m_breakout_pct":
            breakout15.get(
                "breakout_pct"
            ),

        "15m_breakout_distance_pct":
            breakout15.get(
                "distance_pct"
            ),

        "5m_breakout":
            breakout5.get("ok"),

        "5m_breakout_body_pct":
            breakout5.get(
                "body_pct"
            ),

        "5m_breakout_volume_ratio":
            breakout5.get(
                "volume_ratio"
            ),

        "5m_breakout_close_location":
            breakout5.get(
                "close_location"
            ),

        "breakout_watch":
            breakout_watch,

        "entry_mode":
            entry_mode,

        "potential":
            potential,

        "ready_space_ok": (
            potential is not None
            and potential.get(
                "potential_pct"
            ) is not None
            and potential.get(
                "potential_pct"
            ) >= MIN_READY_SPACE_PCT
        ),

        "turnover_5m":
            turnover_5m,

        "turnover_1h":
            turnover_1h,

        "spread_pct":
            spread_pct,

        "2560_status":
            embedded2560.get("status", "N/A"),

        "2560_daily_ok":
            (
                embedded2560.get("1d_strategy")
                if get_group(symbol) == "HK-STOCK"
                else embedded2560.get("1d_confirm")
            ),

        "2560_detail":
            embedded2560,
    }



def breakout_chase_info(r):
    """
    BREAKOUT_ENTRY 人工複核資訊：
    - BO_R15: 15m突破位
    - 距突破位: current / BO_R15 - 1
    - 追價風險: LOW / HIGH / N/A
    - 處理建議
    """
    current = r.get("price")
    bo_r15 = r.get("bo_r15")

    distance_pct = pct_change(
        bo_r15,
        current
    ) if (
        bo_r15 is not None
        and current is not None
        and bo_r15 > 0
    ) else None

    if distance_pct is None:
        risk = "N/A"
        action = "缺少突破位資料，人工複核。"
    elif distance_pct <= BREAKOUT_CHASE_RISK_PCT:
        risk = "LOW"
        action = "可等回踩突破位附近，守住再考慮進場。"
    else:
        risk = "HIGH"
        action = "不追高，等待回踩；若剩餘空間掉到2%以下則放棄。"

    return {
        "bo_r15": bo_r15,
        "distance_pct": distance_pct,
        "risk": risk,
        "action": action,
    }



def enrich_signal_output(result):
    """
    只要有訊號，就統一補現價/固定目標/剩餘空間/支撐壓力/成本後出場價。
    """
    status = result.get("status")
    if status in (None, "NO_SIGNAL", "NOT_FOUND", "WAIT_HISTORY", "STALE_DATA"):
        return result

    p = result.get("potential") or {}
    current = result.get("price") or result.get("price_5m") or result.get("latest_close")
    target = (
        result.get("setup_target")
        or result.get("fixed_target")
        or p.get("target_price")
        or p.get("tp2")
    )

    result["display_current_price"] = current
    result["display_target_price"] = target

    if current and target and current > 0:
        result["remaining_space_pct"] = (target / current - 1) * 100

    result["support_price"] = p.get("support") or p.get("nearest_support")
    result["resistance_price"] = p.get("resistance") or p.get("nearest_resistance")

    group = result.get("group") or get_group(result.get("symbol"))
    entry_price = result.get("entry_price") or current
    if entry_price:
        result["breakeven_price"] = net_exit_price(entry_price, group, 0.0)
        result["net_tp3_price"] = net_exit_price(entry_price, group, 3.0)
        result["net_tp5_price"] = net_exit_price(entry_price, group, 5.0)

    return result


def format_signal_detail_35(result):
    label = result.get("display_name") or result.get("symbol") or result.get("base")
    if result.get("group") == "HK-STOCK":
        label = hk_display_label(result.get("symbol"))

    return (
        f"{label}\n"
        f"35狀態：{result.get('status')}\n"
        f"2560：{result.get('2560_status','N/A')}\n"
        f"現價：{price_text(result.get('display_current_price'))}\n"
        f"固定目標：{price_text(result.get('display_target_price'))}\n"
        f"剩餘空間：{pct_text(result.get('remaining_space_pct'))}\n"
        f"支撐：{price_text(result.get('support_price'))}\n"
        f"壓力：{price_text(result.get('resistance_price'))}\n"
        f"損益兩平：{price_text(result.get('breakeven_price'))}\n"
        f"淨3%出場：{price_text(result.get('net_tp3_price'))}\n"
        f"淨5%出場：{price_text(result.get('net_tp5_price'))}"
    )

# ============================================================
# ntfy
# ============================================================

def send_ntfy(title, msg, priority="default", tags="bell"):
    """
    中文標題安全 + 通知失敗不污染交易狀態。
    """
    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set; notification skipped.")
        return False

    try:
        safe_title = Header(str(title), "utf-8").encode()
        req = urllib.request.Request(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=str(msg).encode("utf-8"),
            method="POST",
            headers={
                "Title": safe_title,
                "Priority": str(priority),
                "Tags": str(tags),
                "Content-Type": "text/plain; charset=utf-8",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            print("ntfy:", resp.status, title)
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"NTFY WARN {title}: {e}")
        return False


def load_state():

    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    if not STATE_FILE.exists():

        return {
            "symbols": {},
            "last_summary_utc":
                None,
        }


    try:

        with STATE_FILE.open(
            "r",
            encoding="utf-8"
        ) as f:

            state = json.load(f)


        state.setdefault(
            "symbols",
            {}
        )

        state.setdefault(
            "last_summary_utc",
            None
        )

        return state


    except Exception as e:

        print(
            "STATE LOAD ERROR:",
            str(e)
        )

        return {
            "symbols": {},
            "last_summary_utc":
                None,
        }


def save_state(state):

    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    with STATE_FILE.open(
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2
        )


# ============================================================
# 階段記憶
# ============================================================

def reset_cycle(
    symbol_state
):

    keys = [
        "watch_price",
        "watch_time",
        "ready_price",
        "ready_time",
        "entry_price",
        "entry_time",
        "entry_target_price",
        "entry_original_potential_pct",
        "entry_grade",
        "entry_tp1",
        "entry_tp2",
        "entry_resistance",
        "entry_atr_pct",
        "entry_volume_ratio",
        "entry_turnover_5m",
        "entry_turnover_1h",
        "entry_spread_pct",
        "entry_liquidity",
        "entry_mode",
        "trend_setup_price",
        "trend_setup_time",
        "trend_setup_target_price",
        "trend_setup_original_space_pct",
        "breakout_setup_price",
        "breakout_setup_time",
        "breakout_setup_target_price",
        "breakout_setup_original_space_pct",
        "breakout_resistance",
        "breakout_pct",
        "breakout_body_pct",
        "breakout_volume_ratio",
    ]

    for key in keys:

        symbol_state.pop(
            key,
            None
        )


def update_stage_memory(
    r,
    symbol_state,
    previous
):

    status = r["status"]
    price = r["price"]

    symbol_state[
        "current_price"
    ] = price

    symbol_state[
        "last_seen_utc"
    ] = now_iso()


    # --------------------------------------------------------
    # 新週期開始
    #
    # 如果之前完全 NO_SIGNAL，
    # 現在重新進入 WATCH / READY / ENTRY，
    # 清除上一輪歷史。
    # --------------------------------------------------------

    if (
        previous
        in (
            "NO_SIGNAL",
            "UNKNOWN"
        )

        and

        status
        in (
            "WATCH",
            "BREAKOUT_WATCH",
            "READY",
            "ENTRY_CHECK",
            "BREAKOUT_CHECK",
            "ENTRY",
            "BREAKOUT_ENTRY"
        )
    ):

        reset_cycle(
            symbol_state
        )


    # --------------------------------------------------------
    # WATCH
    # --------------------------------------------------------

    if (
        status
        in (
            "WATCH",
            "BREAKOUT_WATCH",
            "READY",
            "ENTRY_CHECK",
            "BREAKOUT_CHECK",
            "ENTRY",
            "BREAKOUT_ENTRY"
        )

        and

        symbol_state.get(
            "watch_price"
        )
        is None
    ):

        symbol_state[
            "watch_price"
        ] = price

        symbol_state[
            "watch_time"
        ] = now_iso()


    # --------------------------------------------------------
    # READY
    # --------------------------------------------------------

    if (
        status
        in (
            "READY",
            "ENTRY_CHECK",
            "BREAKOUT_CHECK",
            "ENTRY",
            "BREAKOUT_ENTRY"
        )

        and

        symbol_state.get(
            "ready_price"
        )
        is None
    ):

        symbol_state[
            "ready_price"
        ] = price

        symbol_state[
            "ready_time"
        ] = now_iso()


    # --------------------------------------------------------
    # 固定 SETUP TARGET
    #
    # TREND：第一次進 READY / ENTRY_CHECK / ENTRY 時鎖定
    # BREAKOUT：第一次進 BREAKOUT_WATCH / CHECK / ENTRY 時鎖定
    # 後續快掃只用目前價去計算「剩餘空間」，不再把目標往上搬。
    # --------------------------------------------------------

    p = (
        r.get("potential")
        or {}
    )

    if (
        status in (
            "READY",
            "ENTRY_CHECK",
            "BREAKOUT_ENTRY",
            "ENTRY"
        )
        and symbol_state.get("trend_setup_target_price") is None
        and p.get("target_price") is not None
    ):
        symbol_state["trend_setup_price"] = price
        symbol_state["trend_setup_time"] = now_iso()
        symbol_state["trend_setup_target_price"] = p.get("target_price")
        symbol_state["trend_setup_original_space_pct"] = pct_change(
            price,
            p.get("target_price")
        )

    if (
        status in (
            "BREAKOUT_WATCH",
            "BREAKOUT_CHECK",
            "BREAKOUT_ENTRY"
        )
        and symbol_state.get("breakout_setup_target_price") is None
        and p.get("target_price") is not None
    ):
        symbol_state["breakout_setup_price"] = price
        symbol_state["breakout_setup_time"] = now_iso()
        symbol_state["breakout_setup_target_price"] = p.get("target_price")
        symbol_state["breakout_setup_original_space_pct"] = pct_change(
            price,
            p.get("target_price")
        )


    # --------------------------------------------------------
    # ENTRY
    # --------------------------------------------------------

    if (
        status in (
            "ENTRY",
            "BREAKOUT_ENTRY"
        )

        and

        symbol_state.get(
            "entry_price"
        )
        is None
    ):

        p = (
            r.get(
                "potential"
            )
            or {}
        )

        symbol_state[
            "entry_price"
        ] = price

        symbol_state[
            "entry_time"
        ] = now_iso()

        symbol_state[
            "entry_target_price"
        ] = p.get(
            "target_price"
        )

        symbol_state[
            "entry_original_potential_pct"
        ] = p.get(
            "potential_pct"
        )

        symbol_state[
            "entry_grade"
        ] = p.get(
            "grade"
        )

        symbol_state[
            "entry_tp1"
        ] = p.get(
            "tp1"
        )

        symbol_state[
            "entry_tp2"
        ] = p.get(
            "tp2"
        )

        symbol_state[
            "entry_resistance"
        ] = p.get(
            "resistance"
        )

        symbol_state[
            "entry_atr_pct"
        ] = p.get(
            "atr_pct"
        )

        symbol_state[
            "entry_volume_ratio"
        ] = p.get(
            "volume_ratio"
        )


        symbol_state[
            "entry_turnover_5m"
        ] = p.get(
            "turnover_5m"
        )

        symbol_state[
            "entry_turnover_1h"
        ] = p.get(
            "turnover_1h"
        )

        symbol_state[
            "entry_spread_pct"
        ] = p.get(
            "spread_pct"
        )

        symbol_state[
            "entry_liquidity"
        ] = p.get(
            "liquidity"
        )

        symbol_state[
            "entry_mode"
        ] = r.get(
            "entry_mode",
            "TREND"
        )

        symbol_state[
            "breakout_resistance"
        ] = r.get(
            "15m_breakout_resistance"
        )

        symbol_state[
            "breakout_pct"
        ] = r.get(
            "15m_breakout_pct"
        )

        symbol_state[
            "breakout_body_pct"
        ] = r.get(
            "5m_breakout_body_pct"
        )

        symbol_state[
            "breakout_volume_ratio"
        ] = r.get(
            "5m_breakout_volume_ratio"
        )


# ============================================================
# 目前階段統計
# ============================================================

def stage_stats(
    symbol_state
):

    current = symbol_state.get(
        "current_price"
    )

    watch_price = symbol_state.get(
        "watch_price"
    )

    ready_price = symbol_state.get(
        "ready_price"
    )

    entry_price = symbol_state.get(
        "entry_price"
    )

    target_price = symbol_state.get(
        "entry_target_price"
    )


    watch_to_now = pct_change(
        watch_price,
        current
    )

    ready_to_now = pct_change(
        ready_price,
        current
    )

    entry_to_now = pct_change(
        entry_price,
        current
    )


    remaining_potential = None

    if (
        target_price is not None
        and
        current is not None
        and
        current > 0
    ):

        remaining_potential = (
            target_price
            / current
            - 1
        ) * 100


    return {
        "current_price":
            current,

        "watch_price":
            watch_price,

        "ready_price":
            ready_price,

        "entry_price":
            entry_price,

        "target_price":
            target_price,

        "watch_to_now":
            watch_to_now,

        "ready_to_now":
            ready_to_now,

        "entry_to_now":
            entry_to_now,

        "remaining_potential":
            remaining_potential,
    }


# ============================================================
# 階段文字
# ============================================================

def build_stage_block(
    symbol_state
):

    s = stage_stats(
        symbol_state
    )

    lines = []


    if s["watch_price"] is not None:

        lines.append(
            "WATCH："
            + price_text(
                s["watch_price"]
            )
        )


    if s["ready_price"] is not None:

        lines.append(
            "READY："
            + price_text(
                s["ready_price"]
            )
        )


    if s["entry_price"] is not None:

        lines.append(
            "ENTRY："
            + price_text(
                s["entry_price"]
            )
        )


    lines.append(
        "目前："
        + price_text(
            s["current_price"]
        )
    )


    lines.append("")


    if s["watch_to_now"] is not None:

        lines.append(
            "WATCH→目前："
            + pct_text(
                s["watch_to_now"]
            )
        )


    if s["ready_to_now"] is not None:

        lines.append(
            "READY→目前："
            + pct_text(
                s["ready_to_now"]
            )
        )


    if s["entry_to_now"] is not None:

        lines.append(
            "ENTRY→目前："
            + pct_text(
                s["entry_to_now"]
            )
        )


    return "\n".join(
        lines
    )


# ============================================================
# ENTRY 完整通知
# ============================================================

def send_entry(
    r,
    symbol_state
):

    stats = stage_stats(
        symbol_state
    )

    original_pct = symbol_state.get(
        "entry_original_potential_pct"
    )

    target_price = symbol_state.get(
        "entry_target_price"
    )

    remaining_pct = stats.get(
        "remaining_potential"
    )

    grade = symbol_state.get(
        "entry_grade",
        "N/A"
    )

    tp1 = symbol_state.get(
        "entry_tp1"
    )

    tp2 = symbol_state.get(
        "entry_tp2"
    )

    entry_price_for_cost = symbol_state.get("entry_price")
    one_way_fee = fee_rate_for_group(r["group"])
    breakeven_price = net_exit_price(entry_price_for_cost, r["group"], 0.0)
    net_tp3_price = net_exit_price(entry_price_for_cost, r["group"], 3.0)
    net_tp5_price = net_exit_price(entry_price_for_cost, r["group"], 5.0)

    resistance = symbol_state.get(
        "entry_resistance"
    )

    atr_pct = symbol_state.get(
        "entry_atr_pct"
    )

    volume_ratio = symbol_state.get(
        "entry_volume_ratio"
    )

    turnover_5m = symbol_state.get(
        "entry_turnover_5m"
    )

    turnover_1h = symbol_state.get(
        "entry_turnover_1h"
    )

    spread_pct = symbol_state.get(
        "entry_spread_pct"
    )

    liquidity = symbol_state.get(
        "entry_liquidity",
        "N/A"
    )

    entry_mode = symbol_state.get(
        "entry_mode",
        "TREND"
    )

    breakout_resistance = (
        symbol_state.get(
            "breakout_resistance"
        )
    )

    breakout_pct = (
        symbol_state.get(
            "breakout_pct"
        )
    )

    breakout_body_pct = (
        symbol_state.get(
            "breakout_body_pct"
        )
    )

    breakout_volume_ratio = (
        symbol_state.get(
            "breakout_volume_ratio"
        )
    )

    priority = (
        "high"
        if grade in (
            "OK",
            "HIGH"
        )
        else "default"
    )

    volume_ratio_text = (
        f"{volume_ratio:.2f}x"
        if volume_ratio is not None
        else "N/A"
    )

    breakout_volume_text = (
        f"{breakout_volume_ratio:.2f}x"
        if breakout_volume_ratio is not None
        else "N/A"
    )

    spread_text = (
        f"{spread_pct:.3f}%"
        if spread_pct is not None
        else "N/A"
    )

    if entry_mode == "BREAKOUT":

        condition_block = (
            f"模式：BREAKOUT ENTRY\n"
            f"✅ 1H 不明顯偏空=True\n"
            f"✅ 15m 突破=True\n"
            f"✅ 5m 爆量大陽=True\n"
            f"✅ 剩餘空間門檻：≥{MIN_ENTRY_SPACE_PCT:.1f}%\n\n"

            f"15m突破壓力：{price_text(breakout_resistance)}\n"
            f"突破幅度：{pct_text(breakout_pct)}\n"
            f"5m實體漲幅：{pct_text(breakout_body_pct)}\n"
            f"5m突破量能比：{breakout_volume_text}\n\n"
        )

        title = (
            f"3-5% BREAKOUT ENTRY {r['base']}"
        )

    else:

        condition_block = (
            f"模式：TREND ENTRY\n"
            f"✅ 1H=True\n"
            f"✅ 15m=True\n"
            f"✅ 5m=True\n"
            f"✅ 剩餘空間門檻：≥{MIN_ENTRY_SPACE_PCT:.1f}%\n\n"
        )

        title = (
            f"3-5% ENTRY {r['base']}"
        )

    msg = (
        f"群組：{r['group']}\n"
        f"合約：{r['contract']}\n"
        f"{build_2560_notify_block(r)}\n"

        f"{build_stage_block(symbol_state)}\n\n"

        f"{condition_block}"

        f"固定SETUP目標：{price_text(target_price)}\n"
        f"SETUP原始空間：{pct_text(original_pct)}\n"
        f"剩餘預估空間：{pct_text(remaining_pct)}\n"
        f"潛力等級：{grade}\n\n"

        f"TP1（技術）：{price_text(tp1)}\n"
        f"TP2（技術）：{price_text(tp2)}\n"
        f"單邊手續費：{one_way_fee * 100:.3f}%\n"
        f"含買賣費損益兩平：{price_text(breakeven_price)}\n"
        f"淨利3%出場價：{price_text(net_tp3_price)}\n"
        f"淨利5%出場價：{price_text(net_tp5_price)}\n"
        f"最近壓力：{price_text(resistance)}\n"
        f"1H ATR：{pct_text(atr_pct)}\n\n"

        f"5m成交額：{money_text(turnover_5m)}\n"
        f"近1H成交額：{money_text(turnover_1h)}\n"
        f"5m量能比：{volume_ratio_text}\n"
        f"Spread：{spread_text}\n"
        f"流動性：{liquidity}\n\n"

        f"流動性僅供人工判斷，不阻擋訊號。\n"
        f"人工複核後再決定是否進場。"
    )

    send_ntfy(
        title,
        msg,
        priority,
        "chart_with_upwards_trend,bell"
    )


def bias_zh(value):
    return {
        "POSITIVE": "偏多",
        "NEUTRAL_POSITIVE": "中性偏多",
        "NEUTRAL": "中性",
        "NEGATIVE": "偏空",
        "N/A": "N/A",
        None: "N/A",
    }.get(value, str(value))


def build_2560_notify_block(r):
    return (
        f"2560狀態：{r.get('2560_status', 'N/A')}\n"
        f"2560方向：{bias_zh(r.get('2560_bias'))}\n"
    )


# ============================================================
# 狀態通知
# ============================================================

def notify_status(
    r,
    state
):

    symbol = r["base"]
    status = r["status"]

    symbol_state = (
        state
        .setdefault(
            "symbols",
            {}
        )
        .setdefault(
            symbol,
            {}
        )
    )

    previous = symbol_state.get(
        "status",
        "UNKNOWN"
    )

    print(
        f"{symbol}: "
        f"{previous} -> {status}"
    )

    update_stage_memory(
        r,
        symbol_state,
        previous
    )

    if status == previous:
        return

    # --------------------------------------------------------
    # 正式 ENTRY
    # --------------------------------------------------------

    if status in (
        "ENTRY",
        "BREAKOUT_ENTRY"
    ):

        send_entry(
            r,
            symbol_state
        )

    # --------------------------------------------------------
    # 原 TREND ENTRY CHECK
    # --------------------------------------------------------

    elif status == "ENTRY_CHECK":

        p = (
            r.get(
                "potential"
            )
            or {}
        )

        remaining = p.get(
            "potential_pct"
        )

        spread_pct = p.get(
            "spread_pct"
        )

        spread_text = (
            f"{spread_pct:.3f}%"
            if spread_pct is not None
            else "N/A"
        )

        volume_ratio = p.get(
            "volume_ratio"
        )

        volume_ratio_text = (
            f"{volume_ratio:.2f}x"
            if volume_ratio is not None
            else "N/A"
        )

        send_ntfy(
            f"3-5% ENTRY CHECK {symbol}",
            (
                f"群組：{r['group']}\n"
                f"合約：{r['contract']}\n"
                f"{build_2560_notify_block(r)}\n"

                f"{build_stage_block(symbol_state)}\n\n"

                f"模式：TREND\n"
                f"1H=True\n"
                f"15m=True\n"
                f"5m=True\n\n"

                f"5m 已達標，但目前預估空間："
                f"{pct_text(remaining)}\n"
                f"正式 ENTRY 最低要求："
                f"+{MIN_ENTRY_SPACE_PCT:.2f}%\n\n"

                f"5m成交額："
                f"{money_text(p.get('turnover_5m'))}\n"
                f"近1H成交額："
                f"{money_text(p.get('turnover_1h'))}\n"
                f"5m量能比："
                f"{volume_ratio_text}\n"
                f"Spread：{spread_text}\n"
                f"流動性：{p.get('liquidity', 'N/A')}\n\n"

                f"目前不列正式 ENTRY，繼續每1分鐘追蹤。"
            ),
            "default",
            "eyes"
        )

    # --------------------------------------------------------
    # BREAKOUT CHECK
    # --------------------------------------------------------

    elif status == "BREAKOUT_CHECK":

        p = (
            r.get(
                "potential"
            )
            or {}
        )

        remaining = p.get(
            "potential_pct"
        )

        spread_pct = p.get(
            "spread_pct"
        )

        spread_text = (
            f"{spread_pct:.3f}%"
            if spread_pct is not None
            else "N/A"
        )

        send_ntfy(
            f"3-5% BREAKOUT CHECK {symbol}",
            (
                f"群組：{r['group']}\n"
                f"合約：{r['contract']}\n"
                f"{build_2560_notify_block(r)}\n"

                f"{build_stage_block(symbol_state)}\n\n"

                f"1H不明顯偏空=True\n"
                f"15m突破=True\n"
                f"5m爆量大陽=True\n\n"

                f"15m突破壓力："
                f"{price_text(r.get('15m_breakout_resistance'))}\n"
                f"突破幅度："
                f"{pct_text(r.get('15m_breakout_pct'))}\n"
                f"5m實體："
                f"{pct_text(r.get('5m_breakout_body_pct'))}\n\n"

                f"目前預估空間："
                f"{pct_text(remaining)}\n"
                f"正式 BREAKOUT ENTRY 最低要求："
                f"+{MIN_ENTRY_SPACE_PCT:.2f}%\n\n"

                f"目前不列正式 ENTRY，繼續每1分鐘追蹤。"
            ),
            "default",
            "eyes"
        )

    # --------------------------------------------------------
    # READY
    # --------------------------------------------------------

    elif status == "READY":

        p = (
            r.get(
                "potential"
            )
            or {}
        )

        send_ntfy(
            f"3-5% READY {symbol}",
            (
                f"群組：{r['group']}\n"
                f"合約：{r['contract']}\n"
                f"{build_2560_notify_block(r)}\n"

                f"{build_stage_block(symbol_state)}\n\n"

                f"1H=True\n"
                f"15m=True\n"
                f"5m=False\n\n"

                f"固定SETUP目標：{price_text(p.get('target_price'))}\n"
                f"目前剩餘空間：{pct_text(p.get('potential_pct'))}\n"
                f"READY 最低要求：+{MIN_READY_SPACE_PCT:.2f}%\n\n"

                f"✅ 空間足夠，已進入 READY。\n"
                f"接下來每1分鐘快掃 5m。\n"
                f"SETUP目標現在起固定，不再跟著價格上移。\n"
                f"5m 達標時，用固定目標計算剩餘空間，\n"
                f"仍 ≥{MIN_ENTRY_SPACE_PCT:.1f}% 才會另發正式 ENTRY。"
            ),
            "default",
            "eyes"
        )

    # --------------------------------------------------------
    # BREAKOUT WATCH
    # --------------------------------------------------------

    elif status == "BREAKOUT_WATCH":

        p = (
            r.get(
                "potential"
            )
            or {}
        )

        send_ntfy(
            f"3-5% BREAKOUT WATCH {symbol}",
            (
                f"群組：{r['group']}\n"
                f"合約：{r['contract']}\n"
                f"{build_2560_notify_block(r)}\n"

                f"{build_stage_block(symbol_state)}\n\n"

                f"1H完整多頭=False\n"
                f"1H不明顯偏空=True\n"
                f"15m近期壓力："
                f"{price_text(r.get('15m_breakout_resistance'))}\n"
                f"距壓力："
                f"{pct_text(r.get('15m_breakout_distance_pct'))}\n\n"

                f"目前預估空間："
                f"{pct_text(p.get('potential_pct'))}\n\n"

                f"已靠近突破區，接下來每1分鐘快掃。\n"
                f"需同時出現15m突破 + 5m爆量大陽 + 剩餘空間≥"
                f"{MIN_ENTRY_SPACE_PCT:.1f}% 才會發 BREAKOUT ENTRY。"
            ),
            "default",
            "eyes"
        )

    # --------------------------------------------------------
    # WATCH
    # --------------------------------------------------------

    elif status == "WATCH":

        p = (
            r.get(
                "potential"
            )
            or {}
        )

        if r.get("15m_ready"):

            watch_detail = (
                f"1H=True\n"
                f"15m=True\n"
                f"5m=False\n\n"
                f"固定SETUP目標：{price_text(p.get('target_price'))}\n"
                f"目前剩餘空間：{pct_text(p.get('potential_pct'))}\n"
                f"READY 最低要求：+{MIN_READY_SPACE_PCT:.2f}%\n\n"
                f"⚠️ 空間不足，暫不進入 TREND 1分鐘快掃。\n"
                f"保留 WATCH，等待價格回落或上方空間改善。"
            )

        else:

            watch_detail = (
                f"1H=True\n"
                f"15m=False\n"
                f"5m=False\n\n"
                f"等待15m READY。"
            )

        send_ntfy(
            f"3-5% WATCH {symbol}",
            (
                f"群組：{r['group']}\n"
                f"合約：{r['contract']}\n"
                f"{build_2560_notify_block(r)}\n"
                f"{build_stage_block(symbol_state)}\n\n"
                f"{watch_detail}"
            ),
            "default",
            "eyes"
        )

    # --------------------------------------------------------
    # NO SIGNAL
    # --------------------------------------------------------

    elif status == "NO_SIGNAL":

        if previous in (
            "WATCH",
            "BREAKOUT_WATCH",
            "READY",
            "ENTRY_CHECK",
            "BREAKOUT_CHECK",
            "ENTRY",
            "BREAKOUT_ENTRY"
        ):

            send_ntfy(
                f"3-5% invalid {symbol}",
                (
                    f"群組：{r['group']}\n"
                    f"{symbol} 短打環境失效\n"
                    f"{build_2560_notify_block(r)}\n"

                    f"{build_stage_block(symbol_state)}\n\n"

                    f"前一狀態：{previous}\n"
                    f"目前：{price_text(r['price'])}\n\n"

                    f"暫停做多。"
                ),
                "default",
                "warning"
            )

    symbol_state[
        "status"
    ] = status

    symbol_state[
        "updated_utc"
    ] = now_iso()


# ============================================================
# 摘要
# ============================================================

def summary_due(
    state,
    force=False
):

    if force:
        return True


    last = state.get(
        "last_summary_utc"
    )


    if not last:
        return True


    try:

        last_dt = datetime.fromisoformat(
            last
        )

        now = datetime.now(
            timezone.utc
        )

        return (
            (
                now
                - last_dt
            ).total_seconds()

            >= SUMMARY_INTERVAL
        )

    except Exception:

        return True


def format_group(
    group,
    results,
    state
):

    entry = []
    breakout_entry = []
    entry_check = []
    breakout_check = []
    ready = []
    breakout_watch = []
    watch = []

    for r in results:

        if r.get(
            "group"
        ) != group:
            continue

        symbol = r.get(
            "base"
        )

        status = r.get(
            "status"
        )

        symbol_state = (
            state
            .get(
                "symbols",
                {}
            )
            .get(
                symbol,
                {}
            )
        )

        if status == "ENTRY":

            stats = stage_stats(
                symbol_state
            )

            remaining = stats.get(
                "remaining_potential"
            )

            if remaining is not None:

                entry.append(
                    f"{symbol}"
                    f"({remaining:+.1f}%)"
                )

            else:

                entry.append(
                    symbol
                )

        elif status == "BREAKOUT_ENTRY":

            stats = stage_stats(
                symbol_state
            )

            remaining = stats.get(
                "remaining_potential"
            )

            if remaining is not None:

                breakout_entry.append(
                    f"{symbol}"
                    f"({remaining:+.1f}%)"
                )

            else:

                breakout_entry.append(
                    symbol
                )

        elif status == "ENTRY_CHECK":

            entry_check.append(
                symbol
            )

        elif status == "BREAKOUT_CHECK":

            breakout_check.append(
                symbol
            )

        elif status == "READY":

            ready.append(
                symbol
            )

        elif status == "BREAKOUT_WATCH":

            breakout_watch.append(
                symbol
            )

        elif status == "WATCH":

            watch.append(
                symbol
            )

    def show(items):

        return (
            "、".join(items)
            if items
            else "無"
        )

    return (
        f"[{group}]\n"
        f"ENTRY：{show(entry)}\n"
        f"BREAKOUT_ENTRY：{show(breakout_entry)}\n"
        f"ENTRY_CHECK：{show(entry_check)}\n"
        f"BREAKOUT_CHECK：{show(breakout_check)}\n"
        f"READY：{show(ready)}\n"
        f"BREAKOUT_WATCH：{show(breakout_watch)}\n"
        f"WATCH：{show(watch)}"
    )


def send_environment_summary(
    results,
    state,
    error_count,
    force=False
):

    if not summary_due(
        state,
        force=force
    ):

        print(
            "SUMMARY: not due"
        )

        return


    message = (
        f"{format_group('CRYPTO', results, state)}\n\n"
        f"{format_group('US-STOCK-PERP', results, state)}\n\n"
        f"{format_group('HK-STOCK', results, state)}\n\n"
        f"本輪錯誤：{error_count}\n"
        f"主巡查：每10分鐘；READY / ENTRY_CHECK / BREAKOUT_WATCH / BREAKOUT_CHECK 後每1分鐘快掃。"
    )


    success = send_ntfy(
        "3-5% Short Monitor Summary",
        message,
        "default",
        "bar_chart"
    )


    if success:

        state[
            "last_summary_utc"
        ] = now_iso()


def apply_locked_setup_target(
    r,
    symbol_state
):

    """
    v2.1 核心修正：
    SETUP 成立後，目標價固定。
    後續快掃只以 fixed_target / current_price - 1 計算剩餘空間。
    不允許因為價格上漲又重新把目標往上推。
    """

    p = r.get("potential")

    if not p:
        return r

    current_price = r.get("price")

    if current_price is None or current_price <= 0:
        return r

    trend_setup = (
        r.get("1h_trend")
        and r.get("15m_ready")
    )

    breakout_signal = (
        r.get("1h_not_bearish")
        and r.get("15m_breakout")
        and r.get("5m_breakout")
    )

    breakout_watch = bool(
        r.get("breakout_watch")
    )

    # 原本這一輪重新計算出的目標留作診斷，
    # 但不再拿它決定正式 ENTRY。
    p["live_recalc_target_price"] = p.get("target_price")
    p["live_recalc_space_pct"] = p.get("potential_pct")

    fixed_target = None
    fixed_setup_price = None
    fixed_original_space = None
    fixed_mode = None

    # TREND 通道優先，跟原狀態判斷一致。
    if trend_setup:
        fixed_target = symbol_state.get("trend_setup_target_price")
        fixed_setup_price = symbol_state.get("trend_setup_price")
        fixed_original_space = symbol_state.get("trend_setup_original_space_pct")
        fixed_mode = "TREND"

    elif breakout_signal or breakout_watch:
        fixed_target = symbol_state.get("breakout_setup_target_price")
        fixed_setup_price = symbol_state.get("breakout_setup_price")
        fixed_original_space = symbol_state.get("breakout_setup_original_space_pct")
        fixed_mode = "BREAKOUT"

    if fixed_target is None:
        # 第一次出現 setup，尚未寫入 state。
        # 這輪仍使用當下估算，update_stage_memory 會立即鎖定。
        return r

    remaining = pct_change(
        current_price,
        fixed_target
    )

    p["target_price"] = fixed_target
    p["potential_pct"] = remaining
    p["fixed_setup_target"] = True
    p["setup_mode"] = fixed_mode
    p["setup_price"] = fixed_setup_price
    p["setup_original_space_pct"] = fixed_original_space

    # TP 也以固定 setup target 為上限重新顯示。
    if fixed_setup_price is not None and fixed_setup_price > 0:
        original_space = pct_change(
            fixed_setup_price,
            fixed_target
        )

        if original_space is not None:
            tp1_pct = min(
                max(original_space * 0.60, 0.0),
                3.0
            )
            tp2_pct = min(
                max(original_space, 0.0),
                5.0
            )
            p["tp1"] = fixed_setup_price * (1 + tp1_pct / 100)
            p["tp2"] = min(
                fixed_setup_price * (1 + tp2_pct / 100),
                fixed_target
            )

    # 重新用「固定目標的剩餘空間」判斷狀態。
    if trend_setup:
        r["entry_mode"] = "TREND"

        if r.get("5m_entry"):
            if (
                remaining is not None
                and remaining >= MIN_ENTRY_SPACE_PCT
            ):
                r["status"] = "ENTRY"
            else:
                r["status"] = "ENTRY_CHECK"
        else:
            if (
                remaining is not None
                and remaining >= MIN_READY_SPACE_PCT
            ):
                r["status"] = "READY"
            else:
                r["status"] = "WATCH"

    elif breakout_signal:
        r["entry_mode"] = "BREAKOUT"

        if (
            remaining is not None
            and remaining >= MIN_ENTRY_SPACE_PCT
        ):
            r["status"] = "BREAKOUT_ENTRY"
        else:
            r["status"] = "BREAKOUT_CHECK"

    elif breakout_watch:
        r["status"] = "BREAKOUT_WATCH"

    r["ready_space_ok"] = (
        remaining is not None
        and remaining >= MIN_READY_SPACE_PCT
    )

    r["setup_target_locked"] = True
    r["setup_target_price"] = fixed_target
    r["setup_remaining_space_pct"] = remaining

    return r


def process_result(
    r,
    state
):

    symbol = r["base"]

    if (
        r["status"]
        == "WAIT_HISTORY"
    ):

        print(
            f"{symbol:<14} WAIT_HISTORY"
        )

        return

    symbol_state = (
        state
        .setdefault(
            "symbols",
            {}
        )
        .setdefault(
            symbol,
            {}
        )
    )

    preview_previous = symbol_state.get(
        "status",
        "UNKNOWN"
    )

    # HK 每個交易日重新開始一輪狀態，避免昨天 READY
    # 讓今天第一個 READY 被誤認成重複通知。
    if r.get("group") == "HK-STOCK":

        today_hk = hk_trade_date()

        if (
            symbol_state.get(
                "hk_trade_date"
            )
            != today_hk
        ):

            reset_cycle(
                symbol_state
            )

            symbol_state[
                "status"
            ] = "UNKNOWN"

            symbol_state[
                "hk_trade_date"
            ] = today_hk

            preview_previous = "UNKNOWN"

    # v2.3：先把第一次出現的 setup 寫入 state，立即鎖定目標。
    # 接著再用固定 SETUP_TARGET 重算本輪剩餘空間與狀態。
    # 這樣第一輪 READY / BREAKOUT_WATCH 就已經有 FIXED_TARGET，
    # 後續快掃絕不允許目標跟著價格往上搬。
    update_stage_memory(
        r,
        symbol_state,
        preview_previous
    )

    r = apply_locked_setup_target(
        r,
        symbol_state
    )

    # 固定目標可能讓狀態從 READY 降成 WATCH、
    # 或從 ENTRY 降成 ENTRY_CHECK。用修正後狀態再同步一次階段記憶。
    update_stage_memory(
        r,
        symbol_state,
        preview_previous
    )

    stats = stage_stats(
        symbol_state
    )

    extra = ""

    if r.get("status") == "BREAKOUT_ENTRY":
        chase = breakout_chase_info(r)
        extra += (
            f" BO_R15={price_text(chase.get('bo_r15'))}"
            f" BO_DIST={pct_text(chase.get('distance_pct'))}"
            f" CHASE={chase.get('risk')}"
        )

    if r.get("potential") is not None:

        p = r.get("potential") or {}

        extra = (
            f" current="
            f"{price_text(stats['current_price'])}"

            f" potential="
            f"{pct_text(p.get('potential_pct'))}"
        )

    if r.get("15m_breakout_resistance") is not None:

        extra += (
            f" BO_R15="
            f"{price_text(r.get('15m_breakout_resistance'))}"

            f" BO15="
            f"{r.get('15m_breakout')}"

            f" BO5="
            f"{r.get('5m_breakout')}"
        )

    # v2.3：保存快掃需要的診斷值。BREAKOUT_WATCH 若離壓力太遠，
    # 狀態仍保留，但不再吃 1 分鐘快掃資源。
    symbol_state["last_breakout_distance_pct"] = r.get(
        "15m_breakout_distance_pct"
    )

    fixed_target = r.get("setup_target_price")
    fixed_left = r.get("setup_remaining_space_pct")

    if fixed_target is None:
        # 就算本輪狀態沒有套用到固定目標，也把 state 裡已鎖定的目標印出來，
        # 方便確認目標是否存在、是否曾被錯誤清掉。
        if r.get("1h_trend") and r.get("15m_ready"):
            fixed_target = symbol_state.get("trend_setup_target_price")
        elif r.get("breakout_watch") or r.get("15m_breakout"):
            fixed_target = symbol_state.get("breakout_setup_target_price")

        if fixed_target is not None:
            fixed_left = pct_change(
                r.get("price"),
                fixed_target
            )

    if fixed_target is not None:
        extra += (
            f" FIXED_TARGET={price_text(fixed_target)}"
            f" FIXED_LEFT={pct_text(fixed_left)}"
        )

    extra += f" 2560={r.get('2560_status','N/A')}"

    print(
        f"{symbol:<14} "
        f"{r['status']:<12} "
        f"contract={r['contract']:<18} "
        f"price={price_text(r['price'])} "
        f"1H={r['1h_trend']} "
        f"15m={r['15m_ready']} "
        f"5m={r['5m_entry']}"
        f"{extra}"
    )

    notify_status(
        r,
        state
    )


def fast_scan_ready(
    contracts,
    state,
    ctx2560
):

    for round_no in range(
        1,
        FAST_SCAN_ROUNDS + 1
    ):

        candidates = []

        for symbol, symbol_state in (
            state
            .get(
                "symbols",
                {}
            )
            .items()
        ):

            fast_status = symbol_state.get(
                "status"
            )

            if fast_status not in (
                "READY",
                "ENTRY_CHECK",
                "BREAKOUT_WATCH",
                "BREAKOUT_CHECK"
            ):
                continue

            # v2.3：BREAKOUT_WATCH 太遠就不進快掃。
            # 它仍保持 BREAKOUT_WATCH，等下一個10分鐘主巡查重新評估。
            if fast_status == "BREAKOUT_WATCH":
                bo_dist = symbol_state.get(
                    "last_breakout_distance_pct"
                )

                if (
                    bo_dist is None
                    or bo_dist > FAST_SCAN_BREAKOUT_DISTANCE_PCT
                ):
                    continue

            contract = contracts.get(
                symbol
            )

            if not contract:
                continue

            group = get_group(
                symbol
            )

            if (
                group == "HK-STOCK"
                and not hk_market_open()
            ):
                continue

            candidates.append(
                (
                    symbol,
                    contract
                )
            )

        if not candidates:

            print(
                "FAST SCAN: no READY / ENTRY_CHECK / BREAKOUT candidates"
            )

            return

        print(
            f"\nFAST SCAN {round_no}/{FAST_SCAN_ROUNDS} "
            f"| wait {FAST_SCAN_SECONDS}s "
            f"| close-only symbols={','.join(x[0] for x in candidates)}"
        )

        time.sleep(
            FAST_SCAN_SECONDS
        )

        for symbol, contract in candidates:

            if (
                get_group(symbol)
                == "HK-STOCK"
                and not hk_market_open()
            ):

                print(
                    f"{symbol}: HK market closed, stop fast scan"
                )

                continue

            try:

                r = finalize_35_result(
                    analyze(
                        symbol,
                        contract
                    ),
                    ctx2560
                )

                process_result(
                    r,
                    state
                )

                save_state(
                    state
                )

            except Exception as e:

                print(
                    f"{symbol}: FAST SCAN ERROR {e}"
                )

            time.sleep(
                0.12
            )


# ============================================================
# MAIN
# ============================================================



def short35_signal_log_line(result):
    """有訊號時統一輸出 35 + 2560 + 現價 + 固定目標 + 剩餘空間。"""
    symbol = result.get("symbol") or result.get("base") or "?"
    if result.get("group") == "HK-STOCK":
        label = hk_display_label(symbol)
    else:
        label = symbol

    current = (
        result.get("display_current_price")
        or result.get("price")
        or result.get("price_5m")
        or result.get("latest_close")
    )
    target = (
        result.get("display_target_price")
        or result.get("setup_target")
        or result.get("fixed_target")
    )
    left = result.get("remaining_space_pct")

    return (
        f"{label} {result.get('status')} "
        f"| 2560={result.get('2560_status','N/A')} "
        f"| now={price_text(current)} "
        f"| target={price_text(target)} "
        f"| left={pct_text(left)}"
    )


def notify_35_signal_detail(result):
    """狀態變化時的完整通知。"""
    status = result.get("status")
    if status in (None, "NO_SIGNAL", "NOT_FOUND", "WAIT_HISTORY", "STALE_DATA"):
        return False

    title_symbol = (
        hk_display_label(result.get("symbol"))
        if result.get("group") == "HK-STOCK"
        else str(result.get("symbol") or result.get("base"))
    )

    return send_ntfy(
        f"35 {status} {title_symbol}",
        format_signal_detail_35(result),
        "default",
        "chart_with_upwards_trend",
    )

def finalize_35_result(result, ctx2560=None):
    # 2560 已由 analyze() 自己計算，不再依賴另一個 workflow 的 JSON。
    result = apply_embedded_2560_filter(result)
    result = enrich_signal_output(result)
    if result.get("group") == "HK-STOCK":
        result["display_name"] = hk_display_label(
            result.get("symbol") or result.get("base")
        )
    return result

def main():
    ctx2560 = None
    print("2560 CONTEXT: SELF-CONTAINED (calculated inside 35)")

    print(
        "3~5% Short Monitor | FINAL 2026-09-29 MAIN"
    )

    print(
        now_iso()
    )

    print(
        "Manual run:",
        MANUAL_RUN
    )

    print(
        "HK market open:",
        hk_market_open()
    )

    state = load_state()

    contracts = discover_contracts()
    hk_contracts = discover_hk_stock_symbols()
    contracts.update(hk_contracts)

    print(
        "HK Gate stock whitelist:",
        ", ".join(
            f"{k}={v or 'NOT_FOUND'}"
            for k, v in hk_contracts.items()
        )
    )

    results = []
    errors = []

    for group, symbols in GROUPS.items():

        print(
            f"\n===== {group} ====="
        )

        # 港股只跟香港正式交易時段。
        # CRYPTO / US-STOCK-PERP 不受此限制。
        if (
            group == "HK-STOCK"
            and not hk_market_open()
        ):

            print(
                "HK-STOCK SKIP: outside 09:30-12:00 / 13:00-16:00 Hong Kong time"
            )

            continue

        for symbol in symbols:

            contract = contracts.get(
                symbol
            )

            if not contract:

                print(
                    f"{symbol:<14} NOT_FOUND"
                )

                continue

            try:

                r = finalize_35_result(
                    analyze(
                        symbol,
                        contract
                    ),
                    ctx2560
                )

                results.append(
                    r
                )

                process_result(
                    r,
                    state
                )

            except Exception as e:

                errors.append(
                    (
                        symbol,
                        str(e)
                    )
                )

                print(
                    f"{symbol:<14} ERROR {e}"
                )

            time.sleep(
                0.12
            )

    send_environment_summary(
        results,
        state,
        len(errors),
        force=MANUAL_RUN
    )

    # 先存一次主巡查狀態
    save_state(
        state
    )

    # 有 READY / ENTRY_CHECK / BREAKOUT_WATCH / BREAKOUT_CHECK
    # 才留下來進行 1 分鐘快掃；平常無候選就立刻結束。
    fast_scan_ready(
        contracts,
        state,
        ctx2560
    )

    save_state(
        state
    )

    counts = {}

    for symbol_state in (
        state
        .get(
            "symbols",
            {}
        )
        .values()
    ):

        status = symbol_state.get(
            "status",
            "UNKNOWN"
        )

        counts[
            status
        ] = (
            counts.get(
                status,
                0
            )
            + 1
        )

    print(
        "\nSTATUS COUNTS:",
        counts
    )

    print(
        "ERROR COUNT:",
        len(errors)
    )

    print(
        "STATE FILE:",
        STATE_FILE
    )


if __name__ == "__main__":
    main()
