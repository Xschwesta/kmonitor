# 🤖 KGEN Telegram Monitor

KGEN/USDT Binance Futures için 7/24 çalışan Telegram bildirim sistemi.
GitHub Actions üzerinde her 5 dakikada bir çalışır.

## 📦 Ne Yapar?

**Anlık (her 5 dk kontrol):**
- 🔔 Fiyat / PnL alarmları (uygulamadan ayrı, kapalıyken bile çalışır)
- ⚡ $2K+ likidasyonlar (LONG / SHORT pozisyon kapanmaları)
- 🐋 $5K+ süper balina işlemleri
- 📊 Anomali tespiti: 5dk'da %3+ fiyat hareketi, 3x hacim spike

**Periyodik raporlar (Türkiye saati):**
- 🕖 **4 saatlik rapor** — 07:00, 11:00, 15:00, 19:00, 23:00 (5 rapor/gün)
- 🕛 **Günlük özet** — 00:00 (gün kapanışı)

## 📋 4 Saatlik Rapor İçeriği

1. ✅ 4 saatlik fiyat değişimi (% + açılış/kapanış/yüksek/düşük)
2. ✅ En büyük alış ve en büyük satış işlemi (USD değer + saat)
3. ✅ Toplam alış / satış işlem adedi + hacim
4. ✅ Funding rate (LONG mu SHORT mu ödüyor)
5. ✅ Long/Short oranı (top trader hesapları)
6. ✅ Güncel market cap (CoinGecko)

---

## 🚀 KURULUM (15 dakika)

### 1. Telegram Bot oluştur (zaten yaptıysan atla)

1. Telegram'da **@BotFather** ara
2. `/newbot` yaz, bot adını ve username'ini belirle
3. **Token'ı kopyala** (örn: `1234567890:ABCdef...`)
4. Yeni botunu aç, `/start` gönder
5. **@userinfobot** ara → `/start` → **Chat ID'ni** verecek (örn: `123456789`)

### 2. GitHub Repo oluştur (PRIVATE!)

1. GitHub'da yeni repo aç: **Settings → Visibility = Private** (kimse göremez!)
2. Repo adı: `kgen-monitor` (veya istediğin)
3. **Bu klasördeki dosyaları repo'ya yükle:**
   - `monitor.py`
   - `.github/workflows/monitor.yml`
   - Bu `README.md` (opsiyonel)
   - Boş bir `state/` klasörü (script kendi yaratır da, manuel açabilirsin)

**Yükleme yöntemi 1 — Web UI (kolay):**
- Repo sayfasında "Add file" → "Upload files"
- Tüm dosyaları sürükleyip bırak
- "Commit changes"

**Yükleme yöntemi 2 — Komut satırı (terminale aşinaysan):**
```bash
git init
git add .
git commit -m "İlk kurulum"
git branch -M main
git remote add origin https://github.com/KULLANICI_ADI/kgen-monitor.git
git push -u origin main
```

### 3. GitHub Secrets ekle (KRİTİK)

**Settings → Secrets and variables → Actions → New repository secret**

Her birini ayrı ayrı ekle:

| Secret Adı | Değer | Açıklama |
|---|---|---|
| `TG_TOKEN` | `1234567890:ABCdef...` | BotFather'ın verdiği token |
| `TG_CHAT` | `123456789` | userinfobot'un verdiği chat ID |
| `POS_ENTRY` | `0.1663` | Pozisyonunun ortalama girişi |
| `POS_QTY` | `39120` | Token miktarı |
| `POS_MARGIN` | `2840.67` | Margin (USDT) |
| `POS_TARGET` | `0.25` | Hedef fiyat |
| `ALARMS` | `[]` | (Opsiyonel) JSON alarm listesi - aşağıda örnek |

**Alarmlar nasıl yazılır?** `ALARMS` secret'ına JSON formatında ver:

```json
[
  {"type":"up","target":0.20},
  {"type":"down","target":0.14},
  {"type":"pnl","target":500},
  {"type":"pnl","target":-200}
]
```

- `up` = fiyat hedefin üstüne çıkınca tetiklenir
- `down` = fiyat hedefin altına düşünce
- `pnl` = positive = kâr hedefi, negative = zarar limiti

Boş bırakmak istiyorsan `[]` yaz.

### 4. Workflow'u tetikle

**Actions sekmesi → KGEN Monitor → Run workflow** (manuel ilk test için)

İlk çalışmadan sonra cron her 5 dakikada bir kendi başına çalışır.

### 5. Doğrulama

1. **Actions** sekmesinde yeşil tik ✅ gör
2. Telegram'da botundan ilk mesaj gelmeli (4 saatlik rapor saati değilse, ilk rapor saatini bekleyeceksin — ama anlık alarmlar/likidasyonlar derhal çalışıyor)
3. Manuel test için bir alarm koyabilirsin (örn. mevcut fiyatın 0.0001 üstüne `up` tipi alarm)

---

## 🔧 POZİSYON DEĞİŞTİRDİĞİNDE

**Settings → Secrets and variables → Actions** içinden:
- `POS_ENTRY`, `POS_QTY`, `POS_MARGIN`, `POS_TARGET` secret'larından hangisi değiştiyse "Update" tıkla, yeni değeri yapıştır.
- 30 saniyelik iş.

DCA yaptıysan: yeni ortalamayı `POS_ENTRY`, yeni toplam miktarı `POS_QTY`, eklenen marjini de `POS_MARGIN`'e ekle.

---

## ⚠️ DİKKAT EDİLECEK NOKTALAR

**1. Repo MUTLAKA private olmalı.**
Bot token'ı kodda görünür değil (Secrets'ta), ama git geçmişinde hata yapma riski varsa private gelirse risk azalır.

**2. GitHub Actions ücretsiz limiti:**
- Public repo: sınırsız
- Private repo: ayda 2000 dakika ücretsiz
- Bu script ~30 saniyede bitiyor, günde 288 çalışma = 144 dk/gün, ayda **~4300 dakika**.

⚠️ **Private repo'da limiti aşar!** İki seçenek:

**A) Public repo yap** ama Secrets'ta token sakladığın için yine güvenli (Secrets açık görünmez). Pozisyon bilgilerini görenler olabilir ama trade için aksiyon alamazlar.

**B) Cron sıklığını düşür** — `*/5` yerine `*/10` (her 10dk) yap. Anomali yakalama biraz gecikir ama ayda ~2150 dk olur, limite girer.

**Önerim B:** Public repo + 10 dakika cron. Veya repo private kalabilir, fakat workflow'u şöyle değiştir:

```yaml
# .github/workflows/monitor.yml içinde:
- cron: '*/10 * * * *'  # 10 dakikada bir
```

**3. State dosyası**
Script çalıştığında `state/state.json`'a hangi alarm tetiklendi, hangi likidasyon bildirildi gibi bilgiler yazar ve commit eder. Bu sayede her çalışmada aynı şeyi tekrar bildirmiyor.

**4. CoinGecko rate limit**
Ücretsiz CoinGecko API'si dakikada ~30 istek. Biz dakikada 1'den az istek atıyoruz, sorun yok. Ama çok uzun süre rate-limit'e takılırsa fallback devreye girer (Binance fiyat × 200M tahmini supply).

---

## 🐛 SORUN GİDERME

**Mesaj gelmiyor?**
1. Actions sekmesinde son çalışma başarılı mı? (yeşil tik)
2. Log'da `❌ TG hata` var mı? Token/Chat ID yanlış olabilir
3. Bot ile manuel sohbet açtın mı? (`/start` gönderdin mi)
4. Bot sana ilk mesajı atması için seninle bir kez konuşması gerekir

**4 saatlik rapor saati geçti, gelmedi?**
- Cron 5 dakikada bir çalışıyor, ama saat 07:00:00'da değil 07:00–07:04 arasında bir zamanda çalışır
- Eğer 4 saatlik rapor saati `07:00` ise, ilk 5 dakika içindeki çalışmada gönderilir
- State'te o gün/saat için zaten gönderildiyse tekrar göndermez

**Çok fazla mesaj geliyor / az geliyor?**
- `monitor.py`'nin tepesindeki sabitleri ayarla:
  ```python
  WHALE_HUGE      = 5000     # arttır → daha az mesaj
  LIQ_BIG         = 2000     # arttır → daha az mesaj
  PRICE_SPIKE_PCT = 3.0      # arttır → daha az mesaj
  VOLUME_SPIKE_X  = 3.0      # arttır → daha az mesaj
  ```
- Değişikliği commit et, otomatik aktif olur

---

## 📊 BEKLENEN MESAJ AKIŞI

Tipik bir günde:
- ⚡ 0–3 likidasyon bildirimi
- 🐋 1–4 süper balina bildirimi
- 📊 0–2 anomali bildirimi
- 🕖 5 adet 4 saatlik rapor
- 📅 1 günlük özet
- 🚨 Alarmların tetiklenirse +X tane

**Toplam: günde ortalama 10–15 bildirim.**

---

## 🛑 NASIL DURDURULUR?

**Geçici (workflow'u kapat):**
- Actions → KGEN Monitor → "..." menüsü → Disable workflow

**Tamamen sil:**
- Repo'yu sil veya `.github/workflows/monitor.yml` dosyasını sil/commit
