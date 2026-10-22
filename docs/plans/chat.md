# Plan — Sohbet ekranı (Yol haritası Adım 5)

> Durum: **taslak onay bekliyor** · Hazırlanma: 2026-10-22
> Taslak: `static/mockups/chat.html` (çalışan KIO1'de `/static/mockups/chat.html`),
> ekran görüntüleri: [açık](chat-mockup/light.png) · [koyu + ayrıntı çekmecesi](chat-mockup/dark-with-details.png) · [telefon](chat-mockup/phone.png)

## Amaç

Run sayfasını, hiç bilmeyen birinin de kullanabileceği bir **sohbete** dönüştürmek:
isteğini yazar, KIO1 planını ve adımlarını kısa bir kartta gösterir, gerekiyorsa
onay ya da cevap ister, sonunda kaynak gösteren bir cevap verir. Sonraki mesaj
öncekinin devamıdır ("şimdi tip ipuçlarını da ekle").

## Kullanıcının koyduğu kurallar (pazarlık dışı)

- **Arayüzde metin minimum.** Bölüm başına en fazla bir kısa satır; açıklama
  dokümana. Önceki tasarım "tonla yazı" yüzünden beğenilmedi.
- **Ekran görüntüsüyle göster**, sonra "bitti" de. `tools/console_screenshots.py`.
- **Commit ve push yok.** Her adımın sonunda commit mesajı ver; commit'i kullanıcı yapar.
- Kullanıcıyla Türkçe konuş; arayüz şimdilik İngilizce.

## Taslaktaki düzen

```
┌ rail ┬ Sohbetler ──────┬ Sohbet ───────────────────────────────┐
│ Run  │ + New chat      │ başlık                 model · [▤]    │
│ Tgts │ Today           │            [kullanıcı balonu]         │
│ Sett │  • aktif sohbet │ K1 KIO1                                │
│ Help │  • …            │ ┌ plan kartı: durum · N adım · süre ┐ │
│      │ Earlier         │ │ ✓ Agent · capability        23 s  │ │
│      │  • …            │ └───────────────────────────────────┘ │
│      │                 │ ┌ cevap balonu [Kaynak] etiketleri ─┐ │
│      │                 │ [kopyala] [yeniden çalıştır]           │
│      │                 │ ┌ onay / soru kutusu (gerekirse) ───┐ │
│      │                 ├───────────────────────────────────────┤
│      │                 │ [ Ask KIO1…   @Agent  @  📎  Send ]    │
└──────┴─────────────────┴───────────────────────────────────────┘
                                    [▤] → sağdan "Run details" çekmecesi
                                         (modelin yazdığı + etkinlik akışı)
```

- Sol menü (Run/Targets/Settings/Help) **kalır**; taslakta odak için çıkarıldı.
  "Run" sekmesi sohbet olur.
- Mevcut canlı panel (model çıktısı + etkinlik) **çekmeceye** taşınır; sohbetin
  kendisinde yalnızca kart, onay/soru ve cevap görünür.
- Onay kutusu ve soru kutusu, bugünkü bileşenlerin (`.approval`, `.question`)
  sohbet içindeki hâlidir.
- Telefonda sohbet listesi gizlenir (bir düğmeyle açılır).

## Kararlar (kullanıcıya sorulacak)

1. **Run sayfası mı değişsin, yeni "Chat" sekmesi mi?** Öneri: Run sayfası sohbet
   olsun; eski tablo görünümü "Run details" çekmecesinde zaten var.
2. **Sohbet bağlamı ne kadar geriye gitsin?** Öneri: son 3 tur (istek + cevap) ve
   o sohbetteki soru-cevaplar, ~4 000 karakterle sınırlı.
3. **Arayüz dili:** şimdilik İngilizce kalsın mı, Türkçe/İngilizce seçimi mi gelsin?
   (Ayrı bir adım olarak önerilir.)

## Uygulama adımları

Her adım kendi içinde biter, testleri geçer ve ayrı bir commit olur.

### 1. Veri: sohbetler ve mesajlar (arka uç)  `M`

- `services/session_manager/sqlite_repository.py` + `repository.py` (memory,
  Postgres): yeni tablolar
  - `conversations(conversation_id, title, created_at, updated_at)`
  - `sessions`'a `conversation_id` sütunu (migration: SQLite `PRAGMA table_info`
    kalıbı `_migrate()` içinde, Postgres `ADD COLUMN IF NOT EXISTS`).
- Sohbet başlığı: ilk isteğin ilk ~60 karakteri.
- Soru-cevapları kalıcı yap: `sessions`'a `clarifications` (JSON) sütunu; şu an
  yalnızca bellekte (`Clarifications` sınıfı, `services/workflow_api/runner.py`).
- **Kabul:** depo testleri (`tests/test_session_manager.py` parametreli
  `repository` fixture'ı memory/sqlite/postgres) yeni alanlarla geçer.

### 2. API  `S`

- `POST /conversations` → yeni sohbet; `GET /conversations?limit=` → liste;
  `GET /conversations/{id}` → sohbet + oturumları (istek, durum, cevap, adımlar).
- `POST /workflow/run` gövdesine isteğe bağlı `conversation_id`; yoksa yeni sohbet açılır.
- `DELETE /conversations/{id}` (yönetici token'ı).
- **Kabul:** `tests/test_conversations.py`.

### 3. Bağlam: devam mesajları  `M`

- Runner'da (`WorkflowRunner._run`) aynı sohbetteki son N oturumun isteği ve
  cevabı, planlayıcıya ve değerlendiriciye `clarified.apply()` gibi bir ekle
  verilir: `"Earlier in this conversation:\n- Q: … A: …"`.
- Hedeflere giden görev metni yine kendi başına yeterli olmalı (planlayıcı
  prompt'undaki kural zaten var: `prompts/kio1_system_prompt_ver2.txt`, Arguments bölümü).
- **Kabul:** sahte modelle test — ikinci mesajın plan isteği ilk cevabı içerir;
  canlı deneme: "average() boş listede çöküyor" → "şimdi tip ipuçları ekle".

### 4. Sohbet ekranı (ön yüz)  `L`

- `static/index.html` Run görünümü → sohbet düzeni (taslaktaki sınıflar
  `app.css`'e taşınır; taslağın `<style>` bloğu referanstır).
- `static/app.js` şu an ~2 000 satır tek dosya. Bu adımda **ES modüllerine**
  bölünmesi önerilir (`<script type="module">`, derleme yok):
  `chat.js`, `live.js` (SSE: `followRun`/`onLiveEvent`), `targets.js`,
  `settings.js`, `setup.js`, `shell.js`. Testler `static/app.js`'i okuyor
  (`tests/test_workflow_api.py`: `fetch(` ve `$("id")` taramaları) — bölünürse
  bu testleri tüm modülleri tarayacak şekilde güncelle.
- Plan kartı: `plan`, `step_started`, `step_finished`, `status` olaylarından
  canlı güncellenir (olaylar: `core/run_events.py`, akış: `GET /workflow/{id}/events`).
- Cevap balonu: mevcut `showAnswer()` (kaynak etiketleri, `**kalın**`, `` `kod` ``).
- Onay/soru: mevcut `showApproval()` / `showQuestion()` + `/waiting`, `/approval`, `/answer`.
- Gönder: Enter gönderir, Shift+Enter yeni satır (bugün Ctrl+Enter).
- **Kabul:** açık/koyu, 1366/1280/420 genişlik ekran görüntüleri; JS hatası yok;
  `tests/test_workflow_api.py` konsol testleri geçer.

### 5. `@agent` ve dosya ekleme  `M`

- `@` yazınca hedef listesi açılır (`GET /registry/agents`); seçilen agent
  istekle birlikte gönderilir ve planlayıcıya "use @X for this" diye eklenir.
- Dosya ekleme: sürükle-bırak → `POST /files` (çalışma klasörüne kopyalanır,
  boyut sınırı) → adımlara `data.attachments` olarak gider.
- **Kabul:** testler + canlı deneme.

### 6. Temizlik  `S`

- Eski Run tablosu/composer kodu kaldırılır, `docs/console.md` güncellenir,
  `static/mockups/` kaldırılabilir ya da referans olarak kalır.

## Riskler

- **Küçük modelle bağlam:** ministral-3:3b uzun bağlamda planı bozabilir. Bağlamı
  kısa tut, cevap modelini ayrı seç (Settings → Answer model).
- **CPU'da süre:** her mesaj 1–2 dk sürebilir; kartın "reading the prompt · 23 s"
  sayacı (bugün `startReadingTimer`) sohbette de görünmeli.
- **Bölünmüş JS:** `type="module"` ile global `$`, `api`, `esc` gibi yardımcılar
  açıkça import edilmeli.
