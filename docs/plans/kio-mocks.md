# Plan — Göreve uygun çalışan sahte KIO'lar

> Durum: **1–3. adımlar bitti (KIO2, KIO3, KIO4, KIO6 uyum, KIO7, KIO10, KIO11)** · Hazırlanma: 2026-10-28
> Dayanak: DoA Annex 1 Part B (§1.1, KIO tanımları) ve **D2.6** (§4.1 KIO
> gereksinimleri, §5.2–5.3 use case akışları). İkisi çeliştiğinde D2.6 geçerli:
> **KIO2 hata düzeltmez, yerini bulur**; düzeltmeyi UC-UC1-03'te KIO7 önerir.

## Amaç

KIO2 dışındaki KIO'lar henüz yok ya da uzak sunucuda. `tools/dummy_fleet.py`
her isteğe aynı hazır cevabı verdiği için değerlendirici sahte hataların peşine
düşüyor ve cevaplar uydurma oluyor. Hedef:

- Her sahte KIO **D2.6'daki görevini gerçekten yapar**: çalışma klasöründeki
  kodu okur, izini kaydeder, testi çalıştırır, tarar. Çıktısı girdiye göre değişir.
- **Protokol gerçek KIO'larla aynıdır**: KIO1 sahte ile gerçeği ayırt edemez;
  gerçek KIO gelince yalnızca adres değişir.
- **Model isteğe bağlıdır**: varsayılan kapalı; açıkken yalnızca metin üreten
  işlerde kullanılır.
- **Projeden ayrıdır**: `kio-mocks/` KIO1 kodunu içe aktarmaz; tek bağ HTTP.

## Kapsam

D2.6'nın yazılım mühendisliği rolleri; kabul senaryoları **UC1**'in dört
akışıdır. UC2 (sağlık) kendi mock'larını taşıyor
(`../../ai4sweng-uc2`), UC3'ün HLS/FPGA araç zinciri ve KIO10'un gerçek
donanımı burada **simüle edilir**: aynı girdi ve çıktı biçimi, gerçek iş yerine
açıkça "simulated" işaretli sonuç.

## Her KIO ne yapacak (D2.6'ya göre)

Girdi her zaman KIO1'in mesajıdır: `task`, `capability`,
`data.repository.path`, `data.target.entry_point`, `data.upstream`
(önceki adımların çıktıları), `data.arguments`. Çıktı `summary`, `verdict`,
gerekirse `hitl_required` ve göreve özgü alanlar.

| KIO | Yetenekler | Gerçekten yapılan (modelsiz) | D2.6 |
|---|---|---|---|
| KIO2 | `bug_localization`, `diagnosis`, `replay`, `trace_alignment` | Python giriş noktasını ya da başarısız testi `sys.settrace` ile **izini kaydederek** çalıştırır (`trace_ref`). İstisnadan geriye doğru **dinamik dilim** çıkarır, hatalı satırı ve tanımlama-kullanma zincirini verir. `replay` kaydı adım adım açar; `trace_alignment` iki kaydı hizalayıp ilk ayrıştıkları noktayı bulur. **Düzeltme önermez.** | FR-KIO2-01…07 |
| KIO3 | `requirements_engineering`, `user_stories`, `acceptance_criteria` | Doğal dil isteği cümle ve niyete ayırır: işlevsel/işlevsel olmayan gereksinimler (FR-/NFR- kimlikli), "As a … I want … so that …" hikâyeleri, Given/When/Then kriterleri, varsayımlar ve kısıtlar, yapılandırılmış iş kaydı (öncelik, etiket, story point), Markdown dışa aktarma. | FR-KIO3-01…08, UC-UC1-01 |
| KIO4 | `architecture_design`, `component_design` | Yukarıdan KIO3 gereksinimlerini alır; bileşenleri ve bağımlılıklarını çıkarır, **gereksinim ↔ bileşen izlenebilirliği** (iki yönlü), arayüzler, karar gerekçeleri, Mermaid diyagramı ve **iskelet kod** (dosya listesi ya da çıktı klasörüne yazılmış). Mevcut kodda modül ve import grafiğinden mimariyi çıkarır. | FR-KIO4-01…10 |
| KIO5 | `synthetic_data_generation` | Şemadan tohumlu tablo verisi; eksik değerleri zaman/mekânda enterpolasyon; **ağ trafiği akışları** (iyi huylu ve saldırı etiketli); üretim raporu ve dağılım ölçütleri. | FR-KIO5-01…06 |
| KIO6 | `data_validation`, `quality_analysis` | Keşifsel veri analizi (meta veri), doğal dil görevinden **soyut hazırlama hattı**, hattın doğrulanması, atomik araçlarla yürütme (yinelenen kayıt, eksik değer, normalleştirme), hazırlanan verinin doğrulanması, **model birim testi** (basit bir modelin veriye uyması), rapor. UC-UC1-02'de **gereksinim → kod uyumunu** doğrular. | FR-KIO6-01…10 |
| KIO7 | `code_generation`, `code_understanding` | `code_understanding`: `ast` ile modül, fonksiyon ve sınıf özeti, karmaşıklık, olası hatalar. `code_generation`: KIO4 iskeletinden ya da görevden kod ve **test takımı** (FR-KIO7-11), çıktı klasörüne; KIO2'nin bulgusundan **düzeltme yaması önerisi** (UC-UC1-03). Modelsiz hâlde kurallı ve sınırlı; model açıkken gerçek üretim. | FR-KIO7-05…07, 11 |
| KIO8 | `build`, `deployment`, `devops`, `packaging` | Hedef ortama göre araç zinciri yapılandırması (Python sürümü, platform), derleme (`compileall`), paketleme (`dist/`), çapraz hedefler için **simüle** yapı, rapor; yapılandırma sorunu varsa `hitl_required`. | FR-KIO8-01…04 |
| KIO9 | `responsible_ai`, `compliance_review` | Kod tabanının **hash ile bütünlük doğrulaması**, kurallı uyumluluk modeli (GDPR/KVKK: kişisel veri desenleri, kayda düşen kişisel veri, saklama süresi, onay), uyarı ve önerili rapor; `pass`/`warn`/`block`. Gerçek KIO9 gibi **iş arayüzüyle** (`accepted` + `GET /jobs/{id}`) ve gecikmeyle cevap verir. | FR-KIO9-01, 04, 05, 07 |
| KIO10 | `tinyml`, `energy_efficiency` | `energy_efficiency`: testleri ya da giriş noktasını ölçerek çalıştırır (CPU süresi → enerji tahmini, alternatiflerin karşılaştırması; UC-UC1-03). `tinyml`: hedef cihaz kataloğuna (ESP32, STM32, nRF52…) göre bellek, enerji, süre kısıtı kontrolü, uymazsa uygun cihaza **geri düşme**, firmware iskeleti (simüle). | FR-KIO10-01…05 |
| KIO11 | `test_automation`, `validation` | Koddan **test senaryosu üretimi** (`ast` ile fonksiyonlar, sınır değerleri), önceliklendirme, çalıştırılabilir pytest betikleri, çalıştırma ve sonuç; basit **bulanık test (fuzz)** ile hatayı doğrulama (UC-UC1-03 "confirm failure"); `validation`: kabul kriterlerini testlerle eşleştirir. | FR-KIO11-01…07 |
| KIO12 | `cybersecurity_validation` | Kaynak kodda zafiyet taraması (`eval`/`exec`, `shell=True`, gömülü sır, birleştirilmiş SQL, zayıf hash, güvensiz deserialize), API uç noktalarından **güvenlik test senaryoları**, saldırı senaryosu analizi (SQLi, XSS…), yapılandırılmış rapor. | FR-KIO12-01…05, 09 |
| KIO13 | `developer_training`, `onboarding` | Kullanıcının alan kavramlarından (ontolojik öğeler) **prompt şablonları**, alıştırmalar, prompt varyantlarının karşılaştırılması için ölçütler, adım adım kılavuz; `onboarding`: depodan başlangıç rehberi. | FR-KIO13-01…12 |

## Protokol

- Hepsi: `GET /health`, `GET /manifest`, `GET /tasks` (yetenekler ve
  desteklenmeyenler), `GET /schema`, `POST /execute`. KIO2 gerçeğinin kuralları:
  her zaman HTTP 200, bilinmeyen yetenekte `status: error` ve açıklama,
  `workflow_id`/`step_id` geri yansıtılır, iş bir zaman bütçesi içinde biter.
- KIO9 (ve uzun süren her iş): BITNET §3.5 iş arayüzü — `POST` → `accepted` +
  `job_id`, `GET /jobs/{id}` → `success` / `needs_clarification` / `failure`.
- KIO12'nin gerçeği bir model sohbet API'si; sahte KIO12 KIO protokolünü konuşur.

## Klasör

```
kio-mocks/                 ← depoda ama KIO1'den bağımsız
  README.md                ← tek komutla başlatma, her KIO'nun ne yaptığı
  kio_mocks/
    protocol.py            ← uç noktalar, cevap biçimi, hata kuralları
    jobs.py                ← iş arayüzü
    workspace.py           ← çalışma klasöründe güvenli okuma, alt süreç, zaman bütçesi
    tracing.py             ← KIO2 iz kaydı (sys.settrace) ve dilimleme
    llm.py                 ← isteğe bağlı model (KIO_MOCKS_LLM)
    kio2.py … kio13.py     ← her KIO'nun işi
    fleet.py               ← hepsini 81NN portlarında başlatır
  tests/
```

Yalnızca standart kütüphane hedefleniyor (pytest, KIO11 için çalışma
klasöründe ya da sistemde aranır).

## Uygulama adımları

Her adım tek commit; testler geçer; KIO1 ile canlı denenir.

| # | İş | Kabul |
|---|---|---|
| 1 ✅ | İskelet: protokol, iş arayüzü, çalışma klasörü, filo; uygunluk testleri | 12 KIO ayağa kalkar, KIO1 hepsini "ready" görür; bilinmeyen yetenek açık hatayla döner |
| 2 ✅ | UC-UC1-03 zinciri: KIO11 (test, fuzz), KIO2 (iz, dilim, yer bulma), KIO7 (anlama, düzeltme önerisi), KIO10 (enerji) | Gerçek bir hatalı fonksiyonda KIO2 doğru dosya:satırı verir, KIO7 yamayı önerir, KIO11 düzeltmeden sonra yeşil der |
| 3 ✅ | UC-UC1-01/02: KIO3, KIO4, KIO7 (üretim), KIO6 (uyum) | Bir özellik isteği gereksinim → mimari → iskelet → uyum zincirinden geçer |
| 4 | Güvenlik ve uyum: KIO12, KIO9 (iş arayüzü) | KIO9 `block` dediğinde politika kapısı dağıtımı durdurur |
| 5 | KIO8, KIO5, KIO6 (veri), KIO10 (TinyML), KIO13 | UC-UC1-04 uçtan uca; KIO5'in ürettiğini KIO6 doğrular |
| 6 | İsteğe bağlı model | Model açıkken KIO3/KIO4/KIO7/KIO13 metni zenginleşir; kapalıyken her şey çalışır |
| 7 | KIO1 tarafı: `start.py --demo`, konsolda "demo" etiketi, belgeler | Temiz kurulumda tek komutla 12 KIO |

**Canlı (2026-10-28, openrouter/free):** "Testleri çalıştır, başarısız olan testin
hatasının yerini bul, bir düzeltme öner ve enerji etkisini ölç" → KIO11 (4/5,
`shop/pricing.py:25`) → KIO2 (`total_quantity = 0`, `items = []`) → KIO7 (koruma,
kopyada 5/5) → KIO10 (−%20 J), 61 sn. Not: KIO1'in kendi `Pytest` komut agent'ı
açıkken planlayıcı onu seçti ve KIO1 deposunun testlerini çalıştırdı; demo
yapılandırmasında (7. adım) kapalı olmalı.

**3. adım (2026-10-28):** KIO3 isteği FR/NFR, hikâye ve eşiğin iki yanındaki
kriterlere ayırıyor; "hızlı" gibi ölçülemeyen sözcükleri tahmin etmek yerine
soruyor. KIO4 her kavrama bir servis, her kaliteye bir bileşen veriyor ve iki
yönlü izlenebilirlik kuruyor; var olan kodda import grafiğinden mimariyi ve
döngüleri çıkarıyor. KIO7 iskeletin her modülüne izlediği gereksinimleri
yazıyor. KIO6 gereksinimi kodda buluyor, taslağı uygulamadan ayırıyor ve
kriterlerdeki sayıları koddaki sabitlerle ve testlerin argümanlarıyla
karşılaştırıyor. Örnek dükkânda "5 ve üzeri ürüne %5" isteğine karşı: eşikler
kodda var, ama 4 ve 9 adetlik sınırları test eden yok. KIO6'nın veri
doğrulaması 5. adımda.

**Canlı (2026-10-28, openrouter/free):** "Customers want bulk discounts: 5 or more
items get 5%, 10 or more items get 10%. Write the requirements, design it and
check the code against them." → KIO3 (1 FR, 5 kriter) → KIO4 → KIO6: eşikler
`shop/pricing.py`'de, AC-03 (4 adet) ve AC-05 (9 adet) test edilmiyor; 76 sn.
Öğrenilenler: KIO6'nın yapılandırmadaki tanımı uyum kontrolünden söz etmeyince
planlayıcı KIO7'yi seçti (`config.example.json` güncellendi; kendi
`config.json`'ınızda da güncellenmeli). Planlayıcı görevi yeniden yazıyor
("Write natural-language requirements for…", "Include acceptance criteria in
the format…", "customers buying 5 or more items get…", oran önce eşik sonra);
KIO3 bunları artık tanıyor. Bir denemede model plan yazarken token sınırına
takıldı; tekrarında geçti. Türkçe istekte cevap modeli iskeletin kuralları
"uyguladığını" söyledi; KIO6'nın çıktısı doğru (yalnızca taslak).

`tools/dummy_agent.py`, KIO1'in kendi protokol testlerinde kullanıldığı için
kalır. `examples/uc1/agents.py`'nin gerçek iş mantığı buraya taşınır.

## Kararlar

1. **Alan:** D2.6'nın yazılım mühendisliği rolleri, UC1 senaryolarıyla
   sınanarak. UC2'nin sağlık davranışları kendi deposunda.
2. **Model:** varsayılan kapalı. Açıkken OpenAI uyumlu ya da Ollama uç noktası
   (`KIO_MOCKS_LLM=ollama:qwen2.5:3b` ya da `openai:<url>:<model>`); KIO1'in
   ayarına bağlı değil, çünkü sahte KIO'lar ayrı süreçtir.
3. **Gerçek API'ler:** KIO2 (`/execute`, `/jobs`), KIO9 (BITNET §3.5), KIO12
   (sohbet API'si). Diğerleri KIO1–KIO10 protokolünü konuşur.
