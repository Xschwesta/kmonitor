#!/usr/bin/env python3
"""
KGEN Telegram Monitor
=====================
Runs every 5 minutes via GitHub Actions.

Responsibilities:
1) Anlık (instant) bildirimler — fiyat alarmları, $2K+ likidasyon, $5K+ balina, anomaliler
2) Periyodik raporlar — 4 saatlik özet (07/11/15/19/23 TR), günlük özet (00 TR)

Strateji:
- State file (state.json) ile bir önceki çalışmadan kalan verileri tutar (alarmlar fired, son rapor zamanı, vb.)
- Binance public API'lerinden veri çeker (auth gerekmez)
- CoinGecko'dan market cap (rate limit olduğunda fallback)
- Telegram'a HTML formatlı mesaj gönderir
"""

import os
import sys
import json
import time
import math
import datetime as dt
import requests
from pathlib import Path
from zoneinfo import ZoneInfo

# ============================================================
# CONFIG (GitHub Secrets'tan gelir, yoksa env'den)
# ============================================================
TG_TOKEN = os.environ.get('TG_TOKEN', '')
TG_CHAT  = os.environ.get('TG_CHAT', '')

# Pozisyon bilgileri (Secrets'tan gelir)
POS_ENTRY  = float(os.environ.get('POS_ENTRY', '0.1663'))
POS_QTY    = float(os.environ.get('POS_QTY', '39120'))
POS_MARGIN = float(os.environ.get('POS_MARGIN', '2840.67'))
POS_TARGET = float(os.environ.get('POS_TARGET', '0.25'))

# Alarmlar (JSON string olarak Secrets'a koyulur, opsiyonel)
# Örnek: '[{"type":"up","target":0.20},{"type":"down","target":0.14}]'
ALARMS_JSON = os.environ.get('ALARMS', '[]')

# Anomali eşikleri (KGEN düşük hacim için kalibre)
WHALE_HUGE      = 5000     # Anlık $5K+ balina bildirimi
LIQ_BIG         = 2000     # Anlık $2K+ likidasyon
LIQ_HUGE        = 5000     # Vurgulu uyarı
PRICE_SPIKE_PCT = 3.0      # 5dk'da %3+ hareket
VOLUME_SPIKE_X  = 3.0      # Hacim son saat ortalamasının 3x'i
OI_SPIKE_PCT    = 10.0     # 15dk'da OI %10+ değişim

# Saat dilimi
TR_TZ = ZoneInfo('Europe/Istanbul')
SYMBOL = 'KGENUSDT'
COINGECKO_ID = 'kgen'

STATE_FILE = Path(__file__).parent / 'state' / 'state.json'

# ============================================================
# HELPERS
# ============================================================
def log(msg):
    print(f'[{dt.datetime.now(TR_TZ).strftime("%H:%M:%S")}] {msg}', flush=True)

def fmt_usd(v):
    if v >= 1_000_000:
        return f'${v/1_000_000:.2f}M'
    if v >= 1_000:
        return f'${v/1_000:.2f}K'
    return f'${v:.2f}'

def fmt_price(v, dec=5):
    return f'{v:.{dec}f}'

def now_tr():
    return dt.datetime.now(TR_TZ)

# ============================================================
# STATE PERSISTENCE
# ============================================================
def load_state():
    if not STATE_FILE.exists():
        return {
            'last_4h_report': None,
            'last_daily_report': None,
            'fired_alarms': [],
            'last_known_price': 0,
            'session_high': 0,
            'session_low': 0,
            'last_price_check_time': 0,
            'last_oi': 0,
            'last_oi_time': 0,
            'reported_liqs': [],   # likidasyon tx ID'leri (zaten bildirildi)
            'reported_whales': [], # büyük balina tx ID'leri
        }
    with open(STATE_FILE) as f:
        return json.load(f)

def save_state(s):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(s, f, indent=2)

# ============================================================
# TELEGRAM
# ============================================================
def tg_send(text, silent=False):
    if not TG_TOKEN or not TG_CHAT:
        log('⚠️  TG_TOKEN veya TG_CHAT yok — mesaj atlandı')
        return False
    url = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'
    try:
        r = requests.post(url, json={
            'chat_id': TG_CHAT,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
            'disable_notification': silent
        }, timeout=10)
        ok = r.json().get('ok', False)
        if not ok:
            log(f'❌ TG hata: {r.text[:200]}')
        return ok
    except Exception as e:
        log(f'❌ TG istisna: {e}')
        return False

# ============================================================
# BINANCE FUTURES API
# ============================================================
BFAPI = 'https://fapi.binance.com'

def bget(path, params=None):
    try:
        r = requests.get(f'{BFAPI}{path}', params=params or {}, timeout=10)
        if r.status_code == 200:
            return r.json()
        log(f'Binance {path} HTTP {r.status_code}: {r.text[:120]}')
    except Exception as e:
        log(f'Binance {path} hata: {e}')
    return None

def get_price():
    d = bget('/fapi/v1/ticker/price', {'symbol': SYMBOL})
    return float(d['price']) if d else None

def get_24h_stats():
    """24h fiyat değişimi, hacim vb."""
    return bget('/fapi/v1/ticker/24hr', {'symbol': SYMBOL})

def get_funding():
    """premiumIndex: lastFundingRate, nextFundingTime, markPrice"""
    return bget('/fapi/v1/premiumIndex', {'symbol': SYMBOL})

def get_klines(interval, limit=100):
    """OHLCV mumlar. Returns list of [openTime, o, h, l, c, v, closeTime, qv, n, ...]"""
    return bget('/fapi/v1/klines', {'symbol': SYMBOL, 'interval': interval, 'limit': limit})

def get_long_short():
    """Top trader long/short ratio"""
    d = bget('/futures/data/topLongShortAccountRatio', {'symbol': SYMBOL, 'period': '5m', 'limit': 1})
    return d[0] if d else None

def get_open_interest():
    """Anlık OI USD değer"""
    return bget('/fapi/v1/openInterest', {'symbol': SYMBOL})

def get_oi_history(period='5m', limit=12):
    return bget('/futures/data/openInterestHist', {'symbol': SYMBOL, 'period': period, 'limit': limit})

def get_liquidations(limit=50):
    """Son zorla kapatma emirleri (allForceOrders)"""
    return bget('/fapi/v1/forceOrders', {'symbol': SYMBOL, 'limit': limit})

def get_agg_trades(start_ms, end_ms, limit=1000):
    """Belirli zaman aralığındaki tüm işlemler"""
    return bget('/fapi/v1/aggTrades', {
        'symbol': SYMBOL,
        'startTime': start_ms,
        'endTime': end_ms,
        'limit': limit
    })

# ============================================================
# COINGECKO (Market Cap)
# ============================================================
def get_market_cap():
    """CoinGecko ücretsiz API'den market cap. Rate limit olursa fallback."""
    try:
        r = requests.get(
            f'https://api.coingecko.com/api/v3/coins/{COINGECKO_ID}',
            params={'localization':'false','tickers':'false','community_data':'false','developer_data':'false'},
            timeout=10
        )
        if r.status_code == 200:
            d = r.json()
            md = d.get('market_data', {})
            return {
                'mcap': md.get('market_cap', {}).get('usd', 0),
                'fdv': md.get('fully_diluted_valuation', {}).get('usd', 0),
                'supply_circ': md.get('circulating_supply', 0),
                'supply_total': md.get('total_supply', 0),
                'rank': md.get('market_cap_rank', 0),
                'change_24h': md.get('price_change_percentage_24h', 0),
                'source': 'coingecko'
            }
        log(f'CoinGecko HTTP {r.status_code}')
    except Exception as e:
        log(f'CoinGecko hata: {e}')
    # Fallback: yaklaşık 200M circulating supply ile hesapla
    p = get_price()
    if p:
        circ = 200_000_000
        return {
            'mcap': p * circ,
            'fdv': p * 1_000_000_000,
            'supply_circ': circ,
            'supply_total': 1_000_000_000,
            'rank': 0,
            'change_24h': 0,
            'source': 'fallback'
        }
    return None

# ============================================================
# INSTANT NOTIFICATIONS (her 5 dk)
# ============================================================
def check_alarms(state, price, pnl):
    """Kullanıcının koyduğu fiyat/PnL alarmları"""
    try:
        alarms = json.loads(ALARMS_JSON)
    except:
        alarms = []
    if not alarms:
        return
    fired = state.get('fired_alarms', [])
    for a in alarms:
        key = f"{a.get('type')}:{a.get('target')}"
        if key in fired:
            continue
        triggered = False
        atype = a.get('type')
        target = float(a.get('target'))
        if atype == 'up' and price >= target:
            triggered = True
        elif atype == 'down' and price <= target:
            triggered = True
        elif atype == 'pnl':
            if target >= 0 and pnl >= target:
                triggered = True
            elif target < 0 and pnl <= target:
                triggered = True
        if triggered:
            if atype == 'pnl':
                msg = (f'🚨 <b>KGEN ALARM TETİKLENDİ</b>\n\n'
                       f'💰 PnL Hedefi: <b>{"+" if target>=0 else "-"}${abs(target):.2f}</b>\n'
                       f'📊 Anlık PnL: <b>{"+" if pnl>=0 else ""}${pnl:.2f}</b>\n'
                       f'💵 Anlık Fiyat: <b>${fmt_price(price)}</b>\n'
                       f'⏰ {now_tr().strftime("%H:%M:%S")}')
            else:
                arrow = '📈' if atype == 'up' else '📉'
                action = 'yukarı geçti' if atype == 'up' else 'aşağı düştü'
                msg = (f'🚨 <b>KGEN ALARM TETİKLENDİ</b>\n\n'
                       f'{arrow} Fiyat <b>{action}</b>\n'
                       f'🎯 Hedef: <b>${fmt_price(target)}</b>\n'
                       f'💵 Anlık: <b>${fmt_price(price)}</b>\n'
                       f'⏰ {now_tr().strftime("%H:%M:%S")}')
            tg_send(msg)
            fired.append(key)
            log(f'🔔 Alarm tetiklendi: {key}')
    state['fired_alarms'] = fired

def check_liquidations(state):
    """Son likidasyonları kontrol et, $2K+ olanları bildir"""
    liqs = get_liquidations(50) or []
    reported = set(state.get('reported_liqs', []))
    new_reported = list(reported)
    big_liqs = []
    for l in liqs:
        # Her likidasyon emrinde id 'orderId' veya 'time'+'price' kombosu
        key = f"{l.get('time', 0)}_{l.get('price', '')}_{l.get('origQty', '')}"
        if key in reported:
            continue
        try:
            p = float(l.get('price', l.get('averagePrice', 0)))
            q = float(l.get('origQty', l.get('executedQty', 0)))
            value = p * q
            if value >= LIQ_BIG:
                side_raw = l.get('side', '').upper()
                # SELL forceOrder = LONG pozisyon kapanmış
                pos_side = 'LONG' if side_raw == 'SELL' else 'SHORT'
                big_liqs.append({
                    'value': value,
                    'price': p,
                    'pos_side': pos_side,
                    'time': l.get('time', 0)
                })
            new_reported.append(key)
        except Exception as e:
            log(f'Liq parse err: {e}')
            continue
    # State'de sadece son 200 ID tut (büyümesin)
    state['reported_liqs'] = new_reported[-200:]
    # Anlık bildirim: $2K+ olanlar tek tek
    for liq in big_liqs:
        emoji = '🔴' if liq['pos_side'] == 'LONG' else '🟢'
        urgency = '🚨' if liq['value'] >= LIQ_HUGE else '⚡'
        msg = (f'{urgency} <b>KGEN LİKİDASYON</b>\n\n'
               f'{emoji} <b>{liq["pos_side"]}</b> pozisyon kapatıldı\n'
               f'💰 Değer: <b>{fmt_usd(liq["value"])}</b>\n'
               f'💵 Fiyat: <b>${fmt_price(liq["price"])}</b>\n'
               f'⏰ {dt.datetime.fromtimestamp(liq["time"]/1000, TR_TZ).strftime("%H:%M:%S")}')
        tg_send(msg)
        log(f'💥 Liq bildirildi: {liq["pos_side"]} {fmt_usd(liq["value"])}')

def check_huge_whales(state):
    """Son 5 dakikadaki $5K+ balinaları kontrol et"""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - 5 * 60 * 1000
    trades = get_agg_trades(start_ms, end_ms, 1000) or []
    reported = set(state.get('reported_whales', []))
    new_reported = list(reported)
    for t in trades:
        tid = str(t.get('a', ''))
        if tid in reported:
            continue
        try:
            p = float(t.get('p'))
            q = float(t.get('q'))
            value = p * q
            if value >= WHALE_HUGE:
                is_sell = t.get('m', False)  # buyer maker = SELL
                side_emoji = '🔴' if is_sell else '🟢'
                side_text = 'SATIŞ' if is_sell else 'ALIŞ'
                msg = (f'🐋 <b>KGEN SÜPER BALİNA</b>\n\n'
                       f'{side_emoji} <b>{side_text}</b>\n'
                       f'💰 Değer: <b>{fmt_usd(value)}</b>\n'
                       f'💵 Fiyat: <b>${fmt_price(p)}</b>\n'
                       f'📦 Miktar: {q:,.0f} KGEN\n'
                       f'⏰ {dt.datetime.fromtimestamp(t.get("T", end_ms)/1000, TR_TZ).strftime("%H:%M:%S")}')
                tg_send(msg)
                log(f'🐋 Süper balina: {side_text} {fmt_usd(value)}')
            new_reported.append(tid)
        except Exception as e:
            continue
    state['reported_whales'] = new_reported[-500:]

def check_anomalies(state):
    """Fiyat spike, hacim spike, OI spike anomalileri"""
    # Son 6 5dk mum: hareket ve hacim
    klines = get_klines('5m', 13) or []
    if len(klines) < 13:
        return

    # Son mum: en yeni (kapanmamış olabilir), önceki 12 mum: 1 saat
    last = klines[-1]
    prev_hour = klines[-13:-1]

    # Fiyat spike: son mumda %3+ hareket
    o = float(last[1])
    c = float(last[4])
    if o > 0:
        change_pct = ((c - o) / o) * 100
        if abs(change_pct) >= PRICE_SPIKE_PCT:
            state_key = f"price_spike_{last[0]}"
            if state_key not in state.get('reported_anomalies', []):
                arrow = '🚀' if change_pct > 0 else '🔻'
                msg = (f'⚡ <b>KGEN FİYAT SPIKE</b>\n\n'
                       f'{arrow} Son 5dk: <b>{change_pct:+.2f}%</b>\n'
                       f'📂 Açılış: ${fmt_price(o)}\n'
                       f'📁 Kapanış: ${fmt_price(c)}\n'
                       f'⏰ {now_tr().strftime("%H:%M")}')
                tg_send(msg)
                state.setdefault('reported_anomalies', []).append(state_key)
                log(f'⚡ Fiyat spike: {change_pct:+.2f}%')

    # Hacim spike: son mum hacmi, önceki 12 mum ortalamasının 3x'i
    last_vol = float(last[5])  # base asset volume
    avg_vol = sum(float(k[5]) for k in prev_hour) / len(prev_hour)
    if avg_vol > 0 and last_vol >= avg_vol * VOLUME_SPIKE_X:
        state_key = f"vol_spike_{last[0]}"
        if state_key not in state.get('reported_anomalies', []):
            mult = last_vol / avg_vol
            msg = (f'⚡ <b>KGEN HACİM SPIKE</b>\n\n'
                   f'📊 Son 5dk hacmi normal saatlik ortalamanın <b>{mult:.1f}x</b>\'i\n'
                   f'💵 Anlık fiyat: ${fmt_price(c)}\n'
                   f'⏰ {now_tr().strftime("%H:%M")}\n\n'
                   f'<i>Bir şey oluyor — kontrol et.</i>')
            tg_send(msg)
            state.setdefault('reported_anomalies', []).append(state_key)
            log(f'⚡ Hacim spike: {mult:.1f}x')

    # State temizle - sadece son 50 anomali kaydı tut
    anom = state.get('reported_anomalies', [])
    if len(anom) > 50:
        state['reported_anomalies'] = anom[-50:]

# ============================================================
# 4-HOUR REPORT
# ============================================================
def build_4h_report():
    """4 saatlik özet raporu"""
    log('📊 4 saatlik rapor hazırlanıyor...')

    # 4 saat öncesi - şimdi
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - 4 * 60 * 60 * 1000

    # Fiyat değişimi: 4h kline (1 mum) ya da 5m'lik 48 mum
    klines_5m = get_klines('5m', 48) or []
    if not klines_5m:
        return None

    open_price = float(klines_5m[0][1])
    close_price = float(klines_5m[-1][4])
    change_pct = ((close_price - open_price) / open_price) * 100 if open_price > 0 else 0
    high = max(float(k[2]) for k in klines_5m)
    low = min(float(k[3]) for k in klines_5m)

    # Tüm trade'leri 4 saatlik aralıkta çek (sayfalı)
    all_trades = []
    cursor = start_ms
    safety = 0
    while cursor < end_ms and safety < 20:
        chunk = get_agg_trades(cursor, end_ms, 1000) or []
        if not chunk:
            break
        all_trades.extend(chunk)
        if len(chunk) < 1000:
            break
        new_cursor = chunk[-1].get('T', cursor) + 1
        if new_cursor <= cursor:
            break
        cursor = new_cursor
        safety += 1
        time.sleep(0.15)  # rate limit'e takılmamak için

    # En büyük alış / satış
    biggest_buy = {'value': 0, 'price': 0, 'qty': 0, 'time': 0}
    biggest_sell = {'value': 0, 'price': 0, 'qty': 0, 'time': 0}
    buy_count = 0
    sell_count = 0
    buy_volume = 0
    sell_volume = 0
    for t in all_trades:
        try:
            p = float(t['p'])
            q = float(t['q'])
            v = p * q
            is_sell = t.get('m', False)  # buyer is maker → seller initiated → SELL
            if is_sell:
                sell_count += 1
                sell_volume += v
                if v > biggest_sell['value']:
                    biggest_sell = {'value': v, 'price': p, 'qty': q, 'time': t.get('T', 0)}
            else:
                buy_count += 1
                buy_volume += v
                if v > biggest_buy['value']:
                    biggest_buy = {'value': v, 'price': p, 'qty': q, 'time': t.get('T', 0)}
        except:
            continue

    # Funding rate
    fund = get_funding()
    funding_rate = float(fund.get('lastFundingRate', 0)) * 100 if fund else 0
    next_funding = int(fund.get('nextFundingTime', 0)) if fund else 0

    # Long/Short ratio
    ls = get_long_short()
    if ls:
        long_pct = float(ls.get('longAccount', 0)) * 100
        short_pct = float(ls.get('shortAccount', 0)) * 100
        ls_ratio = float(ls.get('longShortRatio', 1))
    else:
        long_pct = short_pct = ls_ratio = 0

    # Market cap
    mc = get_market_cap()

    # FORMAT MESAJ
    arrow = '📈' if change_pct >= 0 else '📉'
    color = '🟢' if change_pct >= 0 else '🔴'
    msg = f'{arrow} <b>KGEN 4 SAATLİK RAPOR</b>\n'
    msg += f'<i>{now_tr().strftime("%d.%m.%Y · %H:%M")}</i>\n'
    msg += f'━━━━━━━━━━━━━━━━━\n\n'

    # 1) Fiyat değişimi
    msg += f'<b>📊 4 SAATLİK DEĞİŞİM</b>\n'
    msg += f'{color} <b>{change_pct:+.2f}%</b>\n'
    msg += f'📂 Başlangıç: <code>${fmt_price(open_price)}</code>\n'
    msg += f'📁 Şimdi: <code>${fmt_price(close_price)}</code>\n'
    msg += f'⬆️ En yüksek: <code>${fmt_price(high)}</code>\n'
    msg += f'⬇️ En düşük: <code>${fmt_price(low)}</code>\n\n'

    # 2) En büyük alış/satış
    msg += f'<b>🏆 EN BÜYÜK İŞLEMLER</b>\n'
    if biggest_buy['value'] > 0:
        bb_time = dt.datetime.fromtimestamp(biggest_buy['time']/1000, TR_TZ).strftime("%H:%M")
        msg += f'🟢 Alış: <b>{fmt_usd(biggest_buy["value"])}</b> @ ${fmt_price(biggest_buy["price"])} ({bb_time})\n'
    else:
        msg += f'🟢 Alış: <i>kayıt yok</i>\n'
    if biggest_sell['value'] > 0:
        bs_time = dt.datetime.fromtimestamp(biggest_sell['time']/1000, TR_TZ).strftime("%H:%M")
        msg += f'🔴 Satış: <b>{fmt_usd(biggest_sell["value"])}</b> @ ${fmt_price(biggest_sell["price"])} ({bs_time})\n'
    else:
        msg += f'🔴 Satış: <i>kayıt yok</i>\n'
    msg += '\n'

    # 3) İşlem sayıları
    total = buy_count + sell_count
    buy_pct = (buy_count / total * 100) if total > 0 else 0
    sell_pct = (sell_count / total * 100) if total > 0 else 0
    msg += f'<b>📋 İŞLEM SAYILARI</b>\n'
    msg += f'🟢 Alış: <b>{buy_count:,}</b> ({buy_pct:.1f}%) · {fmt_usd(buy_volume)}\n'
    msg += f'🔴 Satış: <b>{sell_count:,}</b> ({sell_pct:.1f}%) · {fmt_usd(sell_volume)}\n'
    msg += f'📦 Toplam: <b>{total:,}</b> işlem · {fmt_usd(buy_volume + sell_volume)}\n\n'

    # 4) Funding
    msg += f'<b>💸 FUNDING RATE</b>\n'
    msg += f'Oran: <b>{funding_rate:+.4f}%</b>\n'
    if funding_rate > 0:
        msg += f'➡️ <b>LONG\'lar SHORT\'lara ödüyor</b> 🔴\n'
    elif funding_rate < 0:
        msg += f'➡️ <b>SHORT\'lar LONG\'lara ödüyor</b> 🟢\n'
    else:
        msg += f'➡️ Nötr\n'
    if next_funding:
        nf = dt.datetime.fromtimestamp(next_funding/1000, TR_TZ)
        msg += f'⏳ Sonraki: <code>{nf.strftime("%H:%M")}</code>\n'
    msg += '\n'

    # 5) Long/Short
    msg += f'<b>⚖️ LONG / SHORT ORANI</b>\n'
    if ls:
        if ls_ratio > 1.5:
            sentiment = '🐂 Aşırı LONG'
        elif ls_ratio > 1.1:
            sentiment = '📈 LONG ağırlıklı'
        elif ls_ratio > 0.9:
            sentiment = '⚖️ Dengeli'
        elif ls_ratio > 0.66:
            sentiment = '📉 SHORT ağırlıklı'
        else:
            sentiment = '🐻 Aşırı SHORT'
        msg += f'🟢 Long: <b>{long_pct:.1f}%</b>\n'
        msg += f'🔴 Short: <b>{short_pct:.1f}%</b>\n'
        msg += f'🎯 Oran: <code>{ls_ratio:.3f}</code> — {sentiment}\n\n'
    else:
        msg += f'<i>Veri alınamadı</i>\n\n'

    # 6) Market cap
    msg += f'<b>💼 MARKET CAP</b>\n'
    if mc and mc.get('mcap'):
        msg += f'💵 MC: <b>{fmt_usd(mc["mcap"])}</b>'
        if mc.get('rank'):
            msg += f' (#{mc["rank"]})'
        msg += f'\n'
        msg += f'🔢 FDV: {fmt_usd(mc.get("fdv", 0))}\n'
        msg += f'📊 Dolaşımdaki: {mc.get("supply_circ", 0):,.0f} KGEN'
        if mc.get('source') == 'fallback':
            msg += f'\n<i>⚠️ Tahmini (CoinGecko API erişilemedi)</i>'
        msg += '\n'
    else:
        msg += f'<i>Veri alınamadı</i>\n'

    return msg

# ============================================================
# DAILY REPORT
# ============================================================
def build_daily_report():
    log('📅 Günlük rapor hazırlanıyor...')
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - 24 * 60 * 60 * 1000

    # 24h ticker
    stats24 = get_24h_stats() or {}
    open_price = float(stats24.get('openPrice', 0))
    close_price = float(stats24.get('lastPrice', 0))
    high = float(stats24.get('highPrice', 0))
    low = float(stats24.get('lowPrice', 0))
    volume = float(stats24.get('quoteVolume', 0))  # USDT volume
    change_pct = float(stats24.get('priceChangePercent', 0))
    trade_count = int(stats24.get('count', 0))

    # Funding history (3 funding/gün)
    fund = get_funding()
    funding_rate = float(fund.get('lastFundingRate', 0)) * 100 if fund else 0

    # Long/Short
    ls = get_long_short()
    if ls:
        long_pct = float(ls.get('longAccount', 0)) * 100
        short_pct = float(ls.get('shortAccount', 0)) * 100
        ls_ratio = float(ls.get('longShortRatio', 1))
    else:
        long_pct = short_pct = ls_ratio = 0

    # Market cap
    mc = get_market_cap()

    arrow = '📈' if change_pct >= 0 else '📉'
    color = '🟢' if change_pct >= 0 else '🔴'

    msg = f'📅 <b>KGEN GÜNLÜK ÖZET</b>\n'
    msg += f'<i>{now_tr().strftime("%d.%m.%Y")} kapanışı</i>\n'
    msg += f'━━━━━━━━━━━━━━━━━\n\n'

    msg += f'<b>{arrow} 24 SAATLİK PERFORMANS</b>\n'
    msg += f'{color} <b>{change_pct:+.2f}%</b>\n'
    msg += f'📂 Açılış: <code>${fmt_price(open_price)}</code>\n'
    msg += f'📁 Kapanış: <code>${fmt_price(close_price)}</code>\n'
    msg += f'⬆️ Tepe: <code>${fmt_price(high)}</code>\n'
    msg += f'⬇️ Dip: <code>${fmt_price(low)}</code>\n'
    msg += f'📊 Hacim: <b>{fmt_usd(volume)}</b>\n'
    msg += f'📋 Toplam işlem: <b>{trade_count:,}</b>\n\n'

    msg += f'<b>💸 FUNDING RATE</b>\n'
    msg += f'<b>{funding_rate:+.4f}%</b> — '
    if funding_rate > 0:
        msg += f'LONG\'lar ödüyor 🔴\n\n'
    elif funding_rate < 0:
        msg += f'SHORT\'lar ödüyor 🟢\n\n'
    else:
        msg += f'Nötr\n\n'

    msg += f'<b>⚖️ LONG / SHORT</b>\n'
    if ls:
        msg += f'🟢 Long: <b>{long_pct:.1f}%</b> · 🔴 Short: <b>{short_pct:.1f}%</b>\n'
        msg += f'🎯 Oran: <code>{ls_ratio:.3f}</code>\n\n'

    msg += f'<b>💼 MARKET CAP</b>\n'
    if mc and mc.get('mcap'):
        msg += f'💵 MC: <b>{fmt_usd(mc["mcap"])}</b>'
        if mc.get('rank'):
            msg += f' (#{mc["rank"]})'
        msg += '\n'
        msg += f'📊 Dolaşımdaki: {mc.get("supply_circ", 0):,.0f} KGEN\n'

    # Pozisyon özeti
    if close_price > 0:
        pnl = (close_price - POS_ENTRY) * POS_QTY
        roe = (pnl / POS_MARGIN) * 100 if POS_MARGIN > 0 else 0
        msg += f'\n<b>💼 POZİSYON DURUMU</b>\n'
        msg += f'💵 PnL: <b>{"+" if pnl>=0 else ""}${pnl:.2f}</b>\n'
        msg += f'📊 ROE: <b>{roe:+.2f}%</b>\n'
        msg += f'🎯 Hedef: ${fmt_price(POS_TARGET)} ('
        msg += f'{((POS_TARGET-close_price)/close_price*100):+.1f}% uzakta)\n'

    msg += f'\n<i>İyi geceler 🌙</i>'
    return msg

# ============================================================
# SCHEDULER LOGIC
# ============================================================
REPORT_HOURS = [7, 11, 15, 19, 23]
DAILY_HOUR = 0

def should_send_4h_report(state):
    """Mevcut saat 7/11/15/19/23 ise ve henüz bu saat için rapor atılmadıysa True"""
    n = now_tr()
    if n.hour not in REPORT_HOURS:
        return False
    # Saat tag'i (örn. "2026-05-04_15") henüz işlenmediyse
    tag = n.strftime("%Y-%m-%d_%H")
    last = state.get('last_4h_report', '')
    return tag != last

def should_send_daily(state):
    n = now_tr()
    if n.hour != DAILY_HOUR:
        return False
    tag = n.strftime("%Y-%m-%d")
    last = state.get('last_daily_report', '')
    return tag != last

def mark_4h_sent(state):
    state['last_4h_report'] = now_tr().strftime("%Y-%m-%d_%H")

def mark_daily_sent(state):
    state['last_daily_report'] = now_tr().strftime("%Y-%m-%d")

# ============================================================
# MAIN
# ============================================================
def main():
    log(f'🚀 KGEN Monitor başladı · TR saati: {now_tr().strftime("%Y-%m-%d %H:%M")}')

    # Sanity check
    if not TG_TOKEN or not TG_CHAT:
        log('❌ TG_TOKEN ve TG_CHAT secrets gerekli — çıkıyorum')
        sys.exit(1)

    state = load_state()

    # Şu anki fiyat (PnL hesabı için)
    price = get_price()
    if price is None:
        log('⚠️ Binance fiyat alınamadı, devam etmiyoruz')
        sys.exit(0)
    pnl = (price - POS_ENTRY) * POS_QTY
    log(f'💵 Anlık fiyat: ${fmt_price(price)} | PnL: ${pnl:+.2f}')

    # 1) ANLIK BİLDİRİMLER (her run)
    try:
        check_alarms(state, price, pnl)
    except Exception as e:
        log(f'check_alarms hata: {e}')

    try:
        check_liquidations(state)
    except Exception as e:
        log(f'check_liquidations hata: {e}')

    try:
        check_huge_whales(state)
    except Exception as e:
        log(f'check_huge_whales hata: {e}')

    try:
        check_anomalies(state)
    except Exception as e:
        log(f'check_anomalies hata: {e}')

    # 2) 4 SAATLİK RAPOR
    if should_send_4h_report(state):
        try:
            report = build_4h_report()
            if report:
                if tg_send(report):
                    mark_4h_sent(state)
                    log('✅ 4 saatlik rapor gönderildi')
        except Exception as e:
            log(f'4h rapor hata: {e}')

    # 3) GÜNLÜK RAPOR
    if should_send_daily(state):
        try:
            report = build_daily_report()
            if report:
                if tg_send(report):
                    mark_daily_sent(state)
                    log('✅ Günlük rapor gönderildi')
        except Exception as e:
            log(f'daily rapor hata: {e}')

    # State kaydet
    state['last_known_price'] = price
    save_state(state)
    log('✅ Bitti')

if __name__ == '__main__':
    main()
