"""
Data fetcher — reusable data-fetching utilities.

Provides:
    - get_all_a_stocks()     — from Sina
    - get_market_data()      — from Tencent (market cap, circulating shares)
    - get_kline()            — from Tencent (daily kline data)
    - get_sector_rankings()  — sector index performance
"""

import json
import re
import time
import urllib.request
from typing import Any

SINA_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}
TENCENT_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com"}

SINA_ALL_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeDataSimple?page=1&num=5000"
    "&sort=changepercent&asc=0&node=hs_a&symbol=&_s_r_a=page"
)

TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,500,qfq"


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def safe_float(v, default=0.0):
    try:
        return float(v) if v and str(v).strip() not in ("-", "") else default
    except (ValueError, TypeError):
        return default


def fetch_url(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or SINA_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        log(f"  [WARN] fetch failed: {url[:60]}... {e}")
        return None


def strip_prefix(sym):
    return re.sub(r"^(sh|sz|bj)", "", sym)


def code_to_prefix(code):
    if code.startswith(("6", "9")):
        return "sh"
    elif code.startswith(("0", "3")):
        return "sz"
    elif code.startswith(("8", "4")):
        return "bj"
    return ""


def match_track(name):
    TRACK_KW = [
        ("半导体/芯片", ["半导体","芯片","微电","集成电路","晶圆","封装","光刻","存储","GPU","AI芯片"]),
        ("AI/人工智能", ["AI","人工智能","大模型","智能体","视觉","语音","讯飞","算法"]),
        ("人形机器人", ["机器人","绿的谐波","减速器","丝杠","执行器","灵巧手","伺服"]),
        ("低空经济", ["低空","无人机","eVTOL","飞行汽车","空管"]),
        ("智能驾驶", ["自动驾驶","无人驾驶","激光雷达","ADAS"]),
        ("创新药/医药", ["创新药","生物","医药","恒瑞","百济","药明","基因","医疗","制药"]),
        ("新能源/储能", ["新能源","光伏","储能","电池","宁德","锂电","逆变器","固态电池","风电"]),
        ("军工/国防", ["军工","航天","航空","北斗","卫星","中航","雷达","国防"]),
    ]
    for tn, kws in TRACK_KW:
        for kw in kws:
            if kw in name:
                return tn
    return "其他"


class DataFetcher:
    def __init__(self, config=None):
        self.config = config or {}

    def get_all_a_stocks(self):
        """Fetch all A-share stocks from Sina."""
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
                stocks.append({
                    "code": strip_prefix(raw_code),
                    "symbol": raw_code,
                    "name": s.get("name", ""),
                    "price": safe_float(s.get("trade", 0)),
                    "changepercent": safe_float(s.get("changepercent", 0)),
                    "amount": safe_float(s.get("amount", 0)),
                })
            except Exception:
                continue
        return stocks

    def get_market_data(self, stocks, batch_size=80):
        """Fetch market cap and circulating shares from Tencent."""
        delay = self.config.get("request_delay", 0.3)
        result = {}
        for i in range(0, len(stocks), batch_size):
            batch = stocks[i:i+batch_size]
            syms = []
            for s in batch:
                c = s.get("code", "")
                prefix = code_to_prefix(c)
                if prefix:
                    syms.append(f"{prefix}{c}")
            if not syms:
                continue
            url = f"https://qt.gtimg.cn/q={','.join(syms)}"
            raw = fetch_url(url, headers=TENCENT_HEADERS, timeout=self.config.get("timeout_tencent", 15))
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
                                "code": code, "name": parts[1],
                                "price": safe_float(parts[3]),
                                "mv": safe_float(parts[44]),
                                "circ_shares": safe_float(parts[57]) / 10000,
                                "changepercent": safe_float(parts[32]) if len(parts) > 32 else 0,
                                "amount": safe_float(parts[37]) if len(parts) > 37 else 0,
                            }
                        except Exception:
                            continue
                except Exception:
                    pass
            time.sleep(delay)
            if (i // batch_size + 1) % 10 == 0:
                log(f"  Market data: {i+batch_size}/{len(stocks)}")
        return result

    def get_kline(self, sym):
        """Fetch daily kline data for a single stock."""
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,500,qfq"
        for _ in range(2):
            raw = fetch_url(url, headers=TENCENT_HEADERS, timeout=self.config.get("timeout_kline", 10))
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

    def get_sector_rankings(self):
        """Fetch sector index performance from Tencent."""
        SECTOR_INDICES = ["sz399928","sz399929","sz399930","sz399931","sz399932","sz399933","sz399934","sz399935","sz399936","sz399937","sz399971","sz399973","sz399974","sz399975","sz399976","sz399977","sz399440","sz399441","sz399967","sz399998","sz399989","sz399994","sz399987","sz399395","sz399396","sz399393","sz399389","sz399997","sz399995","sz399996","sz399993","sz399992","sz399986","sz399990","sz399275","sz399276","sz399978","sz399579","sz399242","sz399248","sz399234","sz399232","sz399972","sz399951","sz399952","sz399953","sz399954","sz399956","sz399955","sz399957","sz399958","sz399985"]
        SECTOR_NAMES = {"sz399928":"中证能源","sz399929":"中证材料","sz399930":"中证工业","sz399931":"中证可选","sz399932":"中证消费","sz399933":"中证医药","sz399934":"中证金融","sz399935":"中证信息","sz399936":"中证电信","sz399937":"中证公用"}
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
            if '="' not in line: continue
            try:
                val = line.split('="')[1].rstrip(";").rstrip('"')
                parts = val.split("~")
                if len(parts) < 35: continue
                sectors.append({"code": parts[2], "name": parts[1] or SECTOR_NAMES.get(parts[2], ""), "changepercent": safe_float(parts[32])})
            except Exception:
                continue
        sectors.sort(key=lambda x: x["changepercent"], reverse=True)
        return sectors
