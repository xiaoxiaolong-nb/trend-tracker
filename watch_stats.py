"""
watch_stats.py
极值缩量战法：扫描全市场 .day，统计「前一日量比<阈值 → 后续 N 天上涨概率」

输出 watch_stats.json: {
  thresholds: {0.6: {samples: int, T+1: {win_rate, mean, median, count}, T+2: ..., T+3: ...}, 0.4: {...}},
  by_industry: {industry: {0.6: stats}},
  scanned: int, generated_at: ISO, days_window: 90
}
"""
import os, json, struct
from datetime import datetime
import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
VIP = r'C:/new_tdx/vipdoc'
OUT = os.path.join(BASE, 'watch_stats.json')
THRESHOLDS = [0.6, 0.4]
HORIZONS = [1, 2, 3, 5, 10]
DAYS = 90  # 统计最近 90 个交易日

# 简易行业表（按代码前缀分大类）
def industry(digits):
    d = int(digits[:3])
    # 简化行业映射
    if d in (301, 302, 303):  # 创业板（深）
        return '创业板'
    if d in (688,):  # 科创板
        return '科创板'
    if d in (430, 830, 831, 832, 833, 834, 835, 836, 837, 838, 839):  # 北证
        return '北证'
    if d in (600, 601, 603, 605):
        return '沪市主板'
    if d in (0, 2, 3):  # 深主板/中小
        return '深市主板'
    return '其他'


def code_to_path(mkt, digits):
    return os.path.join(VIP, mkt, 'lday', f'{mkt}{digits}.day')


def scan_files():
    files = []
    for mkt in ('sh', 'sz', 'bj'):
        d = os.path.join(VIP, mkt, 'lday')
        if not os.path.exists(d):
            continue
        for f in os.listdir(d):
            if f.endswith('.day'):
                files.append((mkt, f[:-4], os.path.join(d, f)))
    return files


def load_day(path):
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as fh:
        data = fh.read()
    n = len(data) // 32
    rows = []
    for i in range(n):
        r = data[i * 32:(i + 1) * 32]
        d, o, h, l, c, amt, vol, res = struct.unpack('<iiiiifii', r)
        rows.append((d, c / 100, vol))
    return rows


def main():
    print(f'扫描 {DAYS} 日数据...')
    files = scan_files()
    print(f'  共 {len(files)} 文件')
    by_thr = {t: [] for t in THRESHOLDS}
    by_ind = {t: {} for t in THRESHOLDS}
    n = 0
    for mkt, code, path in files:
        rows = load_day(path)
        if not rows or len(rows) < 30:
            continue
        digits = ''.join(c for c in code if c.isdigit())
        ind = industry(digits)
        # 取最近 DAYS+10 根（要算后续 N 天）
        tail = rows[-(DAYS + max(HORIZONS) + 10):]
        if len(tail) < 30:
            continue
        n += 1
        for i in range(5, len(tail) - max(HORIZONS)):
            d, close, vol = tail[i]
            # 5 日均量
            vol5 = sum(tail[i - 5 + k][2] for k in range(5)) / 5.0
            if vol5 <= 0:
                continue
            vr = vol / vol5
            for t in THRESHOLDS:
                if vr < t:
                    entry = {'code': mkt + digits, 'date': d, 'vr': round(vr, 3), 'close': close}
                    for h in HORIZONS:
                        if i + h < len(tail):
                            future_close = tail[i + h][2] and tail[i + h][1]
                            # 用 vol 字段占位
                            future_close = tail[i + h][1]
                            entry[f'T{h}'] = round((future_close / close - 1) * 100, 2)
                    by_thr[t].append(entry)
                    by_ind[t].setdefault(ind, []).append(entry)
        if n % 1000 == 0:
            print(f'  扫 {n} ...')

    print(f'扫描完成 {n} 只票')
    out = {
        'scanned': n,
        'days_window': DAYS,
        'thresholds': {},
        'by_industry': {},
        'generated_at': datetime.now().isoformat(timespec='seconds'),
    }
    for t in THRESHOLDS:
        items = by_thr[t]
        stats = {'samples': len(items)}
        for h in HORIZONS:
            key = f'T{h}'
            vals = [it[key] for it in items if it.get(key) is not None]
            if vals:
                arr = np.array(vals)
                stats[key] = {
                    'count': int(len(arr)),
                    'win_rate': round(float((arr > 0).mean()) * 100, 1),
                    'mean': round(float(arr.mean()), 2),
                    'median': round(float(np.median(arr)), 2),
                    'max': round(float(arr.max()), 2),
                    'min': round(float(arr.min()), 2),
                }
            else:
                stats[key] = {'count': 0}
        out['thresholds'][str(t)] = stats

        # 行业
        ind_stats = {}
        for ind, lst in by_ind[t].items():
            if len(lst) < 5:
                continue
            ind_stats[ind] = {'samples': len(lst)}
            for h in HORIZONS:
                key = f'T{h}'
                vals = [it[key] for it in lst if it.get(key) is not None]
                if vals:
                    arr = np.array(vals)
                    ind_stats[ind][key] = {
                        'count': int(len(arr)),
                        'win_rate': round(float((arr > 0).mean()) * 100, 1),
                        'mean': round(float(arr.mean()), 2),
                        'median': round(float(np.median(arr)), 2),
                    }
        out['by_industry'][str(t)] = ind_stats

    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f'已写 {OUT}')
    print()
    for t in THRESHOLDS:
        s = out['thresholds'][str(t)]
        print(f'=== 量比<{t}（{s["samples"]} 样本）===')
        for h in HORIZONS:
            v = s.get(f'T{h}', {})
            print(f'  T+{h}: 胜率={v.get("win_rate")}% 均值={v.get("mean")}% 中位={v.get("median")}% (n={v.get("count")})')


if __name__ == '__main__':
    main()
