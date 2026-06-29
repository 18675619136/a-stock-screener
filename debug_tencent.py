#!/usr/bin/env python3
"""调试版 - 只跑前200只腾讯查询"""
import json, re, urllib.request, sys, os

DATA_DIR = '/home/super-user/screening'
HEADERS = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.sina.com.cn'}

def safe_float(v, default=0.0):
    try:
        return float(v) if v and str(v).strip() != '-' else default
    except:
        return default

# 获取全A股
url = 'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeDataSimple?page=1&num=5000&sort=changepercent&asc=0&node=hs_a&symbol=&_s_r_a=page'
req = urllib.request.Request(url, headers=HEADERS)
with urllib.request.urlopen(req, timeout=30) as resp:
    raw = resp.read().decode('gbk', errors='replace')
all_stocks = json.loads(raw)
print(f'全A股: {len(all_stocks)}', flush=True)

# 只取前200只测试
test_stocks = [s for s in all_stocks if not s.get('name','').startswith(('ST','*ST','S'))][:200]
test_codes = [s['symbol'] for s in test_stocks if s['symbol'].startswith(('sh','sz','bj'))]
print(f'测试代码数: {len(test_codes)}', flush=True)
print(f'代码样例: {test_codes[:5]}', flush=True)

# 分批查询腾讯
results = {}
batch_size = 80
for i in range(0, len(test_codes), batch_size):
    batch = test_codes[i:i+batch_size]
    url = f"https://qt.gtimg.cn/q={','.join(batch)}"
    print(f'批次{i//batch_size+1}: URL长度={len(url)}', flush=True)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://gu.qq.com'})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode('gbk', errors='replace')
        print(f'  返回数据: {len(raw)} 字节', flush=True)
        lines = [l for l in raw.strip().split('\n') if '="' in l]
        print(f'  有效行数: {len(lines)}', flush=True)
        
        count = 0
        for line in lines:
            val = line.split('="')[1].rstrip('"').rstrip(';')
            parts = val.split('~')
            if len(parts) < 58:
                continue
            prefixed = line.split('=')[0].replace('v_', '').strip()
            results[prefixed] = {
                'name': parts[1],
                'total_mv': safe_float(parts[44]),
                'circ_shares': safe_float(parts[57]) / 10000,
            }
            count += 1
        print(f'  解析成功: {count}', flush=True)
    except Exception as e:
        print(f'  失败: {e}', flush=True)
    import time
    time.sleep(0.3)

print(f'\n总计解析: {len(results)}', flush=True)

# 打印满足条件的
candidates = []
for code, td in results.items():
    circ = td['circ_shares']
    mv = td['total_mv']
    if circ <= 0 or mv <= 0:
        continue
    if circ < 10 and mv < 1000:
        candidates.append((code, td['name'], mv, circ))

candidates.sort(key=lambda x: x[2])
print(f'\n满足条件(股本<10亿 且 市值<1000亿): {len(candidates)}', flush=True)
for c in candidates[:20]:
    print(f'  {c[1]:12s} ({c[0]:8s}) 市值={c[2]:>7.2f}亿 流通股本={c[3]:>5.2f}亿', flush=True)
