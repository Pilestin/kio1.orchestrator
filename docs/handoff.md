# Devir notu — KIO1 orchestrator

> 2026-10-22 itibarıyla. Yeni bir oturumun nereden devam edeceğini anlatır.
> Sıradaki iş: [plans/chat.md](plans/chat.md) (sohbet ekranı). Genel plan: [roadmap.md](roadmap.md).

## Kullanıcı ve çalışma kuralları

- Kullanıcıyla **Türkçe** konuş (doğru Türkçe karakterlerle). Kod, kod yorumları ve
  `docs/` (roadmap ve planlar hariç) İngilizce.
- **Asla `git push` yapma. Commit de yapma.** Her adımın sonunda kod bloğu içinde
  bir commit mesajı ver; commit'i kullanıcı yapar. Adımları ayrı commit'lere
  sığacak büyüklükte tut.
- **Arayüzde metin minimum.** Kullanıcı açıklama paragraflarıyla dolu tasarımı
  beğenmedi. Büyük bir görsel değişiklikten önce yön sor; bitince ekran
  görüntüsüyle göster.
- Testleri çalıştır, sonucu dürüstçe söyle. Canlı (gerçek modelle) dene ve ne
  gördüğünü aktar; zayıf sonuçları da söyle.

## Bugünkü durum

811 test geçiyor, 18 atlanıyor (PostgreSQL/NATS gerektirenler).

| Özellik | Dosyalar | Doküman |
|---|---|---|
| Tek komutla başlatma (`start.cmd`/`start.py`), `.env`, SQLite geçmiş | `start.py`, `core/dotenv.py`, `services/session_manager/sqlite_repository.py`, `storage.py` | `usage.md` |
| Hedef türleri: `kio`, `http` (argüman şemalı), `cli`, `mcp`, **`llm`** | `dispatch/agent_registry.py`, `*_transport.py`, `dispatch/llm_transport.py` | `connecting-a-tool.md`, `mcp.md`, `llm-agents.md` |
| Agent'lar veritabanında, sürüm geçmişi, geri yükleme, dışa aktarma | `core/agent_store.py` | `agent-store.md` |
| Canlı izleme (SSE): model çıktısı, adımlar, loglar | `core/run_events.py`, `app.py` (`/workflow/{id}/events`) | `console.md` |
| Sağlayıcı/model seçimi (Ollama, OpenRouter, OpenAI, Anthropic), ayrı cevap modeli | `providers/`, `app.py` (`/settings`) | `configuration.md`, `review.md` |
| Döngü: planla → çalıştır → değerlendir → (yeniden planla) → cevap | `services/workflow_api/runner.py`, `dispatch/review.py`, `prompts/kio1_review_prompt.txt` | `review.md` |
| Plan onayı, netleştirme sorusu | `core/approvals.py`, runner | `approval.md` |
| İlk kurulum: donanım, model önerisi, konsoldan indirme | `core/system_info.py`, `core/model_pull.py` | `setup.md` |
| Konsol (tek sayfa, derlemesiz) | `static/index.html`, `app.js` (~2 000 satır), `app.css` | `console.md` |

## Çalıştırma ve test

```bash
python start.py --no-browser --port 8010     # sunucu (Ollama'yı da başlatır)
python -m pytest -q -p no:cacheprovider      # sistem Python'u (3.11) — test araçları orada
python -m ruff check <dosyalar> --output-format concise
python -m mypy <dosyalar>
python -m black -q <dosyalar>; python -m isort --profile black -q <dosyalar>
node --check static/app.js
```

- `.venv`'i `start.py` kurar ve yalnızca çalışma zamanı bağımlılıklarını içerir;
  testler ve lint **sistem Python'uyla** çalıştırıldı.
- Ruff'ta önceden var olan, bilinçli bırakılmış kalıplar: `TRY004`, `B008`,
  `SIM117`, `RET501`, `PLR1711`. Yeni kodda başka uyarı bırakma.
- **Arayüzü görerek doğrula:**
  `python tools/console_screenshots.py --base http://127.0.0.1:8010 --out <klasör> run:light:1366 targets:dark:1280`
  (Chrome gerekir; sayfa JS hatalarını da yazar.)
- Canlı istek göndermek için Python kullan (`urllib`); Git Bash'te `curl` Türkçe
  karakterli gövdeyi bozuyor.
- Token: `.env` içindeki `KIO1_REGISTRY_TOKEN` (yoksa `data/registry-token`).

## Bu ortamdaki tuzaklar (Windows + Git Bash)

- **MSYS yol dönüşümü:** `//` ile başlayan komut satırı argümanları `/`'ye
  dönüşüyor (bir dosyadaki `// ── bölüm` işaretlerini argüman olarak verince
  bozuldu). Dosya düzenlemelerini Edit aracıyla ya da Write ile yazılmış bir
  Python betiğiyle yap.
- **Heredoc kaçışları:** `<<'EOF'` içindeki Python string'lerinde `\n`, `\\`
  bazen bozuldu. Kaçış içeren kodu heredoc'la değil, Edit/Write ile yaz.
- **Prompt dosyaları sunucu açılırken bir kez okunur** — prompt değişince sunucuyu
  yeniden başlat.
- Konsol sayfası sunucuda dosya değişince yenilenir; CSS/JS içerik özetiyle
  sürümlenir (`?v=`) ve `Cache-Control: no-cache` ile sunulur.

## Makine ve modeller

- 15,2 GB RAM, GPU yok (AMD tümleşik GPU Ollama'da kullanılmıyor). Ollama 0.31.2.
- Kurulu modeller: `ministral-3:3b` (planlama, ~30–70 sn), `qwen3.5:4b`
  (cevap, daha yavaş ama doğru), `qwen2.5:3b`.
- Ölçüm: ministral tek başına cevapta zayıf (yanlış sıralama, uydurma sayı);
  ministral planlayıp qwen3.5:4b cevaplayınca doğru. Kullanıcının ayarında
  cevap modeli şu an **planlama modeliyle aynı** — kurulum diyaloğundaki
  "Use recommended" bunu düzeltir.

## Bilinen açıklar

- Kullanıcının **`WeatherForecast` kaydı bozuk**: içinde TimeLookup tanımı var
  (POST timeapi.io → HTTP 405). Bu, artık veritabanında (`data/kio1.db`).
  Konsoldan `config.example.json`'daki Open-Meteo tanımıyla düzeltilmeli. Kök
  neden (yanlış ada kaydetme) düzeltildi.
- Testte oluşturulan **CodeReviewer** (`llm`) agent'ı kullanıcının veritabanında duruyor.
- PostgreSQL için agent deposu yok (dosyaya düşer). Docker'da `/app/data` bağlanmalı.
- Soru-cevaplar ve canlı olaylar kalıcı değil (olaylar bellekte, son 50 çalışma).
- `main.py` (terminal) değerlendirme/onay/soru döngüsünü kullanmıyor; yalnızca
  plan + çalıştırma.
- KIO2–KIO13 servisleri çalışmıyor; denemek için `python tools/dummy_fleet.py`.
- Konsolda iptal düğmesi, süre/token bütçeleri yok (yol haritası Adım 4 kalanları).

## Mimari hızlı harita

```
start.py ─► uvicorn app:app
app.py           API + konsol; _State: runner, session_client, directory,
                 scoreboard, events, approvals, store, pulls
services/workflow_api/runner.py
                 WorkflowRunner.run → _run (turlar) → _carry_out (plan + onay +
                 execute_workflow) → _review → cevap
dispatch/        registry, directory (yoklama), planner (parse/route),
                 executor (DAG, failover, policy), transports
core/            config_loader/writer, agent_store, run_events, approvals,
                 dotenv, system_info, model_pull, prompt_loader
providers/       ollama, openai, openrouter, anthropic (send_request on_token akışı)
prompts/         kio1_system_prompt_ver2.txt (planlayıcı), kio1_review_prompt.txt
static/          index.html, app.js, app.css, mockups/chat.html
```
