#!/usr/bin/env python3
"""
KGEN Telegram Monitor v2 — BYBIT EDITION
=========================================
Bybit V5 API kullanır (Binance ABD IP engeli sorunu çözümü).
Pozisyonun Binance'te açık kalır, sadece veri Bybit'ten çekilir.
Fiyat farkları %0.1'in altında olduğu için PnL hesabı pratikte aynı kalır.

Tetiklenme:
- Her 5 dakikada bir GitHub Actions cron ile çalışır
- Anlık bildirimler: alarmlar, $2K+ likidasyonlar*, $5K+ balinalar, anomaliler
- 4 saatlik raporlar: 07/11/15/19/23 TR
- Günlük özet: 00 TR

(*) Bybit V5 REST API'sinde public liquidations endpoint yok — sadece WS var.
GitHub Actions cron tabanlı çalıştığı için canlı liquidations'ı dinleyemez.
Onun yerine "büyük market satışları" anomali olarak yakalanır.
"""

import os
import sys
import json
import time
import datetime as dt
import requests
from pathlib import Path
from zoneinfo import ZoneInfo

# ============================================================
# CONFIG
# ============================================================
TG_TOKEN = os.environ.get('TG_TOKEN', '')
TG_CHAT  = os.environ.get('TG_CHAT', '')

POS_ENTRY  = float(os.environ.get('POS_ENTRY', '0.1663'))
POS_QTY    = float(os.environ.get('POS_QTY', '39120'))
POS_MARGIN = float(os.environ.get('POS_MARGIN', '2840.67'))
POS_TARGET = float(os.environ.get('POS_TARGET', '0.25'))
ALARMS_JSON = os.environ.get('ALARMS', '[]')

# KGEN düşük hacim için kalibre eşikler
WHALE_HUGE      = 5000     # $5K+ tek işlem
PRICE_SPIKE_PCT = 3.0      # 5dk'da %3+
VOLUME_SPIKE_X  = 3.0      # son 5dk hacim, son 1 saat ortalamasının 3x'i

TR_TZ = ZoneInfo('Europe/Istanbul')
SYMBOL = 'KGENUSDT'
COINGECKO_ID = 'kgen'
CATEGORY = 'linear'  # USDT perpetual

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
# STATE
# ============================================================
def load_state():
    if not STATE_FILE.exists():
        return {
            'last_4h_report': None,
            'last_daily_report': None,
            'fired_alarms': [],
            'reported_anomalies': [],
            'reported_whales': [],
        }
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception as e:
        log(f'State load hata: {e} — yeni state ile başlıyorum')
        return {'last_4h_report':None,'last_daily_report':None,'fired_alarms':[],'reported_anomalies':[],'reported_whales':[]}

def save_state(s):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(s, f, indent=2)

# ============================================================
# TELEGRAM
# ============================================================
def tg_send(text, silent=False):
    if not TG_TOKEN or not TG_CHAT:
        log('⚠️  TG_TOKEN veya TG_CHAT yok — atlandı')
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
# BYBIT V5 API
# ============================================================
BYBIT_API = 'https://api.bybit.com'

def bybit_get(path, params=None):
    """Bybit V5 GET helper. Returns 'result' içeriği veya None."""
    try:
        r = requests.get(f'{BYBIT_API}{path}', params=params or {}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get('retCode') == 0:
                return data.get('result')
            log(f'Bybit {path} retCode={data.get("retCode")} msg={data.get("retMsg","")}')
        else:
            log(f'Bybit {path} HTTP {r.status_code}: {r.text[:120]}')
    except Exception as e:
        log(f'Bybit {path} hata: {e}')
    return None

def get_ticker():
    """KGEN ticker — fiyat, 24h, OI, funding rate hepsi burada"""
    res = bybit_get('/v5/market/tickers', {'category': CATEGORY, 'symbol': SYMBOL})
    if res and res.get('list'):
        return res['list'][0]
    return None

def get_price():
    t = get_ticker()
    return float(t.get('lastPrice', 0)) if t else None

def get_klines(interval, limit=200):
    """Bybit kline. interval: '5','15','60','240','D'.
    Response: list ters sırada (en yeni ilk). Çevirmek lazım.
    Format: [start, open, high, low, close, volume, turnover]"""
    res = bybit_get('/v5/market/kline', {
        'category': CATEGORY,
        'symbol': SYMBOL,
        'interval': str(interval),
        'limit': limit
    })
    if res and res.get('list'):
        # Bybit yeniden eskiye sıralı veriyor - eskiden yeniye çevir
        return list(reversed(res['list']))
    return []

def get_recent_trades(limit=1000):
    """Son public trades. Bybit max 1000 verir.
    Format: {execId, symbol, price, size, side ('Buy'|'Sell'), time}"""
    res = bybit_get('/v5/market/recent-trade', {
        'category': CATEGORY,
        'symbol': SYMBOL,
        'limit': limit
    })
    if res and res.get('list'):
        return res['list']
    return []

def get_long_short(period='1h'):
    """Top trader long/short ratio. period: 5min,15min,30min,1h,4h,1d"""
    res = bybit_get('/v5/market/account-ratio', {
        'category': CATEGORY,
        'symbol': SYMBOL,
        'period': period,
        'limit': 1
    })
    if res and res.get('list'):
        return res['list'][0]
    return None

def get_funding_history(limit=1):
    res = bybit_get('/v5/market/funding/history', {
        'category': CATEGORY,
        'symbol': SYMBOL,
        'limit': limit
    })
    if res and res.get('list'):
        return res['list']
    return []

# ============================================================
# COINGECKO (Market Cap)
# ============================================================
def get_market_cap():
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
    # Fallback: Bybit fiyat × tahmini supply
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
# ANLIK BİLDİRİMLER
# ============================================================
def check_alarms(state, price, pnl):
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
            log(f'🔔 Alarm: {key}')
    state['fired_alarms'] = fired

def check_huge_whales(state):
    """Son public trades'den $5K+ tek işlemleri yakala"""
    trades = get_recent_trades(1000)
    if not trades:
        return
    # Son 5 dakikadaki trade'lere odaklan
    cutoff = int(time.time() * 1000) - 5 * 60 * 1000
    reported = set(state.get('reported_whales', []))
    new_reported = list(reported)
    for t in trades:
        try:
            tid = str(t.get('execId', ''))
            tt = int(t.get('time', 0))
            if tt < cutoff:
                continue
            if tid in reported:
                continue
            p = float(t.get('price'))
            q = float(t.get('size'))
            value = p * q
            if value >= WHALE_HUGE:
                side = t.get('side', '').upper()  # 'BUY' or 'SELL'
                is_sell = (side == 'SELL')
                emoji = '🔴' if is_sell else '🟢'
                side_text = 'SATIŞ' if is_sell else 'ALIŞ'
                msg = (f'🐋 <b>KGEN SÜPER BALİNA</b>\n\n'
                       f'{emoji} <b>{side_text}</b>\n'
                       f'💰 Değer: <b>{fmt_usd(value)}</b>\n'
                       f'💵 Fiyat: <b>${fmt_price(p)}</b>\n'
                       f'📦 Miktar: {q:,.0f} KGEN\n'
                       f'⏰ {dt.datetime.fromtimestamp(tt/1000, TR_TZ).strftime("%H:%M:%S")}')
                tg_send(msg)
                log(f'🐋 Balina: {side_text} {fmt_usd(value)}')
            new_reported.append(tid)
        except Exception:
            continue
    state['reported_whales'] = new_reported[-500:]

def check_anomalies(state):
    """Fiyat spike, hacim spike"""
    klines = get_klines('5', 13)
    if len(klines) < 13:
        return
    last = klines[-1]
    prev_hour = klines[-13:-1]
    # Bybit kline format: [start, open, high, low, close, volume, turnover]
    try:
        last_start = str(last[0])
        o = float(last[1])
        c = float(last[4])
        last_vol = float(last[5])
    except (IndexError, ValueError, TypeError):
        return

    reported = state.get('reported_anomalies', [])

    # Fiyat spike
    if o > 0:
        change_pct = ((c - o) / o) * 100
        if abs(change_pct) >= PRICE_SPIKE_PCT:
            key = f"price_{last_start}"
            if key not in reported:
                arrow = '🚀' if change_pct > 0 else '🔻'
                msg = (f'⚡ <b>KGEN FİYAT SPIKE</b>\n\n'
                       f'{arrow} Son 5dk: <b>{change_pct:+.2f}%</b>\n'
                       f'📂 Açılış: ${fmt_price(o)}\n'
                       f'📁 Kapanış: ${fmt_price(c)}\n'
                       f'⏰ {now_tr().strftime("%H:%M")}')
                tg_send(msg)
                reported.append(key)
                log(f'⚡ Fiyat spike: {change_pct:+.2f}%')

    # Hacim spike
    avg_vol = sum(float(k[5]) for k in prev_hour) / len(prev_hour)
    if avg_vol > 0 and last_vol >= avg_vol * VOLUME_SPIKE_X:
        key = f"vol_{last_start}"
        if key not in reported:
            mult = last_vol / avg_vol
            msg = (f'⚡ <b>KGEN HACİM SPIKE</b>\n\n'
                   f'📊 Son 5dk hacmi normal saatlik ortalamanın <b>{mult:.1f}x</b>\'i\n'
                   f'💵 Anlık fiyat: ${fmt_price(c)}\n'
                   f'⏰ {now_tr().strftime("%H:%M")}\n\n'
                   f'<i>Bir şey oluyor — kontrol et.</i>')
            tg_send(msg)
            reported.append(key)
            log(f'⚡ Hacim spike: {mult:.1f}x')

    state['reported_anomalies'] = reported[-50:]

# ============================================================
# 4-HOUR REPORT
# ============================================================
def build_4h_report():
    log('📊 4 saatlik rapor hazırlanıyor...')

    # 4 saatlik 5dk mumları (48 mum)
    klines = get_klines('5', 48)
    if not klines or len(klines) < 10:
        log('⚠️ Yeterli kline yok')
        return None

    open_price = float(klines[0][1])
    close_price = float(klines[-1][4])
    change_pct = ((close_price - open_price) / open_price) * 100 if open_price > 0 else 0
    high = max(float(k[2]) for k in klines)
    low = min(float(k[3]) for k in klines)

    # Recent trades (max 1000) - 4 saati tam yakalayamayabilir ama deneriz
    trades = get_recent_trades(1000)
    cutoff_ms = int(time.time() * 1000) - 4 * 60 * 60 * 1000

    biggest_buy = {'value': 0, 'price': 0, 'qty': 0, 'time': 0}
    biggest_sell = {'value': 0, 'price': 0, 'qty': 0, 'time': 0}
    buy_count = 0
    sell_count = 0
    buy_volume = 0.0
    sell_volume = 0.0
    actual_period_ms = 4 * 60 * 60 * 1000  # default
    if trades:
        oldest_ms = int(trades[-1].get('time', cutoff_ms))
        if oldest_ms > cutoff_ms:
            actual_period_ms = int(time.time() * 1000) - oldest_ms

    for t in trades:
        try:
            tt = int(t.get('time', 0))
            if tt < cutoff_ms:
                continue
            p = float(t['price'])
            q = float(t['size'])
            v = p * q
            side = t.get('side', '').upper()  # 'BUY' / 'SELL'
            is_sell = (side == 'SELL')
            if is_sell:
                sell_count += 1
                sell_volume += v
                if v > biggest_sell['value']:
                    biggest_sell = {'value': v, 'price': p, 'qty': q, 'time': tt}
            else:
                buy_count += 1
                buy_volume += v
                if v > biggest_buy['value']:
                    biggest_buy = {'value': v, 'price': p, 'qty': q, 'time': tt}
        except:
            continue

    # Funding & ticker
    ticker = get_ticker()
    funding_rate = 0
    next_funding = 0
    if ticker:
        try:
            funding_rate = float(ticker.get('fundingRate', 0)) * 100
            next_funding = int(ticker.get('nextFundingTime', 0))
        except:
            pass

    # Long/Short
    ls = get_long_short('1h')
    if ls:
        try:
            buy_ratio = float(ls.get('buyRatio', 0))
            sell_ratio = float(ls.get('sellRatio', 0))
            long_pct = buy_ratio * 100
            short_pct = sell_ratio * 100
            ls_ratio = (buy_ratio / sell_ratio) if sell_ratio > 0 else 0
        except:
            long_pct = short_pct = ls_ratio = 0
    else:
        long_pct = short_pct = ls_ratio = 0

    # Market cap
    mc = get_market_cap()

    # FORMAT
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
    buy_pct_t = (buy_count / total * 100) if total > 0 else 0
    sell_pct_t = (sell_count / total * 100) if total > 0 else 0
    msg += f'<b>📋 İŞLEM SAYILARI</b>\n'
    msg += f'🟢 Alış: <b>{buy_count:,}</b> ({buy_pct_t:.1f}%) · {fmt_usd(buy_volume)}\n'
    msg += f'🔴 Satış: <b>{sell_count:,}</b> ({sell_pct_t:.1f}%) · {fmt_usd(sell_volume)}\n'
    msg += f'📦 Toplam: <b>{total:,}</b> işlem · {fmt_usd(buy_volume + sell_volume)}\n'
    # Veri sınırlama uyarısı
    if total >= 1000:
        msg += f'<i>⚠️ Bybit max 1000 trade veriyor — gerçek sayı daha yüksek olabilir</i>\n'
    elif actual_period_ms < 4 * 60 * 60 * 1000 and total > 800:
        h = actual_period_ms / 3_600_000
        msg += f'<i>ℹ️ Veri son {h:.1f} saati kapsıyor (1000 trade limiti)</i>\n'
    msg += '\n'

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
        msg += '\n'
        msg += f'🔢 FDV: {fmt_usd(mc.get("fdv", 0))}\n'
        msg += f'📊 Dolaşımdaki: {mc.get("supply_circ", 0):,.0f} KGEN'
        if mc.get('source') == 'fallback':
            msg += f'\n<i>⚠️ Tahmini (CoinGecko erişilemedi)</i>'
        msg += '\n'
    else:
        msg += f'<i>Veri alınamadı</i>\n'

    # Bonus: pozisyon durumu
    if close_price > 0:
        pnl = (close_price - POS_ENTRY) * POS_QTY
        roe = (pnl / POS_MARGIN) * 100 if POS_MARGIN > 0 else 0
        msg += f'\n<b>💼 POZİSYON</b>\n'
        msg += f'PnL: <b>{"+" if pnl>=0 else ""}${pnl:.2f}</b> · '
        msg += f'ROE: <b>{roe:+.2f}%</b>\n'

    return msg

# ============================================================
# DAILY REPORT
# ============================================================
def build_daily_report():
    log('📅 Günlük rapor hazırlanıyor...')

    ticker = get_ticker()
    if not ticker:
        return None

    try:
        close_price = float(ticker.get('lastPrice', 0))
        prev24 = float(ticker.get('prevPrice24h', 0))
        high = float(ticker.get('highPrice24h', 0))
        low = float(ticker.get('lowPrice24h', 0))
        change_pct = float(ticker.get('price24hPcnt', 0)) * 100
        volume = float(ticker.get('turnover24h', 0))  # USDT cinsinden
        funding_rate = float(ticker.get('fundingRate', 0)) * 100
    except:
        return None

    ls = get_long_short('1d')
    if ls:
        try:
            buy_ratio = float(ls.get('buyRatio', 0))
            sell_ratio = float(ls.get('sellRatio', 0))
            long_pct = buy_ratio * 100
            short_pct = sell_ratio * 100
            ls_ratio = (buy_ratio / sell_ratio) if sell_ratio > 0 else 0
        except:
            long_pct = short_pct = ls_ratio = 0
    else:
        long_pct = short_pct = ls_ratio = 0

    mc = get_market_cap()

    arrow = '📈' if change_pct >= 0 else '📉'
    color = '🟢' if change_pct >= 0 else '🔴'

    msg = f'📅 <b>KGEN GÜNLÜK ÖZET</b>\n'
    msg += f'<i>{now_tr().strftime("%d.%m.%Y")} kapanışı</i>\n'
    msg += f'━━━━━━━━━━━━━━━━━\n\n'

    msg += f'<b>{arrow} 24 SAATLİK PERFORMANS</b>\n'
    msg += f'{color} <b>{change_pct:+.2f}%</b>\n'
    msg += f'📂 Açılış: <code>${fmt_price(prev24)}</code>\n'
    msg += f'📁 Kapanış: <code>${fmt_price(close_price)}</code>\n'
    msg += f'⬆️ Tepe: <code>${fmt_price(high)}</code>\n'
    msg += f'⬇️ Dip: <code>${fmt_price(low)}</code>\n'
    msg += f'📊 Hacim: <b>{fmt_usd(volume)}</b>\n\n'

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
# SCHEDULER
# ============================================================
REPORT_HOURS = [7, 11, 15, 19, 23]
DAILY_HOUR = 0

def should_send_4h_report(state):
    n = now_tr()
    if n.hour not in REPORT_HOURS:
        return False
    tag = n.strftime("%Y-%m-%d_%H")
    return tag != state.get('last_4h_report', '')

def should_send_daily(state):
    n = now_tr()
    if n.hour != DAILY_HOUR:
        return False
    tag = n.strftime("%Y-%m-%d")
    return tag != state.get('last_daily_report', '')

def mark_4h_sent(state):
    state['last_4h_report'] = now_tr().strftime("%Y-%m-%d_%H")

def mark_daily_sent(state):
    state['last_daily_report'] = now_tr().strftime("%Y-%m-%d")

# ============================================================
# MAIN
# ============================================================
def main():
    log(f'🚀 KGEN Monitor v2 (Bybit) başladı · TR: {now_tr().strftime("%Y-%m-%d %H:%M")}')

    if not TG_TOKEN or not TG_CHAT:
        log('❌ TG_TOKEN ve TG_CHAT secrets gerekli — çıkıyorum')
        sys.exit(1)

    state = load_state()

    price = get_price()
    if price is None:
        log('⚠️ Bybit fiyat alınamadı, devam etmiyoruz')
        sys.exit(0)
    pnl = (price - POS_ENTRY) * POS_QTY
    log(f'💵 Fiyat: ${fmt_price(price)} | PnL: ${pnl:+.2f}')

    # Anlık bildirimler
    try: check_alarms(state, price, pnl)
    except Exception as e: log(f'check_alarms hata: {e}')

    try: check_huge_whales(state)
    except Exception as e: log(f'check_huge_whales hata: {e}')

    try: check_anomalies(state)
    except Exception as e: log(f'check_anomalies hata: {e}')

    # 4 saatlik rapor
    if should_send_4h_report(state):
        try:
            r = build_4h_report()
            if r and tg_send(r):
                mark_4h_sent(state)
                log('✅ 4 saatlik rapor gönderildi')
        except Exception as e:
            log(f'4h hata: {e}')

    # Günlük rapor
    if should_send_daily(state):
        try:
            r = build_daily_report()
            if r and tg_send(r):
                mark_daily_sent(state)
                log('✅ Günlük rapor gönderildi')
        except Exception as e:
            log(f'daily hata: {e}')

    save_state(state)
    log('✅ Bitti')

if __name__ == '__main__':
    main()
