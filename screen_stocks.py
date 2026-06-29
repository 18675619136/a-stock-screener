#!/usr/bin/env python3
"""
A股全市场筛选脚本
条件：
1. 主板/创业板/科创板/北交所（全市场）
2. 流通股本 < 10亿股，总市值 < 1000亿
3. MA5 > MA20 且 收盘价 ≤ MA20 × 1.10
4. 排除所有ST
"""
import json, re, urllib.request, time, sys, os

DATA_DIR = '/home/super-user/screening'
os.makedirs(DATA_DIR, exist_ok=True)

HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.sina.com.cn'}

def log(msg):
    print(msg, file=sys.stderr, flush=True)
    sys.stderr.flush()

def safe_float(v, default=0.0):
    try:
        return float(v) if v and str(v).strip() != '-' else default
    except:
        return default

# ========== 第一步：从新浪获取全A股列表 ==========
log('=== 第一步：获取全A股列表 ===')
url = 'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeDataSimple?page=1&num=5000&sort=changepercent&asc=0&node=hs_a&symbol=&_s_r_a=page'
req = urllib.request.Request(url, headers=HEADERS)
with urllib.request.urlopen(req, timeout=30) as resp:
    raw = resp.read().decode('gbk', errors='replace')
all_stocks = json.loads(raw)
log(f'获取全A股: {len(all_stocks)} 只')

# 筛选ST
stocks_clean = []
st_count = 0
for s in all_stocks:
    name = s.get('name', '')
    code = s.get('symbol', '')
    if name.startswith(('ST', '*ST', 'S', 'SST')):
        st_count += 1
        continue
    stocks_clean.append(s)
log(f'排除ST: {st_count} 只, 剩余: {len(stocks_clean)} 只')

# 保存股票列表供后续查询
stock_map = {}  # code -> {name, ...}
for s in stocks_clean:
    code = s['symbol']  # e.g. sh600000
    stock_map[code] = s

# ========== 第二步：从腾讯批量获取市值/股本 ==========
log('\n=== 第二步：获取市值和股本 ===')

def batch_get_tencent(prefixed_codes):
    """批量查询腾讯行情，每次最多80只（使用带前缀代码sh/sz/bj）"""
    results = {}
    batch_size = 80
    for i in range(0, len(prefixed_codes), batch_size):
        batch = prefixed_codes[i:i+batch_size]
        url = f"https://qt.gtimg.cn/q={','.join(batch)}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://gu.qq.com'})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode('gbk', errors='replace')
            for line in raw.strip().split('\n'):
                if '="' not in line: continue
                val = line.split('="')[1].rstrip('"').rstrip(';')
                parts = val.split('~')
                if len(parts) < 58: continue
                prefixed = line.split('=')[0].replace('v_', '').strip()  # v_sz000001 -> sz000001
                code = parts[2]
                results[prefixed] = {  # 使用带前缀的代码作为key
                    'name': parts[1],
                    'price': safe_float(parts[3]),
                    'total_mv': safe_float(parts[44]),
                    'circ_shares': safe_float(parts[57]) / 10000,
                }
        except Exception as e:
            log(f'  批次 {i//batch_size + 1} 失败: {e}')
        time.sleep(0.5)
        if (i // batch_size + 1) % 5 == 0:
            log(f'  已处理 {min(i+batch_size, len(prefixed_codes))}/{len(prefixed_codes)}')
    return results

# 将新浪代码转成腾讯格式（去掉前缀）
# 腾讯API需要带前缀的代码（sina格式sh/sz/bj可直接用）
tencent_codes = {}
for s in stocks_clean:
    sym = s['symbol']
    if sym.startswith('sh') or sym.startswith('sz') or sym.startswith('bj'):
        tencent_codes[sym] = sym  # 直接使用带前缀的代码

prefixed_codes = list(tencent_codes.keys())
log(f'需要查询腾讯行情: {len(prefixed_codes)} 只')

tencent_data = batch_get_tencent(prefixed_codes)
log(f'腾讯数据返回: {len(tencent_data)} 只')

# ========== 第三步：应用条件1和2筛选 ==========
log('\n=== 第三步：应用市值/股本筛选 ===')
# 条件: 流通股本 < 10亿股, 总市值 < 1000亿

candidates = []
for prefixed_code, sina_sym in tencent_codes.items():
    td = tencent_data.get(prefixed_code)
    if not td:
        continue
    circ = td['circ_shares']      # 流通股本(亿股)
    mv = td['total_mv']           # 总市值(亿元)
    price = td['price']
    name = td['name']
    
    if circ <= 0 or mv <= 0:
        continue  # 数据异常跳过
    
    if circ < 10 and mv < 1000:
        # 去掉前缀获取纯代码，供K线API使用
        plain_code = prefixed_code
        if prefixed_code.startswith(('sh', 'sz', 'bj')):
            plain_code = prefixed_code[2:]
        candidates.append({
            'code': plain_code,
            'sina_sym': sina_sym,
            'name': name,
            'price': price,
            'circ_shares': circ,
            'total_mv': mv
        })

log(f'满足市值/股本条件: {len(candidates)} 只')

# 按市值从小到大排序显示前20只
candidates.sort(key=lambda x: x['total_mv'])
log('\n按市值排序前20只(含各项参数):')
for c in candidates[:20]:
    log(f'  {c["name"]:10s} ({c["code"]:6s}) 市值:{c["total_mv"]:>8.2f}亿 流通股本:{c["circ_shares"]:>5.2f}亿 价格:{c["price"]:>8.2f}')
log(f'  待K线检查... 共{len(candidates)}只')

# ========== 第四步：获取K线，计算MA5/MA20 ==========
log('\n=== 第四步：获取K线数据检查MA条件 ===')
log('条件: MA5 > MA20 且 收盘价 ≤ MA20 × 1.10')

def fetch_kline(code, days=30):
    """获取腾讯K线数据"""
    prefix = ''
    if code.startswith('6') or code.startswith('9'):
        prefix = 'sh'
    elif code.startswith(('0', '3')):
        prefix = 'sz'
    elif code.startswith(('8', '4')):
        prefix = 'bj'
    else:
        return None
    
    url = f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code},day,,,{days},qfq'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://gu.qq.com'})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
        if not raw or len(raw) < 10:
            return None
        parsed = json.loads(raw)
        data = parsed.get('data', {})
        stock_data = None
        for key in data:
            if prefix + code in key:
                stock_data = data[key]
                break
        if not stock_data:
            return None
        klines = stock_data.get('qfqday', stock_data.get('day', []))
        if not klines or len(klines) < 25:
            return None
        closes = []
        for e in klines:
            if len(e) >= 6:
                try:
                    closes.append(float(e[2]))
                except:
                    continue
        if len(closes) < 25:
            return None
        return closes
    except Exception as e:
        return None

# 分批获取K线
results = []
batch_size_k = 10  # K线请求较慢，每批10只
total = len(candidates)

for i, c in enumerate(candidates):
    code = c['code']
    if (i + 1) % 20 == 0:
        log(f'  K线检查进度: {i+1}/{total}, 通过: {len(results)}只')
    
    closes = fetch_kline(code, days=30)
    if not closes:
        continue
    
    # 计算MA5和MA20
    ma5 = sum(closes[-5:]) / 5
    ma20 = sum(closes[-20:]) / 20
    close = closes[-1]
    
    # 条件: MA5 > MA20 且 收盘价 ≤ MA20 × 1.10
    if ma5 > ma20 and close <= ma20 * 1.10:
        diff_pct = (ma5 - ma20) / ma20 * 100
        above_pct = (close - ma20) / ma20 * 100
        results.append({
            **c,
            'ma5': round(ma5, 2),
            'ma20': round(ma20, 2),
            'close': round(close, 2),
            'ma5_ma20_diff': round(diff_pct, 2),
            'close_above_ma20': round(above_pct, 2),
        })
    
    # 每批间隔以避免限流
    if (i + 1) % batch_size_k == 0:
        time.sleep(0.8)

# ========== 最终输出 ==========
results.sort(key=lambda x: x['ma5_ma20_diff'], reverse=True)

print(f'\n\n{"="*80}')
print(f'📊 筛选结果')
print(f'{"="*80}', flush=True)
print(f'筛选条件:')
print(f'  ① 全市场（主板/创业板/科创板/北交所）')
print(f'  ② 流通股本 < 10亿股 且 总市值 < 1000亿')
print(f'  ③ MA5 > MA20 且 收盘价 ≤ MA20×1.10')
print(f'  ④ 排除ST股票')
print(f'{"="*80}')
print(f'全A股: {len(all_stocks)} → 排除ST: {len(stocks_clean)} → 市值/股本筛选: {len(candidates)} → 最终符合: {len(results)}')
print(f'{"="*80}\n')

if not results:
    print('今日无符合全部条件的个股')
else:
    print(f'{"名称":>10s} {"代码":>8s} {"收盘价":>8s} {"MA5":>8s} {"MA20":>8s} {"MA5-MA20":>8s} {"超MA20":>8s} {"流通股本":>8s} {"总市值":>8s}')
    print(f'{"-"*80}')
    for r in results:
        print(f'{r["name"]:>10s} {r["code"]:>8s} {r["close"]:>8.2f} {r["ma5"]:>8.2f} {r["ma20"]:>8.2f} {r["ma5_ma20_diff"]:>+7.2f}% {r["close_above_ma20"]:>+7.2f}% {r["circ_shares"]:>7.2f}亿 {r["total_mv"]:>7.2f}亿')

# 保存结果
output = {
    'total_stocks': len(all_stocks),
    'after_st_filter': len(stocks_clean),
    'after_mv_filter': len(candidates),
    'final_results': len(results),
    'stocks': results
}
with open(os.path.join(DATA_DIR, 'screen_results.json'), 'w') as f:
    json.dump(output, f, ensure_ascii=False, indent=2)
log(f'\n结果已保存: {DATA_DIR}/screen_results.json')
