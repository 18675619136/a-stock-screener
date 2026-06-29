#!/usr/bin/env python3
"""A股筛选 - 简化版
用法: python3 screen_v2.py [--limit N]   # --limit 测试模式，只检查前N只
"""
import json, urllib.request, time, sys, os, argparse

DATA_DIR = '/home/super-user/screening'
os.makedirs(DATA_DIR, exist_ok=True)

parser = argparse.ArgumentParser()
parser.add_argument('--limit', type=int, default=0, help='测试模式：只检查前N只候选（跳过耗时K线）')
args = parser.parse_args()
LIMIT = args.limit

def sf(v, d=0.0):
    try: return float(v) if v and str(v).strip() not in ('-', '') else d
    except: return d

def log(m): print(m, file=sys.stderr, flush=True)

SINA_HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.sina.com.cn'}
TENCENT_HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://gu.qq.com'}

# 步骤1: 获取全A股
log('=== 步骤1: 获取全A股 ===')
url = 'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeDataSimple?page=1&num=5000&sort=changepercent&asc=0&node=hs_a&symbol=&_s_r_a=page'
req = urllib.request.Request(url, headers=SINA_HEADERS)
with urllib.request.urlopen(req, timeout=30) as r:
    raw = r.read().decode('gbk', errors='replace')
all_s = json.loads(raw)
log(f'全A股: {len(all_s)}')

# 排除ST和退市股
clean = [s for s in all_s if not s.get('name','').startswith(('ST','*ST','S')) and '退' not in s.get('name','')]
log(f'排除ST/退市后: {len(clean)}')

# 提取带前缀代码
code_name_map = {}
for s in clean:
    sym = s['symbol']
    if sym.startswith(('sh','sz','bj')):
        name = s.get('name', '')
        code_name_map[sym] = name

all_codes = list(code_name_map.keys())
log(f'待查询腾讯: {len(all_codes)}')

# 步骤2: 批量查询腾讯
log('=== 步骤2: 获取市值/股本 ===')
tencent_data = {}
batch_size = 80
for i in range(0, len(all_codes), batch_size):
    batch = all_codes[i:i+batch_size]
    url = f"https://qt.gtimg.cn/q={','.join(batch)}"
    try:
        req = urllib.request.Request(url, headers=TENCENT_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode('gbk', errors='replace')
        for line in raw.strip().split('\n'):
            if '="' not in line: continue
            try:
                val = line.split('="')[1].rstrip('"').rstrip(';')
                parts = val.split('~')
                if len(parts) < 58: continue
                prefixed = line.split('=')[0].replace('v_', '').strip()
                tencent_data[prefixed] = {
                    'name': parts[1],
                    'price': sf(parts[3]),
                    'mv': sf(parts[44]),       # 总市值(亿)
                    'circ': sf(parts[57]) / 10000,  # 流通股本(亿股)
                }
            except: pass
    except Exception as e:
        log(f'  批次{i//batch_size+1}失败: {e}')
    time.sleep(0.3)
    if (i // batch_size + 1) % 10 == 0:
        log(f'  {i+batch_size}/{len(all_codes)}')

log(f'腾讯返回: {len(tencent_data)}')

# 步骤3: 筛选市值/股本
log('=== 步骤3: 筛选条件 ===')
candidates = []
for code, td in tencent_data.items():
    if td['circ'] <= 0 or td['mv'] <= 0: continue
    if td['circ'] < 10 and td['mv'] < 1000:
        plain = code[2:] if code.startswith(('sh','sz','bj')) else code
        candidates.append({
            'prefixed': code,
            'code': plain,
            'name': td['name'],
            'price': td['price'],
            'circ': td['circ'],
            'mv': td['mv'],
        })

log(f'满足市值/股本条件: {len(candidates)}')
candidates.sort(key=lambda x: x['mv'])
for c in candidates[:10]:
    log(f'  {c["name"]:12s} {c["code"]:8s} 市值={c["mv"]:.2f}亿 流通={c["circ"]:.2f}亿')

# 步骤4: K线检查
log('=== 步骤4: K线检查 MA5>MA20 且 收盘≤MA20×1.10 ===')
# 如果 --limit 设了，只查前N只
check_candidates = candidates[:LIMIT] if LIMIT > 0 else candidates
log(f'  K线检查数量: {len(check_candidates)} 只')
if LIMIT > 0:
    log(f'  预计耗时: 约{len(check_candidates)*0.3:.0f}秒')
else:
    log(f'  预计耗时: 约{len(check_candidates)*0.3//60:.0f}分{len(check_candidates)*0.3%60:.0f}秒')
results = []
for i, c in enumerate(check_candidates):
    code = c['code']
    # 决定前缀
    if code.startswith(('6','9')): pref = 'sh'
    elif code.startswith(('0','3')): pref = 'sz'
    elif code.startswith(('8','4')): pref = 'bj'
    else: continue
    
    url = f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={pref}{code},day,,,30,qfq'
    try:
        req = urllib.request.Request(url, headers=TENCENT_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode('utf-8', errors='replace')
        if len(raw) < 20: continue
        parsed = json.loads(raw)
        data = parsed.get('data', {})
        sk = None
        for k in data:
            if pref+code in k: sk = data[k]; break
        if not sk: continue
        klines = sk.get('qfqday', sk.get('day', []))
        if len(klines) < 25: continue
        closes = []
        for e in klines:
            if len(e) >= 6:
                try: closes.append(float(e[2]))
                except: pass
        if len(closes) < 25: continue
        
        ma5 = sum(closes[-5:]) / 5
        ma20 = sum(closes[-20:]) / 20
        close = closes[-1]
        
        if ma5 > ma20 and close <= ma20 * 1.10:
            results.append({
                'code': code,
                'name': c['name'],
                'close': round(close, 2),
                'ma5': round(ma5, 2),
                'ma20': round(ma20, 2),
                'ma5_diff': round((ma5-ma20)/ma20*100, 2),
                'above_ma20': round((close-ma20)/ma20*100, 2),
                'mv': c['mv'],
                'circ': c['circ'],
                'price': c['price'],
            })
    except: pass
    
    if (i+1) % 30 == 0:
        log(f'  K线检查: {i+1}/{len(candidates)}, 通过: {len(results)}')
    time.sleep(0.3)

# 输出
results.sort(key=lambda x: x['ma5_diff'], reverse=True)
print(f'\n{"="*80}')
print(f'A股筛选结果 | {len(results)} 只符合条件')
print(f'{"="*80}')
print(f'{"名称":>10s} {"代码":>8s} {"收盘":>7s} {"MA5":>7s} {"MA20":>7s} {"MA5-MA20":>8s} {"超MA20":>8s} {"市值":>7s} {"股本":>6s}')
print(f'{"-"*80}')
for r in results:
    print(f'{r["name"]:>10s} {r["code"]:>8s} {r["close"]:>7.2f} {r["ma5"]:>7.2f} {r["ma20"]:>7.2f} {r["ma5_diff"]:>+7.2f}% {r["above_ma20"]:>+7.2f}% {r["mv"]:>6.1f}亿 {r["circ"]:>5.2f}亿')
print(f'{"="*80}')

# 保存
out = {
    'total': len(all_s), 'after_st': len(clean), 'after_mv': len(candidates),
    'kline_checked': len(check_candidates), 'final': len(results),
    'stocks': results
}
with open(os.path.join(DATA_DIR, 'screen_results.json'), 'w') as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
log(f'\n保存完成: {DATA_DIR}/screen_results.json')
