# KIO1 Yol Haritası — Profesyonel ve Herkesin Kurabileceği Bir Agentic Geliştirme Ortamı

> Durum: taslak · Başlangıç: 2026-10-22 · Kural: **asla `git push` yok**; commit'leri proje sahibi yapar.

## Hedef

KIO1, yazılımcı olmayan birinin bile şunları yapabildiği bir ortam olmalı:

1. **Tek adımda başlatmak**: çift tıkla ya da tek komutla açılır, tarayıcıda kurulum sihirbazı karşılar.
2. **Kendi agent'larını oluşturmak**: kod yazmadan bir LLM agent'ı, bir web API'si, bir MCP sunucusu ya da bir komut eklemek.
3. **Sohbet ederek kullanmak**: isteğini yazar, KIO1 planı gösterir, onay alır, agent'ları çalıştırır, sonuçları değerlendirir ve anlaşılır bir cevap verir.
4. **Ne olduğunu görmek**: hangi agent'ın ne yaptığını, neden seçildiğini, ne ürettiğini canlı izlemek.

## Bugünkü durum (2026-10-22 incelemesi)

| Alan | Durum |
| --- | --- |
| Hedef türleri (`kio`, `http`, `cli`, `mcp`) | Hazır |
| Canlı dizin, health probe, failover, scoreboard | Hazır |
| Plan → DAG yürütme, policy gate'leri | Hazır |
| Web konsolu, agent CRUD, ayarlar API'si | Hazır; kayıtlar `config.json`'da |
| Session Manager (PostgreSQL) | Hazır; Docker gerektiriyor |
| Agent'ın elle aktif/pasif yapılması | **Yok** |
| `http` hedefleri için argüman şeması | **Yok** (`WeatherForecast` bu yüzden çalışmıyor) |
| Sonuçları değerlendirme, yeniden planlama, son cevap | **Yok**: akış tek yönlü |
| Sohbet bağlamı (önceki turları hatırlama) | Konsolda yok |
| Kodsuz agent oluşturma | Yok; yalnızca JSON alanları |
| Kolay kurulum | Yok; venv, Docker ve `config.json` bilgisi gerekiyor |

Yerel model ölçümü (yalnızca CPU, ~4 GB boş RAM, ~2.600 token sistem prompt'u):
`ministral-3:3b` doğru plan, 30–60 sn · `qwen3.5:4b` en iyi plan, ~250 sn.
Sonuç: kalite yeterli, **gecikme asıl sorun**. Kısa katalog, model önbelleği ve
görev başına model seçimi gerekli.

---

## Adımlar

Her adım kendi içinde tamamlanır, testleri geçer ve kullanılabilir bir sonuç bırakır.
Boyut: **S** ≈ yarım gün, **M** ≈ 1–2 gün, **L** ≈ 3+ gün.

### Adım 1 — Kolay başlangıç ve temel düzeltmeler  `M`  ✅ tamamlandı (2026-10-22)

Amaç: "indir, çalıştır, tarayıcı açılsın".

- [x] **Tek komutla başlatma**: `start.cmd` (Windows) ve `start.sh` (macOS/Linux) şunları yapar: Python'u kontrol eder, `.venv`'i kurar ya da günceller, bağımlılıkları yükler, KIO1'i başlatır ve tarayıcıyı açar. `python start.py` de aynı işi yapar.
- [x] **Docker'sız varsayılan**: Session Manager'ın SQLite modu, gömülü (aynı süreçte) çalışır. PostgreSQL ve Docker isteğe bağlı "sunucu modu" olarak kalır.
- [x] **Kişisel yapılandırmayı ayırmak**: `config.example.json` depoda kalır. Gerçek `config.json` ilk açılışta oluşturulur ve `.gitignore`'a eklenir; kişisel yollar ve test agent'ları depoya girmez. API anahtarları `.env` dosyasında tutulur.
- [x] **Agent `enabled` alanı**: katalog yalnızca *etkin ve erişilebilir* hedefleri gösterir; konsolda aç/kapa düğmesi.
- [x] **`http` argüman şeması**: `"arguments": {"latitude": {"type": "number", "required": true}}`; katalogda gösterilir, `{arguments.latitude}` yer tutucusu çözülür.
- [x] **Ollama ayarları**: `think: false` varsayılan; planlama zaman aşımı (`request_timeout`) örnekte 300 sn; sistem prompt'u değişmediği sürece önbellekte kalsın diye katalog sırası sabit tutulur.
- [x] **Güvenli varsayılanlar**: `compose.yaml`'daki `KIO1_REGISTRY_TOKEN: admin` kaldırılır; token ilk açılışta rastgele üretilir ve konsolda bir kez gösterilir.

Kabul: Temiz bir Windows makinede `start.cmd` çift tıklanınca konsol açılır, Docker gerekmez.

Doğrulama: `.venv` olmadan `start.py` sıfırdan kurdu ve başlattı; "41.01, 28.97'de hava nasıl?" isteğinde model `WeatherForecast` argümanlarını kendisi doldurdu, Open-Meteo'dan gerçek veri geldi ve sonuç `data/kio1.db`'ye kaydedildi (toplam ~96 sn, çoğu CPU'da planlama). URL'e giren değerlerin kodlanmaması hatası da bu adımda düzeltildi.

### Adım 2 — İlk açılış sihirbazı  `M`

- [x] **Sağlayıcı ve model seçimi (Ayarlar sekmesi)** — 2026-10-22: Ollama / OpenRouter / OpenAI / Anthropic arasında yeniden başlatmadan geçiş; aranabilir model listesi (OpenRouter'ın 468 modeli anahtarsız listeleniyor); API anahtarı önce denenip yalnızca çalışırsa git dışındaki `.env`'e yazılıyor. `.env` artık okunuyor ve git dışında.
- [x] **Canlı izleme** — 2026-10-22: çalışan bir session'da modelin yazdığı plan token token, adımlar başladı/bitti (çıktı önizlemesiyle) ve KIO1'in o çalışmaya ait log satırları SSE ile konsolda (`GET /workflow/{id}/events`). Yerel modelin prompt okuma süresi sayaçla gösteriliyor. Olaylar şimdilik bellekte (son 50 çalışma); veritabanına yazılması Adım 3–4'te.
- [ ] **1. Model sağlayıcı (sihirbaz)**: "Bilgisayarımda çalışsın (Ollama)" / "OpenAI" / "Anthropic" / "OpenRouter" ilk açılışta adım adım. Ollama seçilirse kurulu olup olmadığı ve çalışıp çalışmadığı denetlenir, kurulu modeller listelenir, donanıma göre model önerilir (RAM/GPU tespiti) ve konsoldan `ollama pull` ilerleme çubuğuyla yapılır. Bulut seçilirse API anahtarı istenir ve bir test çağrısı yapılır.
- [ ] **2. Çalışma klasörü**: KIO1'in üzerinde çalışacağı proje klasörü seçilir.
- [ ] **3. Hazır agent paketleri**: "Dosya okuma/yazma (MCP filesystem)", "Git geçmişi", "Kod üretici", "Test çalıştırıcı", "Web'den sayfa getir" kutularla seçilir.
- [ ] **4. Deneme**: örnek bir istekle uçtan uca çalıştırma ve "Hazırsınız" ekranı.
- [ ] **Sistem durumu sayfası**: model, veritabanı, agent'lar ve disk durumu tek sayfada, sorun varsa sade dille ne yapılacağı yazar.

### Adım 3 — Veri katmanı: her şey veritabanında  `M`

- [x] **Agent deposu** — 2026-10-22: `AgentStore` arayüzü; SQLite (varsayılan, `data/kio1.db`) ve `config.json` (`KIO1_AGENT_STORE=file`). *(Kalan: PostgreSQL agent deposu; sürümlü migration dosyaları.)*
- [ ] Yeni tablolar:
  - `agents`: tanım, `enabled`, kaynak, oluşturan, sürüm.
  - `agent_versions`: değişiklik geçmişi, geri alma.
  - `conversations` / `messages`: sohbet geçmişi.
  - `runs` / `iterations`: bir isteğin turları (plan, yürütme, değerlendirme).
  - Mevcut `sessions`, `steps`, `artifacts` ve `lineage` tabloları bunlara bağlanır.
- [x] **Aktarım ve geçmiş** — 2026-10-22: `config.json` agent'ları ilk açılışta bir kez aktarılır; her değişiklik sürüm olarak tutulur, konsoldan geri yüklenir; `GET /registry/export`. Canlı: 18 agent sırasıyla aktarıldı. *(Kalan: içe aktarma; sohbet/tur tabloları.)*
- [ ] "Bu çalıştırmada hangi agent'ın hangi sürümü kullanıldı" kaydı tutulur.

### Adım 4 — Ajan döngüsü: planla → çalıştır → değerlendir → cevapla  `L`

KIO1'in "orkestratör" olarak asıl değeri burada.

- [x] **Netleştirme** — 2026-10-22: zorunlu bir bilgi istekte yoksa planlayıcı plan yerine soru sorar; cevap isteğe eklenip yeniden planlanır, değerlendirici de görür (`orchestration.clarify/max_questions/question_timeout`). Canlı: "Şu an hava nasıl?" → model Türkçe koordinat sordu, konsoldan cevaplandı. *(Kalan: soru-cevapları oturuma kalıcı kaydetmek.)*
- [x] **Plan önizleme ve onay** — 2026-10-22: plan çalışmadan önce gösterilir, "Run plan / Cancel"; `orchestration.approval` = `auto` / `always` / `never`, cevapsız kalan plan süre dolunca iptal. Bkz. `docs/approval.md`. *(Kalan: planı onaydan önce düzenleyebilmek.)*
- [x] **Değerlendirme turu** — 2026-10-22: tüm plan bittikten sonra model sonuçları okur ve `done` ya da `continue` der; `continue`'da yalnızca eksik iş yeniden planlanır (`orchestration.max_rounds`, varsayılan 2). Değerlendirme başarısız olursa çalışma bozulmaz, cevap sonuçlardan yazılır. *(Kalan: `retry_step`, `ask_user`, süre/token bütçeleri.)*
- [x] **Son cevap** — 2026-10-22: isteğin dilinde, kaynak gösteren (`[Deneme]`) cevap; oturuma kaydedilir, konsolda en üstte gösterilir. Bkz. `docs/review.md`.
- [x] **Rol bazlı model seçimi (cevap modeli)** — 2026-10-22: değerlendirme ve cevap ayrı sağlayıcı/modelle yapılabiliyor (`orchestration.review_provider/review_model`, Settings → Answer model). Canlı ölçüm: ministral planlayıp qwen3.5:4b cevaplayınca en yeni commit'ler birebir doğru geldi, uydurma kalmadı (~101 sn, CPU).
- [x] **İnsan onayı gereken hedefler** — 2026-10-22: hedefe `"approval": true` eklenince ona ulaşabilecek her plan (failover adayları dahil) onay bekler; konsolda kilit simgesi. *(Kalan: capability bazında işaretleme.)*
- [ ] **Olay akışı**: her olay (plan, adım başladı/bitti, değerlendirme, soru) tek bir olay veriyolundan SSE ile konsola akar ve veritabanına yazılır. *(Veriyolu ve SSE hazır; kalan: değerlendirme/soru olayları ve kalıcılık.)*
- [ ] **İptal**: çalışan bir istek durdurulabilir; MCP ve HTTP çağrılarına iptal iletilir.
- [ ] **Rol bazlı model seçimi**: planlama, değerlendirme ve son cevap için ayrı model seçilebilir (ör. planlama için yerel model, değerlendirme için bulut model).

### Adım 5 — Sohbet ekranı ve UI/UX  `L`

Düzen: **sol** sohbetler ve projeler · **orta** sohbet · **sağ** canlı çalıştırma paneli.

- [ ] **Sohbet**: mesaj balonları, Markdown ve kod bloğu görünümü, kopyala düğmesi, akan yanıt (streaming), önceki turlara atıf, düzenleyip yeniden gönderme.
- [ ] **Plan kartı**: sohbet içinde, adımlar ve agent ikonlarıyla; onay düğmeleri burada.
- [ ] **Canlı DAG görünümü**: adımlar kutu, bağımlılıklar ok; durum renkleri (bekliyor, çalışıyor, tamam, hata, atlandı); tıklayınca girdi/çıktı, süre, deneme sayısı.
- [ ] **Çıktı görüntüleyiciler**: metin, JSON ağacı, kod, dosya listesi, diff, tablo; üretilen dosyalara bağlantı.
- [ ] **`@agent` ve `/komut`**: `@Pytest testleri çalıştır` belirli bir agent'ı zorlar; `/plan`, `/agents`, `/yeni-agent` kısayolları.
- [ ] **Dosya ekleme**: sohbete dosya sürükle-bırak; dosya çalışma klasörüne kopyalanır ve adımlara `data` olarak geçer.
- [ ] **Anlaşılır hatalar**: teknik hata altta katlanmış durur; üstte "Ne oldu, ne yapabilirsiniz" yazar.
- [ ] Türkçe/İngilizce arayüz, açık/koyu tema, klavye kısayolları, mobil genişlikte kullanılabilirlik.
- [ ] Teknik karar: derleme adımı olmayan hafif bir yapı (yerel dosyalardan Preact + htm ya da Alpine.js); internetsiz çalışmaya devam eder, Node.js gerektirmez.

### Adım 6 — Kodsuz agent oluşturucu  `L`

"Agent Ekle" sihirbazı; her türün kendi formu ve **"Dene"** alanı vardır:

- [x] **LLM agent** — 2026-10-22: `type: llm`; ad, açıklama, talimat, yetenek; isteğe bağlı model. Konsolda form (Agent / JSON). Canlı: şablondan oluşturulan CodeReviewer planlayıcı tarafından seçildi, gerçek hatayı buldu. Bkz. `docs/llm-agents.md`. *(Kalan: agent'ın kendi araç çağırması.)*
- [ ] **Web API**: URL yapıştır → "Dene" → gelen yanıttan sonuç alanına tıklayarak `result_path` seçilir. **OpenAPI/Swagger içe aktarma**: her endpoint otomatik olarak argüman şemasıyla capability olur.
- [ ] **MCP galerisi**: filesystem, git, fetch, GitHub, SQLite gibi yaygın sunucular tek tıkla eklenir; gereken `npx` ya da `uvx` yoksa nasıl kurulacağı gösterilir.
- [ ] **Komut / script**: komut, klasör, izin verilen argümanlar; "bu komut dosya değiştirir mi?" onay ayarı.
- [x] **Şablondan başla** — 2026-10-22: üç hazır şablon (kod inceleyici, gereksinim yazarı, dokümantasyon yazarı); dışa aktarma `GET /registry/export`. *(Kalan: tek agent'ı dosya olarak paylaşma/içe alma.)*
- [ ] **Geliştiriciler için SDK**: `kio1_sdk` ile 10 satırda Python agent'ı yazılır; kendini KIO1'e otomatik kaydeder (manifest + heartbeat).
- [ ] **Agent test alanı**: tek bir agent'ı plan olmadan, verilen girdiyle doğrudan çalıştırıp çıktıyı görme.

### Adım 7 — Profesyonelleştirme  `M–L`

- [ ] **Güvenlik**: kullanıcı girişi ve roller (yönetici, kullanıcı, izleyici); gizli anahtarlar şifreli saklanır ve hiçbir yanıtta gösterilmez; `cli` hedefleri izin listesi ve çalışma klasörü sınırıyla çalışır; LLM çıktısı güvenilmez girdi sayılır.
- [ ] **Paketleme**: `pyproject.toml`, sürüm numarası, `CHANGELOG.md`; `pip install kio1` ve `kio1 up`; Docker imajı.
- [ ] **Kalite**: GitHub Actions'ta lint, typecheck ve test (yalnızca yapılandırma; push sahibine aittir); uçtan uca `live` testleri (dummy fleet + codegen + MCP filesystem + HTTP aracı).
- [ ] **Planlama değerlendirme seti**: örnek istekler ve beklenen agent'larla otomatik puanlama; model ya da prompt değişikliklerinin etkisi ölçülür (2026-10-22'deki ölçüm betiği bunun başlangıcı).
- [ ] **Gözlemlenebilirlik**: mevcut OpenTelemetry yapısına token, maliyet ve gecikme panelleri.
- [ ] **Dokümantasyon**: Türkçe kullanıcı kılavuzu (ekran görüntüleriyle), geliştirici kılavuzu ve API referansı tek bir site altında.

### Adım 8 — Ek özellikler (öncelik sırasıyla)

1. **Projeler/çalışma alanları**: her projenin kendi agent seti, klasörü ve geçmişi olur.
2. **Kayıtlı iş akışları (tarifler)**: başarılı bir planı kaydedip tek tıkla ya da zamanlanmış olarak yeniden çalıştırma.
3. **Git entegrasyonu**: her çalıştırma ayrı bir dalda; değişiklikler diff olarak onaya sunulur; commit kullanıcıdadır, push hiçbir zaman yapılmaz.
4. **KIO1'i MCP sunucusu olarak açmak**: Claude Code, VS Code gibi araçlar KIO1'i ve agent'larını araç olarak kullanabilir.
5. **Proje bilgisi (RAG)**: çalışma klasöründeki kod ve dokümanlar indekslenir; agent'lara ilgili bağlam verilir.
6. **Maliyet ve kota takibi**: sağlayıcı ve agent bazında token ve süre; bütçe aşımında uyarı.
7. **Bildirimler**: uzun çalışmalar bitince ya da onay gerekince masaüstü bildirimi.
8. **A2A (Agent-to-Agent) protokolü desteği**: başka orkestratörlerle birlikte çalışabilirlik.

---

## Açık kararlar

| Karar | Öneri |
| --- | --- |
| Varsayılan veritabanı | SQLite (gömülü); PostgreSQL isteğe bağlı |
| Arayüz teknolojisi | Derleme gerektirmeyen Preact + htm, yerel dosyalardan |
| Session Manager | Varsayılan olarak aynı süreçte; ayrı servis modu korunur |
| Yerel model önerisi | Planlama için `ministral-3:3b` (hızlı); kalite gerekirse bulut model |

## Çalışma kuralları

- Her adım: kod + test + doküman güncellemesi; `pytest` yeşil kalır.
- Commit mesajı önerilir, **commit'i proje sahibi yapar, push yapılmaz**.
- Geriye dönük uyumluluk: mevcut KIO protokolü (`/execute`, mesaj şekli) değişmez.
