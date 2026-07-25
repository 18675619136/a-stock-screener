#!/usr/bin/env python3
"""
Fetch 2026-07-10 East Money concept sector rankings.
Top 15 gainers and top 5 losers.
"""
import json
import urllib.request
import sys

def log(msg):
    print(msg, file=sys.stderr, flush=True)

def fetch_concept_rankings(top_n=20):
    """
    Fetch concept sector daily rankings from East Money.
    Sorted by daily change % (f3) descending.
    """
    url = (
        "https://push2.eastmoney.com/api/qt/clist/get?"
        "fs=m:90+t:3&fid=f3&po=1&pz={}&pn=1&np=1&fltt=2&invt=2"
        "&fields=f12,f14,f2,f3,f4,f104,f105,f106"
    ).format(top_n)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://data.eastmoney.com",
    }

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
        parsed = json.loads(raw)
        items = parsed.get("data", {}).get("diff", [])
        if not items:
            log("No data returned from East Money API")
            return []

        sectors = []
        for item in items:
            sectors.append({
                "code": str(item.get("f12", "")),
                "name": str(item.get("f14", "")).strip(),
                "price": float(item.get("f2", 0) or 0),
                "change_pct": float(item.get("f3", 0) or 0),
                "change_amt": float(item.get("f4", 0) or 0),
                "change_5d": float(item.get("f104", 0) or 0),
                "up_stocks": int(item.get("f105", 0) or 0),
                "down_stocks": int(item.get("f106", 0) or 0),
            })
        return sectors
    except Exception as e:
        log(f"Error fetching East Money data: {e}")
        return []

def main():
    log("Fetching 2026-07-10 A-share concept sector rankings from East Money ...")

    sectors = fetch_concept_rankings(30)

    if not sectors:
        log("FAILED to fetch concept sector data. No network access or API unavailable.")
        sys.exit(1)

    # Top 15 gainers
    gainers = sectors[:15]
    # Bottom 5 losers (last 5 sorted ascending)
    losers = sorted(sectors, key=lambda x: x["change_pct"])[:5]

    result = {
        "date": "2026-07-10",
        "total_sectors_returned": len(sectors),
        "top_15_gainers": gainers,
        "bottom_5_losers": losers,
    }

    output_path = "/home/super-user/screening/concept_sectors_20260710.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    log(f"Results saved to {output_path}")

    # Print summary
    print("\n" + "=" * 60)
    print("📈 2026年7月10日 A股概念板块涨幅排名 TOP 15")
    print("=" * 60)
    print(f"{'排名':<4} {'板块名称':<16} {'涨幅%':<8} {'上涨家数':<8} {'下跌家数':<8}")
    print("-" * 60)
    for i, s in enumerate(gainers, 1):
        print(f"{i:<4} {s['name']:<16} {s['change_pct']:>+6.2f}% {s['up_stocks']:<8} {s['down_stocks']:<8}")

    print("\n" + "=" * 60)
    print("📉 2026年7月10日 A股概念板块跌幅排名 TOP 5")
    print("=" * 60)
    print(f"{'排名':<4} {'板块名称':<16} {'涨幅%':<8} {'上涨家数':<8} {'下跌家数':<8}")
    print("-" * 60)
    for i, s in enumerate(losers, 1):
        print(f"{i:<4} {s['name']:<16} {s['change_pct']:>+6.2f}% {s['up_stocks']:<8} {s['down_stocks']:<8}")

if __name__ == "__main__":
    main()
