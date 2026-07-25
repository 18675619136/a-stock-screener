"""
Data fetcher — reusable data-fetching utilities extracted from existing scripts.

Provides:
    - get_all_a_stocks()     — from Sina
    - get_market_data()      — from Tencent (market cap, circulating shares)
    - get_kline()            — from Tencent (daily kline data)
    - get_sector_rankings()  — sector index performance
"""

import json
import re
import time
import sys
import urllib.request
from typing import Any

SINA_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://finance.sina.com.cn",
}
TENCENT_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://gu.qq.com",
}

SINA_ALL_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeDataSimple?page=1&num=5000"
    "&sort=changepercent&asc=0&node=hs_a&symbol=&_s_r_a=page"
)

TENCENT_MV_URL = "https://qt.gtimg.cn/q={syms}"
TENCENT_KLINE_URL = (
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,500,qfq"
)

SECTOR_INDICES = [
    "sz399928", "sz399929", "sz399930", "sz399931", "sz399932", "sz399933",
    "sz399934", "sz399935", "sz399936", "sz399937", "sz399971", "sz399973",
    "sz399974", "sz399975", "sz399976", "sz399977", "sz399440", "sz399441",
    "sz399967", "sz399998", "sz399989", "sz399994", "sz399987", "sz399395",
    "sz399396", "sz399393", "sz399389", "sz399997", "sz399995", "sz399996",
    "sz399993", "sz399992", "sz399986", "sz399990", "sz399275", "sz399276",
    "sz399978", "sz399579", "sz399242", "sz399248", "sz399234", "sz399232",
    "sz399972", "sz399951", "sz399952", "sz399953", "sz399954", "sz399955",
    "sz399956", "sz399957", "sz399958", "sz399985",
]

SECTOR_NAMES = {
    "sz399928": "中证能源", "sz399929": "中证材料", "sz399930": "中证工业",
    "sz399931": "中证可选", "sz399932": "中证消费", "sz399933": "中证医药",
    "sz399934": "中证金融", "sz399935": "中证信息", "sz399936": "中证电信",
    "sz399937": "中证公用", "sz399971": "中证传媒", "sz399973": "中证国防",
    "sz399974": "国企改革", "sz399975": "证券公司", "sz399976": "CS新能车",
    "sz399977": "内地低碳", "sz399440": "国证钢铁", "sz399441": "生物医药",
    "sz399967": "中证军工", "sz399998": "中证煤炭", "sz399989": "中证医疗",
    "sz399994": "信息安全", "sz399987": "中证酒", "sz399395": "国证有色",
    "sz399396": "国证食品", "sz399393": "国证地产", "sz399389": "国证通信",
    "sz399997": "中证白酒", "sz399995": "基建工程", "sz399996": "智能家居",
    "sz399993": "CSWD生科", "sz399992": "CSWD并购", "sz399986": "中证银行",
    "sz399990": "煤炭等权", "sz399275": "创医药", "sz399276": "创科技",
    "sz399978": "医药100", "sz399579": "中证中药", "sz399242": "商务指数",
    "sz399248": "文化指数", "sz399234": "水电指数", "sz399232": "采矿指数",
    "sz399972": "300深市", "sz399951": "300银行", "sz399952": "300地产",
    "sz399953": "中证地企", "sz399954": "地企100", "sz399956": "国企200",
    "sz399955": "中证国企", "sz399957": "300运输", "sz399958": "创业成长",
    "sz399985": "中证全指",
}

TRACK_KW = [
    ("半导体/芯片", ["半导体", "芯片", "微电", "集成电路", "晶圆", "封装", "光刻", "存储", "GPU", "AI芯片", "中芯", "华创", "华大"]),
    ("创新药/医药", ["创新药", "生物", "医药", "恒瑞", "百济", "药明", "凯莱英", "基因", "医疗", "制药", "药"]),
    ("AI/人工智能", ["AI", "人工智能", "大模型", "智能体", "视觉", "语音", "讯飞", "拓尔思", "云从", "算法", "软件"]),
    ("人形机器人", ["机器人", "绿的谐波", "减速器", "丝杠", "执行器", "灵巧手", "伺服", "电机"]),
    ("低空经济", ["低空", "无人机", "eVTOL", "飞行汽车", "空管", "通航"]),
    ("智能驾驶", ["自动驾驶", "无人驾驶", "激光雷达", "ADAS", "域控制器", "智驾"]),
    ("军工/国防", ["军工", "航天", "航空", "北斗", "卫星", "中航", "雷达", "国防", "船舶", "航发", "沈飞", "中兵", "电科"]),
    ("新能源/储能", ["新能源", "光伏", "储能", "电池", "宁德", "锂电", "逆变器", "固态电池", "风电", "氢能"]),
    ("通信/算力", ["通信", "光通信", "光模块", "算力", "服务器", "数据中心", "IDC", "5G", "6G", "光纤"]),
    ("金融", ["证券", "银行", "保险", "中信", "招商", "兴业", "平安", "金融"]),
    ("新材料", ["有色", "金属", "钨", "锂", "钴", "镍", "稀土", "石英", "材料", "化工", "化学", "化纤"]),
    ("消费", ["消费", "家电", "白酒", "食品", "饮料", "茅台", "五粮液", "伊利", "乳业", "零售", "美的", "格力", "海尔"]),
    ("电力/公用", ["电力", "水电", "风电", "核电", "电网", "能源", "燃气", "水务", "环保"]),
    ("传媒/游戏", ["传媒", "游戏", "影视", "广告", "直播", "短剧", "动漫", "文化"]),
    ("地产/基建", ["地产", "基建", "房地产", "万科", "保利", "建筑", "中铁", "中交", "交建", "路桥"]),
    ("其他", []),
]


def log(msg: str) -> None:
    """Log to stderr (non-interfering with stdout output)."""
    print(msg, file=sys.stderr, flush=True)


def safe_float(v, default: float = 0.0) -> float:
    """Safely convert a value to float."""
    try:
        return float(v) if v and str(v).strip() not in ("-", "") else default
    except (ValueError, TypeError):
        return default


def fetch_url(url: str, headers: dict | None = None, timeout: int = 15) -> bytes | None:
    """Fetch a URL with error handling."""
    req = urllib.request.Request(url, headers=headers or SINA_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        log(f"  [WARN] fetch failed: {url[:60]}... {e}")
        return None


def strip_prefix(sym: str) -> str:
    """Remove exchange prefix from symbol (e.g. 'sh600000' -> '600000')."""
    return re.sub(r'^(sh|sz|bj)', '', sym)

def code_to_prefix(code: str) -> str:
    """Map stock code to exchange prefix."""
    if code.startswith("92"):
        return "bj"
    elif code.startswith(("6", "9", "68")):
        return "sh"
    elif code.startswith(("0", "3")):
        return "sz"
    elif code.startswith(("8", "4")):
        return "bj"
    return ""


def match_track(name: str) -> str:
    """Match a stock name to a hot track/category."""
    for tn, kws in TRACK_KW:
        if tn == "其他":
            continue
        for kw in kws:
            if kw in name:
                return tn
    return "其他"


# ── Sector keywords extraction ──────────────────────────────────

_SECTOR_SUFFIXES = ("概念", "板块", "行业", "指数", "主题", "板块")


def _sector_keywords(name: str) -> list[str]:
    """Generate search keywords from a hot sector name for stock name matching.

    Examples:
        '工程建设'  -> ['工程建设', '工程', '建设']
        '半导体'    -> ['半导体', '半导']
        '酿酒概念'  -> ['酿酒', '酒']
        'AI人工智能' -> ['AI人工智能', 'AI']
    """
    cleaned = name
    for suffix in _SECTOR_SUFFIXES:
        cleaned = cleaned.replace(suffix, "")
    cleaned = cleaned.strip()
    if not cleaned:
        return []

    keywords = {cleaned}  # whole cleaned name
    # 2-char segments for longer names
    if len(cleaned) >= 4:
        keywords.add(cleaned[:2])
        keywords.add(cleaned[2:4])
    if len(cleaned) == 3:
        keywords.add(cleaned[:2])
    return [k for k in keywords if k]


# ── TRACK_KW category to hot sector name cross-reference ─────────
# Maps TRACK_KW categories -> possible East Money hot sector name keywords.
# Used as Layer 2 fallback when direct stock name matching fails.
TRACK_TO_SECTOR_TERMS: dict[str, list[str]] = {
    "半导体/芯片": ["半导体", "芯片", "集成电路", "电子"],
    "创新药/医药": ["医药", "生物", "医疗", "创新药", "中药"],
    "AI/人工智能": ["人工智能", "AI", "软件", "互联网", "大数据", "算力", "信创"],
    "人形机器人": ["机器人", "自动化", "机器"],
    "低空经济": ["低空", "航空", "无人机", "飞行"],
    "智能驾驶": ["汽车", "智能驾驶", "无人驾驶", "汽配", "新能源车"],
    "军工/国防": ["军工", "国防", "航天", "船舶", "航空"],
    "新能源/储能": ["新能源", "光伏", "储能", "电池", "风电", "氢能", "能源", "锂电"],
    "通信/算力": ["通信", "算力", "5G", "光通信", "光纤", "服务器", "数据中心"],
    "金融": ["金融", "证券", "银行", "保险", "券商"],
    "新材料": ["有色", "钢铁", "材料", "化工", "稀土", "金属", "化纤", "钨", "锂"],
    "消费": ["白酒", "消费", "食品", "家电", "酿酒", "饮料", "旅游", "乳业"],
    "电力/公用": ["电力", "能源", "环保", "水务", "燃气", "核电", "电网"],
    "传媒/游戏": ["传媒", "游戏", "影视", "文化", "广告", "短剧", "动漫"],
    "地产/基建": ["地产", "基建", "房地产", "工程", "建设", "建筑", "水泥"],
}


def match_stock_to_hot_sector(
    stock_name: str, hot_sectors: list[dict],
) -> tuple[str, float]:
    """Match a stock name to the best hot sector by name matching.

    Two-layer matching:
      1. Direct: check stock name against sector name keywords
         (e.g. '中芯国际' contains '芯' -> '半导体')
      2. Track-based: use match_track() to categorize stock, then
         cross-reference the track name against hot sector names via
         TRACK_TO_SECTOR_TERMS mapping

    Args:
        stock_name: stock name like '中国建筑'
        hot_sectors: list of {name, strength, ...}

    Returns:
        (sector_name, strength), or ("其他", 0.0) if no match
    """
    if not stock_name or not hot_sectors:
        return ("其他", 0.0)

    # Layer 1: Direct keyword matching (stock name ↔ sector name keywords)
    for sector in hot_sectors:
        for kw in _sector_keywords(sector["name"]):
            if kw and kw in stock_name:
                return (sector["name"], sector["strength"])

    # Layer 2: Track-based matching
    track = match_track(stock_name)
    if track == "其他":
        return ("其他", 0.0)

    search_terms = TRACK_TO_SECTOR_TERMS.get(track, [])
    if not search_terms:
        return ("其他", 0.0)

    for sector in hot_sectors:
        sec_name = sector["name"]
        for term in search_terms:
            if term in sec_name or sec_name in term:
                return (sector["name"], sector["strength"])

    return ("其他", 0.0)


class DataFetcher:
    """Centralized data fetching for all strategies."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    # ── Sina: full A-share list ─────────────────────────────────────

    def get_all_a_stocks(self) -> list[dict[str, Any]]:
        """Fetch all A-share stocks from Sina with basic info (price, change%, amount)."""
        raw = fetch_url(SINA_ALL_URL, timeout=self.config.get("timeout_sina", 30))
        if not raw:
            return []
        try:
            data = json.loads(raw.decode("gbk", errors="replace"))
        except json.JSONDecodeError:
            return []
        stocks = []
        for s in data:
            try:
                raw_code = s.get("symbol", "")
                trade = safe_float(s.get("trade", 0))
                settlement = safe_float(s.get("settlement", 0))
                price = trade if trade > 0 else settlement
                stocks.append({
                    "code": strip_prefix(raw_code),
                    "symbol": raw_code,
                    "name": s.get("name", ""),
                    "price": price,
                    "changepercent": safe_float(s.get("changepercent", 0)),
                    "amount": safe_float(s.get("amount", 0)),
                })
            except Exception:
                continue
        return stocks

    # ── Tencent: market data (MV, circ shares, etc.) ─────────────────

    def get_market_data(
        self, stocks: list[dict], batch_size: int = 80
    ) -> dict[str, dict]:
        """Fetch market cap and circulating shares from Tencent in batches.

        Args:
            stocks: List of stock dicts with 'code' field.
            batch_size: Stocks per batch request.

        Returns:
            Dict[code -> {name, price, mv, circ, ...}]
        """
        delay = self.config.get("request_delay", 0.3)
        result: dict[str, dict] = {}

        for i in range(0, len(stocks), batch_size):
            batch = stocks[i : i + batch_size]
            syms = []
            for s in batch:
                c = s.get("code", "")
                prefix = code_to_prefix(c)
                if prefix:
                    syms.append(f"{prefix}{c}")

            if not syms:
                continue

            url = f"https://qt.gtimg.cn/q={','.join(syms)}"
            raw = fetch_url(url, headers=TENCENT_HEADERS,
                            timeout=self.config.get("timeout_tencent", 15))
            if raw:
                try:
                    text = raw.decode("gbk", errors="replace")
                    for line in text.strip().split("\n"):
                        if '="' not in line:
                            continue
                        try:
                            val = line.split('="')[1].rstrip('"').rstrip(";")
                            parts = val.split("~")
                            if len(parts) < 58:
                                continue
                            code = parts[2]
                            result[code] = {
                                "code": code,
                                "name": parts[1],
                                "price": safe_float(parts[3]),
                                "mv": safe_float(parts[44]),          # 总市值（亿） — parts[44]=总市值, parts[43]=振幅%, parts[46]=市净率PB
                                "total_shares": safe_float(parts[44]) / safe_float(parts[3]) if safe_float(parts[3]) > 0 else 0,  # 总股本（亿股）= 总市值/价格
                                "changepercent": safe_float(parts[32]) if len(parts) > 32 else 0,
                                "amount": safe_float(parts[37]) if len(parts) > 37 else 0,
                            }
                        except Exception:
                            continue
                except Exception:
                    pass

            time.sleep(delay)
            if (i // batch_size + 1) % 10 == 0:
                log(f"  Market data: {i + batch_size}/{len(stocks)}")

        return result

    # ── Tencent: kline data ─────────────────────────────────────────

    def get_kline(self, sym: str) -> list[dict[str, float]] | None:
        """Fetch daily kline data for a single stock.

        Args:
            sym: e.g. "sh600519" or "sz000001"

        Returns:
            List of {close, volume, high, low, open} for recent N days, or None.
        """
        url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,500,qfq"
        for attempt in range(2):
            raw = fetch_url(url, headers=TENCENT_HEADERS,
                            timeout=self.config.get("timeout_kline", 10))
            if not raw or len(raw) < 50:
                time.sleep(1)
                continue
            try:
                parsed = json.loads(raw)
                data = parsed.get("data", {})
                target_key = None
                for k in data:
                    if sym.replace("/", "") in k:
                        target_key = k
                        break
                if not target_key:
                    return None
                klines = data[target_key].get("qfqday", data[target_key].get("day", []))
                if not klines or len(klines) < 5:
                    return None
                result = []
                for e in klines:
                    if len(e) >= 6:
                        try:
                            result.append({
                                "close": float(e[2]),
                                "volume": float(e[5]) if e[5] else 0,
                                "high": float(e[3]),
                                "low": float(e[4]),
                                "open": float(e[1]),
                            })
                        except (ValueError, IndexError):
                            continue
                kline_days = self.config.get("kline_days", 120)
                return result[-kline_days:]
            except (json.JSONDecodeError, KeyError, IndexError):
                time.sleep(1.5)
        return None

    # ── Sector rankings ──────────────────────────────────────────────

    def get_sector_rankings(self) -> list[dict]:
        """Fetch sector index performance from Tencent."""
        url = f"https://qt.gtimg.cn/q={','.join(SECTOR_INDICES)}"
        raw = fetch_url(url, headers=TENCENT_HEADERS)
        if not raw:
            return []
        try:
            text = raw.decode("gbk", errors="replace")
        except Exception:
            return []
        sectors = []
        for line in text.strip().split("\n"):
            if '="' not in line:
                continue
            try:
                val = line.split('="')[1].rstrip(";").rstrip('"')
                parts = val.split("~")
                if len(parts) < 35:
                    continue
                sectors.append({
                    "code": parts[2],
                    "name": parts[1] if parts[1] else SECTOR_NAMES.get(parts[2], ""),
                    "changepercent": safe_float(parts[32]) if len(parts) > 32 else 0,
                })
            except Exception:
                continue
        sectors.sort(key=lambda x: x["changepercent"], reverse=True)
        return sectors

    # ── East Money: hot concept sectors ───────────────────────────────

    def fetch_hot_concept_sectors(self, top_k: int = 10) -> list[str]:
        """Fetch hot concept sectors by 5-day change from East Money.

        Returns list of hot sector names (e.g. ['军工', '新能源', '半导体']).
        Falls back to empty list on failure.
        """
        sectors = self.fetch_hot_sectors_with_strength(top_k=top_k)
        return [s["name"] for s in sectors]

    def fetch_hot_sectors_with_strength(self, top_k: int = 30) -> list[dict]:
        """Fetch hot concept sectors ranked by 涨跌比 (up/down ratio).

        Steps:
          1. Fetch concept sectors from East Money.
          2. Filter to hot sectors (top N by 5-day change %).
          3. Rank those hot sectors by internal 涨跌比 = 上涨家数/下跌家数.

        Returns list of dicts:
            {name, bk_code, strength, up, down, change_5d}
        sorted by strength descending. Empty list on failure.
        """
        url = (
            "https://push2.eastmoney.com/api/qt/clist/get?"
            f"fs=m:90+t:3&fields=f12,f14,f104,f105,f106"
            f"&pn=1&np=1&pz={top_k + 10}"
        )
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://data.eastmoney.com",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
            parsed = json.loads(raw)
            items = parsed.get("data", {}).get("diff", [])
            if not items:
                return []

            # Build list with calculated 涨跌比
            active = []
            for item in items:
                up = int(item.get("f105", 0) or 0)
                down = int(item.get("f106", 0) or 0)
                if up + down == 0:
                    continue
                strength = up / max(down, 1)
                active.append({
                    "name": str(item.get("f14", "")).strip(),
                    "bk_code": str(item.get("f12", "")),
                    "strength": round(strength, 2),
                    "up": up,
                    "down": down,
                    "change_5d": float(item.get("f104", 0) or 0),
                })

            if not active:
                return []

            # Sort by 5-day change to find hot sectors
            active.sort(key=lambda x: x["change_5d"], reverse=True)
            hot = active[:top_k]

            # Re-sort hot sectors by 涨跌比 (strength) descending
            hot.sort(key=lambda x: x["strength"], reverse=True)
            return hot
        except Exception:
            return []
