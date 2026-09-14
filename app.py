import os, json, struct, urllib.request, threading
from datetime import datetime, date
import numpy as np
import pandas as pd
from flask import Flask, request, jsonify, send_from_directory
import flask_socketio as fsio

# ===== 腾讯 K线接口（实时含今日） =====
def code_to_qt(code):
    c = code.strip().lower()
    digits = ''.join(ch for ch in c if ch.isdigit())
    if not digits:
        return None
    if c.startswith(('sh','sz','bj')):
        mkt = c[:2]
    else:
        mkt = 'sh' if digits.startswith('6') else ('bj' if digits.startswith(('8','4')) else 'sz')
    return mkt + digits

def fetch_txfqkline(qcode, count=320):
    """腾讯 web.ifzq.gtimg.cn fqkline 接口（含今日实时 K 线）"""
    try:
        url = f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={qcode},day,,,{count},qfq'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://gu.qq.com/'})
        text = urllib.request.urlopen(req, timeout=4).read().decode('utf-8')
        j = json.loads(text)
        for k, v in j.get('data', {}).items():
            if isinstance(v, dict) and v.get('qfqday'):
                bars = []
                for row in v['qfqday']:
                    if len(row) >= 6:
                        bars.append({
                            'date': row[0].replace('-', ''),
                            'open': round(float(row[1]), 2),
                            'close': round(float(row[2]), 2),
                            'high': round(float(row[3]), 2),
                            'low': round(float(row[4]), 2),
                            'vol': int(float(row[5])),
                        })
                return bars
    except Exception:
        return None
    return None

def fetch_txmin(qcode):
    """腾讯分钟 K 线（含今日实时分时）"""
    try:
        url = f'https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={qcode}'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://gu.qq.com/'})
        text = urllib.request.urlopen(req, timeout=4).read().decode('utf-8')
        j = json.loads(text)
        d = (j.get('data') or {}).get(qcode) or {}
        inner = d.get('data') or {}
        rows = inner.get('data') or []
        date_str = (inner.get('date') or '').replace('-', '')
        out = []
        for row in rows:
            parts = row.split(' ')
            if len(parts) >= 3:
                t = parts[0]  # HHMM
                price = float(parts[1])
                vol = int(parts[2]) if len(parts) > 2 else 0
                out.append({'time': t, 'price': price, 'vol': vol})
        return {'date': date_str, 'points': out}
    except Exception:
        return None



BASE = os.path.dirname(os.path.abspath(__file__))
VIP = r'__vipdoc_unavailable__'
SIGNALS_FILE = os.path.join(BASE, 'signals.json')
WATCH_FILE = os.path.join(BASE, 'watch_signals.json')
WATCH_STATS_FILE = os.path.join(BASE, 'watch_stats.json')
HORIZONS = [1, 3, 5, 10]

app = Flask(__name__)
app.config['SECRET_KEY'] = 'duck-head-tracker-2026'
sio = fsio.SocketIO(app, cors_allowed_origins='*', async_mode='threading')

_name_cache = {}
_mktcap_cache = {}
_push_lock = threading.Lock()
_last_quotes = {}  # code -> {price, change_pct, name}

# ============== 批量实时行情（备用，老版 fetch_quotes_batch） ==============
def fetch_quotes_batch(codes):
    if not codes:
        return {}
    out = {}
    # 按 code_to_qt 归一化
    qmap = {}
    for code in codes:
        c = code.strip().lower()
        if not c.startswith(('sh', 'sz', 'bj')):
            mkt, num = code_to_mkt_digits(c)
            c = mkt + num
        q = code_to_qt(c)
        if q:
            qmap[q] = c
    if not qmap:
        return out
    try:
        url = 'https://qt.gtimg.cn/q=' + ','.join(qmap.keys())
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://stockapp.finance.qq.com/',
        })
        text = urllib.request.urlopen(req, timeout=4).read().decode('gbk', 'ignore')
        for line in text.split('\n'):
            if '="' not in line:
                continue
            key, inner = line.split('="', 1)
            inner = inner.rstrip(';"\n\r ')
            parts = inner.split('~')
            q = key.strip().lstrip('v_')
            orig = qmap.get(q)
            if not orig or len(parts) < 50:
                continue
            try:
                price = float(parts[3])
                prev_close = float(parts[4])
                chg = round((price / prev_close - 1) * 100, 2) if prev_close else 0
                vol_ratio = float(parts[49]) if parts[49] else None
                mktcap = float(parts[44]) if parts[44] else None
                out[orig] = {
                    'price': price,
                    'change_pct': chg,
                    'name': parts[1],
                    'vol_ratio': vol_ratio,
                    'mktcap': mktcap,
                }
            except Exception:
                pass
    except Exception:
        pass
    return out




# ============== 基础工具 ==============

def code_to_mkt_digits(code):
    code = code.strip().lower()
    digits = ''.join(c for c in code if c.isdigit())
    if code.startswith(('sh', 'sz', 'bj')):
        return code[:2], digits
    return ('sh' if digits.startswith('6') else ('bj' if digits.startswith(('8', '4')) else 'sz')), digits


def code_to_path(code):
    mkt, num = code_to_mkt_digits(code)
    return os.path.join(VIP, mkt, 'lday', f'{mkt}{num}.day')


def load_ohlcv(code, with_vol=False):
    f = code_to_path(code)
    if not os.path.exists(f):
        return None
    with open(f, 'rb') as fh:
        data = fh.read()
    n = len(data) // 32
    rows = []
    for i in range(n):
        r = data[i * 32:(i + 1) * 32]
        d, o, h, l, c, amt, vol, res = struct.unpack('<iiiiifii', r)
        if with_vol:
            rows.append((str(d), o / 100, h / 100, l / 100, c / 100, vol))
        else:
            rows.append((str(d), o / 100, h / 100, l / 100, c / 100))
    cols = ['date', 'open', 'high', 'low', 'close', 'vol'] if with_vol else ['date', 'open', 'high', 'low', 'close']
    return pd.DataFrame(rows, columns=cols)


def get_name(code):
    code_k = code.strip().lower()
    if code_k in _name_cache:
        return _name_cache[code_k]
    mkt, digits = code_to_mkt_digits(code_k)
    if not digits:
        return ''
    q = mkt + digits
    name = ''
    try:
        req = urllib.request.Request(f'https://qt.gtimg.cn/q={q}',
                                     headers={'User-Agent': 'Mozilla/5.0',
                                              'Referer': 'https://stockapp.finance.qq.com/'})
        with urllib.request.urlopen(req, timeout=3) as r:
            data = r.read().decode('gbk')
        if '="' in data:
            inner = data.split('="', 1)[1].rstrip(';"\n\r ')
            parts = inner.split('~')
            if len(parts) >= 3:
                name = parts[1].strip()
    except Exception:
        name = ''
    _name_cache[code_k] = name
    return name


def get_mktcap_yi(code, close_price):
    code_k = code.strip().lower()
    if code_k in _mktcap_cache:
        return _mktcap_cache[code_k]
    mkt, digits = code_to_mkt_digits(code_k)
    if not digits:
        return None
    val = None
    try:
        secid = f'{mkt.upper()}.{digits}'
        url = f'https://push2.eastmoney.com/api/qing/stock/get?secid={secid}&fields=f47,f116,f117'
        r = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0',
                                                  'Referer': 'https://quote.eastmoney.com/'})
        with urllib.request.urlopen(r, timeout=3) as resp:
            j = json.loads(resp.read().decode('utf-8', 'ignore'))
        d = j.get('data') or {}
        lt = d.get('f47')
        if lt:
            val = round(float(lt) / 1e8, 2)
        else:
            ltshare = d.get('f117')
            if ltshare and close_price:
                val = round(float(ltshare) * float(close_price) / 1e8, 2)
    except Exception:
        val = None
    _mktcap_cache[code_k] = val
    return val


# ============== 信号计算 ==============

def compute(sig):
    code = sig['code']
    sig_date = str(sig['date']).replace('-', '').replace('/', '')
    df = load_ohlcv(code, with_vol=True)
    res = {'found': False, 'ret': {}}
    if df is None:
        return res
    df = df.reset_index(drop=True)
    idx = df.index[df['date'] == sig_date]
    if len(idx) == 0:
        return res
    i = int(idx[0])
    res['found'] = True
    res['last_date'] = df['date'].iloc[-1]
    res['sig_close'] = df['close'].iloc[i]
    for h in HORIZONS:
        j = i + h
        res['ret'][f'T{h}'] = round((df['close'].iloc[j] / df['close'].iloc[i] - 1) * 100, 2) if j < len(df) else None
    j = i + 1
    res['next_open'] = round((df['open'].iloc[j] / df['close'].iloc[i] - 1) * 100, 2) if j < len(df) else None
    if i >= 5 and df['vol'].iloc[i] > 0:
        avg5 = df['vol'].iloc[max(0, i - 5):i].mean()
        res['vol_ratio'] = round(df['vol'].iloc[i] / avg5, 2) if avg5 > 0 else None
    else:
        res['vol_ratio'] = None
    last = len(df) - 1
    if last >= 5 and df['vol'].iloc[last] > 0:
        avg5 = df['vol'].iloc[last - 5:last].mean()
        res['vol_ratio_latest'] = round(df['vol'].iloc[last] / avg5, 2) if avg5 > 0 else None
    else:
        res['vol_ratio_latest'] = None
    return res


def summarize(items):
    stats = {}
    completed = [it for it in items if it.get('found')]
    for h in HORIZONS:
        key = f'T{h}'
        vals = [it['ret'].get(key) for it in completed if it.get('ret', {}).get(key) is not None]
        if vals:
            arr = np.array(vals)
            stats[key] = {'count': int(len(arr)),
                          'win_rate': round(float((arr > 0).mean()) * 100, 1),
                          'mean': round(float(arr.mean()), 2),
                          'median': round(float(np.median(arr)), 2)}
        else:
            stats[key] = {'count': 0}
    no = [it.get('next_open') for it in completed if it.get('next_open') is not None]
    if no:
        arr = np.array(no)
        stats['next_open'] = {'count': int(len(arr)),
                              'win_rate': round(float((arr > 0).mean()) * 100, 1),
                              'mean': round(float(arr.mean()), 2)}
    stats['total'] = len(items)
    stats['found'] = len(completed)
    return stats


# ============== HTTP实时行情（备用）==============

@app.route('/api/quotes', methods=['POST'])
def quotes():
    codes = (request.json or {}).get('codes', [])
    result = {}
    code_to_q = {}
    q_list = []
    for c in codes:
        c_k = c.strip().lower()
        digits = ''.join(ch for ch in c_k if ch.isdigit())
        if not digits: continue
        mkt, _ = code_to_mkt_digits(c_k)
        q = mkt + digits
        q_list.append(q)
        code_to_q[q] = c_k
    if not q_list:
        return jsonify({})
    try:
        url = 'https://qt.gtimg.cn/q=' + ','.join(q_list)
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://stockapp.finance.qq.com/'})
        with urllib.request.urlopen(req, timeout=3) as r:
            text = r.read().decode('gbk')
        for line in text.strip().split('\n'):
            if '="' not in line: continue
            key, inner = line.split('="', 1)
            q = key.strip().lstrip('v_')
            inner = inner.rstrip(';\n\r "')
            parts = inner.split('~')
            if len(parts) < 5: continue
            try:
                price = float(parts[3])
                prev = float(parts[4])
                chg = round((price / prev - 1) * 100, 2) if prev > 0 else None
                result[code_to_q.get(q, q)] = {'price': price, 'change_pct': chg, 'name': parts[1]}
            except (ValueError, IndexError):
                continue
    except Exception:
        pass
    return jsonify(result)


# ============== 实时行情推送 ==============

def push_quotes_loop():
    """后台线程：每5秒拉腾讯行情，推送所有已订阅代码"""
    while True:
        try:
            with _push_lock:
                codes = list(_last_quotes.keys())
            if not codes:
                import time; time.sleep(5)
                continue
            code_to_q = {}
            q_list = []
            for c in codes:
                c_k = c.strip().lower()
                digits = ''.join(ch for ch in c_k if ch.isdigit())
                if not digits:
                    continue
                mkt, _ = code_to_mkt_digits(c_k)
                q = mkt + digits
                q_list.append(q)
                code_to_q[q] = c_k
            if not q_list:
                import time; time.sleep(5); continue
            url = 'https://qt.gtimg.cn/q=' + ','.join(q_list)
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0',
                'Referer': 'https://stockapp.finance.qq.com/'
            })
            with urllib.request.urlopen(req, timeout=3) as r:
                text = r.read().decode('gbk')
            result = {}
            for line in text.strip().split('\n'):
                if '="' not in line:
                    continue
                key, inner = line.split('="', 1)
                q = key.strip().lstrip('v_')
                inner = inner.rstrip(';\n\r "')
                parts = inner.split('~')
                if len(parts) < 5:
                    continue
                try:
                    price = float(parts[3])
                    prev_close = float(parts[4])
                    change_pct = round((price / prev_close - 1) * 100, 2) if prev_close > 0 else None
                    vol_ratio = None
                    mktcap_yi = None
                    try:
                        if len(parts) > 49 and parts[49]:
                            vol_ratio = float(parts[49])
                        if len(parts) > 44 and parts[44]:
                            mktcap_yi = float(parts[44])
                    except Exception:
                        pass
                    result[code_to_q.get(q, q)] = {
                        'price': price,
                        'change_pct': change_pct,
                        'name': parts[1],
                        'vol_ratio': vol_ratio,
                        'mktcap': mktcap_yi
                    }
                except (ValueError, IndexError):
                    continue
            with _push_lock:
                for code, qdata in result.items():
                    _last_quotes[code] = qdata
            sio.emit('quotes', result)
        except Exception:
            pass
        import time; time.sleep(5)


# ============== Flask 路由 ==============

@app.route('/')
def index():
    return send_from_directory(BASE, 'index.html')

@app.after_request
def no_cache(resp):
    resp.headers['Cache-Control'] = 'no-store, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


@app.route('/api/signals', methods=['GET'])
def list_signals():
    sigs = load_signals()
    out = []
    for s in sigs:
        c = compute(s)
        last_close = None
        try:
            df = load_ohlcv(s['code'])
            if df is not None and len(df):
                last_close = float(df['close'].iloc[-1])
        except Exception:
            pass
        item = dict(s)
        item.update({'found': c['found'], 'last_date': c.get('last_date'),
                     'ret': c.get('ret', {}), 'next_open': c.get('next_open'),
                     'sig_close': c.get('sig_close'),
                     'vol_ratio': c.get('vol_ratio'),
                     'vol_ratio_latest': c.get('vol_ratio_latest'),
                     'mktcap': get_mktcap_yi(s['code'], last_close),
                     'name': get_name(s['code'])})
        item.setdefault('status', '观察中')
        item.setdefault('pinned', False)
        # 合并实时行情
        with _push_lock:
            qdata = _last_quotes.get(s['code'].strip().lower(), {})
        if qdata:
            item['live_price'] = qdata.get('price')
            item['live_change_pct'] = qdata.get('change_pct')
        out.append(item)
    return jsonify({'signals': out, 'stats': summarize(out)})


@app.route('/api/signals', methods=['POST'])
def add_signal():
    data = request.json or {}
    code = (data.get('code') or '').strip()
    sdate = data.get('date')
    if not code or not sdate:
        return jsonify({'error': 'code and date required'}), 400
    sigs = load_signals()
    sig = {'id': datetime.now().strftime('%Y%m%d%H%M%S%f'),
           'code': code, 'date': str(sdate),
           'note': data.get('note', ''),
           'status': '观察中', 'pinned': False,
           'created': datetime.now().isoformat()}
    sigs.append(sig)
    save_signals(sigs)
    # 立即订阅这个代码
    with _push_lock:
        _last_quotes[code.strip().lower()] = {}
    return jsonify({'ok': True, 'signal': sig})


@app.route('/api/signals/<sid>', methods=['PATCH'])
def patch_signal(sid):
    sigs = load_signals()
    data = request.json or {}
    found = False
    for s in sigs:
        if s.get('id') == sid:
            for k in ('status', 'note', 'pinned'):
                if k in data:
                    s[k] = data[k]
            found = True
            break
    if not found:
        return jsonify({'error': 'not found'}), 404
    save_signals(sigs)
    return jsonify({'ok': True})


@app.route('/api/signals/<sid>', methods=['DELETE'])
def del_signal(sid):
    sigs = load_signals()
    sigs = [s for s in sigs if s.get('id') != sid]
    save_signals(sigs)
    return jsonify({'ok': True})


def save_signals(sigs):
    with open(SIGNALS_FILE, 'w', encoding='utf-8') as f:
        json.dump(sigs, f, ensure_ascii=False, indent=2)


def load_signals():
    if not os.path.exists(SIGNALS_FILE):
        return []
    with open(SIGNALS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


# ============== K线接口（日/周/月，全部用本地.day）==============

def resample_bars(bars_or_df, period):
    import pandas as pd
    if isinstance(bars_or_df, list):
        df = pd.DataFrame(bars_or_df)
    else:
        df = bars_or_df.copy()
    if df.empty:
        return []
    df['date'] = pd.to_datetime(df['date'].astype(str), format='%Y%m%d')
    if period == 'weekly':
        grouper = df['date'].dt.isocalendar().week.astype(str) + '_' + df['date'].dt.year.astype(str)
    else:
        grouper = df['date'].dt.strftime('%Y-%m')
    grouped = df.groupby(grouper, sort=True)
    out = []
    for g, gdf in grouped:
        first = gdf.iloc[0]
        date_val = first['date']
        if hasattr(date_val, 'strftime'):
            date_str = date_val.strftime('%Y%m%d')
        else:
            date_str = str(int(float(str(date_val))))
        out.append({
            'date': date_str,
            'open': round(float(first['open']), 2),
            'high': round(float(gdf['high'].max()), 2),
            'low': round(float(gdf['low'].min()), 2),
            'close': round(float(gdf.iloc[-1]['close']), 2),
            'vol': int(gdf['vol'].sum()),
        })
    return out


@app.route('/api/kline')
def kline():
    code = request.args.get('code', '').strip()
    period = request.args.get('period', 'daily')
    days = max(20, min(int(request.args.get('days', '120')), 500))
    if period not in ('daily', 'weekly', 'monthly'):
        period = 'daily'

    bars = None
    qcode = code_to_qt(code)
    if qcode:
        n = days * 8 if period == 'weekly' else (days * 25 if period == 'monthly' else days)
        bars = fetch_txfqkline(qcode, n)

    if not bars:
        df = load_ohlcv(code, with_vol=True)
        if df is None or df.empty:
            return jsonify({'error': 'not found'}), 404
        bars = []
        for _, r in df.iterrows():
            bars.append({
                'date': str(r['date']),
                'open': round(float(r['open']), 2),
                'high': round(float(r['high']), 2),
                'low': round(float(r['low']), 2),
                'close': round(float(r['close']), 2),
                'vol': int(r['vol']),
            })

    if period == 'weekly':
        bars = resample_bars(bars, 'weekly')
    elif period == 'monthly':
        bars = resample_bars(bars, 'monthly')

    bars = bars[-days:]
    if len(bars) < 5:
        return jsonify({'error': 'too few bars'}), 400
    return jsonify({'code': code, 'name': get_name(code), 'period': period, 'bars': bars})


# ============== WebSocket 事件 ==============

@sio.on('connect')
def on_connect():
    print('=== SocketIO client connected ===', flush=True)
    # 启动时把所有已有 signals 加入 _last_quotes
    try:
        items = load_signals()
        with _push_lock:
            for it in items:
                code = it['code'].strip().lower()
                if code not in _last_quotes:
                    _last_quotes[code] = {}
        print(f'Subscribed {len(items)} codes on connect', flush=True)
    except Exception as e:
        print('connect subscribe err:', e, flush=True)


@sio.on('subscribe')
def on_subscribe(data):
    codes = data.get('codes', []) if isinstance(data, dict) else []
    with _push_lock:
        for c in codes:
            if c not in _last_quotes:
                _last_quotes[c.strip().lower()] = {}
    sio.emit('subscribed', {'codes': codes})



# ============== 重点观察区（极值缩量战法） ==============

def load_watch():
    if not os.path.exists(WATCH_FILE):
        return []
    try:
        with open(WATCH_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


def save_watch(items):
    with open(WATCH_FILE, 'w', encoding='utf-8') as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


@app.route('/page/watch')
def page_watch():
    return send_from_directory(BASE, 'page_watch.html')


@app.route('/api/watch', methods=['GET'])
def list_watch():
    items = load_watch()
    # 计算每只信号当前实时价/量比/分时进度
    codes = [it['code'] for it in items]
    quotes = fetch_quotes_batch(codes) if codes else {}
    out = []
    for it in items:
        q = quotes.get(it['code'], {})
        # T+1/T+3 用本地 .day 算
        sig_date = str(it['date']).replace('-', '').replace('/', '')
        df = load_ohlcv(it['code'], with_vol=True)
        ret = {}
        if df is not None and not df.empty:
            df = df.reset_index(drop=True)
            idx = df.index[df['date'] == sig_date]
            if len(idx):
                i = int(idx[0])
                sig_close = df['close'].iloc[i]
                for h in [1, 2, 3]:
                    j = i + h
                    if j < len(df):
                        ret[f'T{h}'] = round((df['close'].iloc[j] / sig_close - 1) * 100, 2)
        out.append({
            **it,
            'name': it.get('name') or get_name(it['code']),
            'price': q.get('price'),
            'change_pct': q.get('change_pct'),
            'vol_ratio_now': q.get('vol_ratio'),
            'ret': ret,
        })
    return jsonify(out)


@app.route('/api/watch', methods=['POST'])
def add_watch():
    data = request.get_json(force=True) or {}
    code = (data.get('code') or '').strip()
    if not code:
        return jsonify({'error': 'code required'}), 400
    if not code.startswith(('sh', 'sz', 'bj')):
        mkt, num = code_to_mkt_digits(code)
        code = mkt + num
    item = {
        'id': int(datetime.now().timestamp() * 1000),
        'code': code,
        'name': get_name(code),
        'date': data.get('date') or datetime.now().strftime('%Y%m%d'),
        'vr_at_entry': data.get('vr'),
        'threshold': data.get('threshold', 0.6),  # 0.6 或 0.4
        'note': data.get('note', ''),
        'added_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    items = load_watch()
    items.append(item)
    save_watch(items)
    return jsonify(item)


@app.route('/api/watch/<int:wid>', methods=['DELETE'])
def del_watch(wid):
    items = load_watch()
    items = [it for it in items if it.get('id') != wid]
    save_watch(items)
    return jsonify({'ok': True, 'left': len(items)})


@app.route('/api/watch/<int:wid>', methods=['PATCH'])
def patch_watch(wid):
    data = request.get_json(force=True) or {}
    items = load_watch()
    for it in items:
        if it.get('id') == wid:
            for k in ('note', 'status'):
                if k in data:
                    it[k] = data[k]
            break
    save_watch(items)
    return jsonify({'ok': True})


@app.route('/api/watch/minline')
def watch_minline():
    code = request.args.get('code', '').strip().lower()
    if not code.startswith(('sh', 'sz', 'bj')):
        mkt, num = code_to_mkt_digits(code)
        code = mkt + num
    q = code_to_qt(code)
    if not q:
        return jsonify({'error': 'bad code'}), 400
    data = fetch_txmin(q)
    if not data:
        return jsonify({'error': 'fetch failed'}), 500
    return jsonify(data)


@app.route('/api/watch/stats')
def watch_stats():
    import numpy as np
    items = load_watch()
    pool_stats = {0.6: {h: [] for h in [1,2,3,5,10]}, 0.4: {h: [] for h in [1,2,3,5,10]}}
    for it in items:
        code = it.get('code', '')
        sig_date = str(it.get('date', '')).replace('-', '').replace('/', '')
        thr = float(it.get('threshold', 0.6))
        if not code or not sig_date:
            continue
        df = load_ohlcv(code, with_vol=True)
        if df is None or df.empty:
            continue
        df = df.reset_index(drop=True)
        idx = df.index[df['date'] == sig_date]
        if len(idx) == 0:
            # 可能是盘中还没收盘/没数据
            continue
        i = int(idx[0])
        sig_close = df['close'].iloc[i]
        for h in [1,2,3,5,10]:
            j = i + h
            if j < len(df):
                r = round((df['close'].iloc[j] / sig_close - 1) * 100, 2)
                if thr in pool_stats and h in pool_stats[thr]:
                    pool_stats[thr][h].append(r)
    out = {'pool': {}}
    for thr, by_h in pool_stats.items():
        sub = {'samples': sum(len(v) for v in by_h.values())}
        for h, vals in by_h.items():
            if vals:
                arr = np.array(vals)
                sub[f'T{h}'] = {
                    'count': int(len(arr)),
                    'win_rate': round(float((arr > 0).mean()) * 100, 1),
                    'mean': round(float(arr.mean()), 2),
                    'median': round(float(np.median(arr)), 2),
                    'min': round(float(arr.min()), 2),
                    'max': round(float(arr.max()), 2),
                }
            else:
                sub[f'T{h}'] = {'count': 0}
        out['pool'][str(thr)] = sub
    # 全市场基线（可选）
    if os.path.exists(WATCH_STATS_FILE):
        try:
            with open(WATCH_STATS_FILE, 'r', encoding='utf-8') as f:
                j = json.load(f)
            out['market'] = j.get('thresholds', {})
            out['market_meta'] = {'generated_at': j.get('generated_at'), 'scanned': j.get('scanned')}
        except Exception:
            pass
    out['pool_size'] = len(items)
    return jsonify(out)


if __name__ == '__main__':
    import os
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    # 启动时订阅所有已有 signals（让 push_quotes_loop 立刻有数据可推）
    try:
        items = load_signals()
        with _push_lock:
            for it in items:
                code = it['code'].strip().lower()
                if code not in _last_quotes:
                    _last_quotes[code] = {}
        print(f'[startup] subscribed {len(items)} codes', flush=True)
    except Exception as e:
        print('[startup] err:', e, flush=True)
    # 启动 push_quotes_loop 后台线程
    t = threading.Thread(target=push_quotes_loop, daemon=True)
    t.start()
    print('[startup] push_quotes_loop started', flush=True)
    # === 云端适配 ===
    import os as _os
    RENDER = _os.environ.get('RENDER', '')
    PORT = _os.environ.get('PORT', '')
    is_cloud = bool(RENDER or PORT)
    if is_cloud:
        # 云端：监听 0.0.0.0，端口来自环境变量
        _port = int(PORT) if PORT else 10000
        print(f'[cloud] 启动云端模式 port={_port}')
        sio.run(app, host='0.0.0.0', port=_port, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)
    else:
        # 本地
        sio.run(app, host='127.0.0.1', port=5001, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)
