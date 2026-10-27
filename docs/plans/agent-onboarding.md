# Plan — Agent eklemeyi kolaylaştırmak ve arayüzü sadeleştirmek

> Durum: **taslak, kararlar bekliyor** · Hazırlanma: 2026-10-27
> Yol haritasında: Adım 6 (kodsuz agent oluşturucu) ve Adım 5'in kalanları.

## Sorun

Bugün bir agent eklemek için kullanıcının şunları bilmesi gerekiyor: hedef
türleri (`kio` / `http` / `cli` / `mcp` / `llm`), "capability" adı, JSON
alanları (`url`, `result_path`, `arguments`), registry token. Targets sayfası
20 satırlık bir tablo; ulaşılamayan her hedef kırmızı bir Windows hata metni
gösteriyor. Settings'te temperature, timeout, system prompt yolu gibi
yalnızca geliştiricinin anlayacağı alanlar var. Proje sahibi bile karmaşık buluyor.

**Hedef:** hiç bilmeyen biri bir agent'ı **2 dakikada**, JSON görmeden ekler,
dener ve sohbette kullanır.

## İlkeler

1. **Önce ne yapmak istediğini sor, sonra türü sen seç.** Kullanıcı "http mi
   mcp mi" diye düşünmez; "bir web adresim var" der.
2. **Görünen alan sayısı en fazla 3.** Gerisi "Gelişmiş" altında katlı.
3. **Kaydetmeden önce dene.** Her formda "Dene" kutusu: bir cümle yaz, agent
   cevap versin. Çalışmayan agent kaydedilmez (zorla kaydetme "Gelişmiş"te).
4. **Jargon yok.** Target → *Agent*, capability → *yapabildiği iş*
   (otomatik üretilir), Run → *Sohbet*.
5. **Sahte KIO'lar birinci sınıf vatandaş.** Demo, test ve değerlendirme
   hep aynı sahte filoyla yapılır.

## Önerilen akış: "Agent ekle"

```
┌ Agent ekle ───────────────────────────────────────────────┐
│ Ne bağlamak istiyorsun?                                   │
│                                                           │
│ [✎ Talimatla bir agent]  [🌐 Bir web adresi / API]        │
│ [🧰 Hazır araçlar]        [⌨ Bilgisayarımdaki bir komut]  │
│                                                           │
│ ya da anlat:  [ "GitHub'daki açık issue'ları listelesin" ]│
│                                         [Taslak çıkar →]  │
│ Geliştirici misin? → Kendi servisini bağla (SDK)          │
└───────────────────────────────────────────────────────────┘
```

| Kart | Kullanıcının girdiği | KIO1'in yaptığı | Bugün |
|---|---|---|---|
| Talimatla agent (`llm`) | Ad, ne yaptığı, talimat | Yetenek adını açıklamadan üretir; isteğe bağlı model | Var (form), "Dene" yok |
| Web adresi (`http`) | URL ya da OpenAPI/Swagger linki | Örnek çağrı yapar; yanıttan sonuç alanını **tıklayarak** seçtirir; OpenAPI'de her endpoint bir iş olur | JSON ile |
| Hazır araçlar (`mcp`) | Galeriden seç (Dosyalar, Git, Web sayfası getir, GitHub, SQLite) | `npx`/`uvx` var mı bakar, yoksa nasıl kurulacağını söyler; araçları kendisi listeler | JSON ile |
| Komut (`cli`) | Komut, klasör | "Dosya değiştirir mi?" sorusu → onay ayarı | JSON ile |
| **Anlat, taslak çıksın** | Bir cümle | LLM yukarıdaki türlerden birini seçip taslak tanım yazar; kullanıcı formda görür, dener, kaydeder | Yok |
| Kendi servisi (SDK) | — | Kopyalanabilir 10 satırlık örnek; servis kendini kaydeder (manifest + heartbeat, `discovery.md`) | Keşif var, SDK yok |

## Arayüz sadeleştirme önerileri

Büyük görsel değişiklik olduğu için **uygulamadan önce onay gerekiyor**. Her
madde ayrı ayrı kabul ya da ret edilebilir.

1. **Sol menü 4 → 3:** Sohbet · Agent'lar · Ayarlar. "How to use" sağ üstte
   `?` simgesi; "Registry token" Ayarlar'ın içine, gerektiğinde zaten soruluyor.
2. **Agent'lar sayfası tablo yerine kartlar:** üstte "Hazır (7)", altta katlı
   "Kapalı (2)" ve "Ulaşılamıyor (11)". Hata metni yalnızca kartın üzerine gelince.
   Tür etiketi (`KIO`, `HTTP`…) küçük bir simgeye döner.
3. **Ayarlar:** görünen tek karar "Model nerede çalışsın + hangi model".
   Temperature, timeout, prompt dosyası, cevap modeli, çalışma klasörü → "Gelişmiş".
4. **Sohbette plan kartı:** tamamlanmış çalışmalarda tek satır
   ("✓ 8 adım · 90 sn ▸"); adımlar tıklayınca. Çalışırken açık.
5. **Üst çubuk:** "openrouter · openrouter/free" ve "connected" çipleri yerine
   tek bir durum noktası; tıklayınca ayrıntı.
6. **Boş sohbet ekranı:** örnek isteklerin yanında, yalnızca **ulaşılabilir**
   agent'lardan üretilen 3 öneri ("KIO2 ile hata bul", "Python sürümünü göster").

## Sahte KIO'lar (demo filo)

> **Ertelendi (2026-10-27).** Kullanıcının belirlediği yön:
> - Her sahte KIO **kendi görevine uygun davranır**, hazır cevap dönmez.
>   Örnek: test KIO'su gerçekten pytest çalıştırır, hata KIO'su traceback'i okur.
>   Bu anlamda "mock" değil, küçük ölçekli gerçek agent'lar. Modelsiz örneği
>   `examples/uc1/agents.py` var.
> - LLM **zorunlu değil**: yalnızca görevin gerektirdiği yerde ve isteğe bağlı.
>   CPU'daki yerel model birden çok agent'ın aynı anda modele gitmesini kaldırmayabilir.
> - Projeye gömülmez, **ayrı bir klasörde** durur. KIO1 ile tek bağı HTTP protokolü olur.
>   Bu protokol gerçek KIO2'deki gibidir (`../../AI4SWENG-KIO2`: `/health`, `/tasks`,
>   `/schema`, `/execute`, `/jobs`; bilmediği işe açık hata döner).
>
> Aşağıdaki maddeler bu yön netleşmeden yazıldı. İşe başlarken yeniden ele alınacak.

`tools/dummy_fleet.py` bugün elle başlatılıyor ve tüm KIO2–KIO13'ü hazır
cevaplarla taklit ediyor (canlı denemede 8 adımlık plan 90 sn'de uçtan uca çalıştı).

- `start.py --demo`: filoyu KIO1 ile birlikte başlatır, kapatınca kapatır.
- Kurulum diyaloğunda tek seçenek: **"Örnek agent'larla dene"**.
- Konsolda bu agent'ların yanında küçük bir **demo** etiketi olur. Cevapları
  sahte olduğu için cevap balonunda da "demo verisi" notu çıkar. Gerçekmiş
  gibi görünmemeli (canlı denemede değerlendirici, sahte bir test hatasını
  düzeltmek için gereksiz ikinci tur açtı).
- Aynı filo uçtan uca testlerde ve planlama değerlendirme setinde kullanılır.

## Uygulama adımları

Her adım tek bir commit; testler geçer; arayüz adımları ekran görüntüsüyle gösterilir.

| # | İş | Boyut | Kabul |
|---|---|---|---|
| 1 | **Agent test alanı:** `POST /registry/agents/try` — kaydedilmemiş bir tanımı tek bir görevle plan olmadan çalıştırır; formlarda "Dene" kutusu | M | Dört tür için test; canlı: CodeReviewer şablonu denenir |
| 2 | **Demo filo:** `start.py --demo`, kurulumda seçenek, "demo" etiketi | S | Temiz başlangıçta 11 demo agent "ready" |
| 3 | **Agent ekle sihirbazı (kartlar):** tür seçimi, 3 alan, "Gelişmiş" katlı; JSON modu "Gelişmiş"e taşınır | L | Ekran görüntüleri; mevcut `targets` testleri geçer |
| 4 | **Web adresi:** URL → örnek çağrı → sonuç alanını tıklayarak seçme; OpenAPI içe aktarma | M | Open-Meteo ve bir OpenAPI örneğiyle canlı |
| 5 | **Hazır araç galerisi (MCP):** 5 sunucu, `npx`/`uvx` kontrolü | M | Dosya sistemi sunucusu tek tıkla eklenip sohbette kullanılır |
| 6 | **"Anlat, taslak çıksın":** cümleden tanım taslağı (LLM, JSON düzeltme turuyla) | M | 5 örnek cümleden 4'ü doğru türü seçer |
| 7 | **Arayüz sadeleştirme** (yukarıdaki 1–6, onaylananlar) | M | Ekran görüntüleri; yeni kullanıcıyla 5 dakikalık deneme |
| 8 | **SDK:** `kio1_sdk` — 10 satırda Python agent'ı, kendini kaydeder | M | Örnek agent sohbette seçilir |

Önerilen sıra: 1 → 2 → 7 → 3 → 4 → 5 → 6 → 8. Test alanı her şeyin temeli;
demo filo ve sadeleştirme ilk izlenimi en hızlı iyileştiren adımlar.

## Kararlar (kullanıcıya sorulacak)

1. Arayüz sadeleştirme maddelerinden (1–6) hangileri uygulansın?
2. "Target" → "Agent" yeniden adlandırması API'ye de yansısın mı?
   Öneri: hayır, yalnızca arayüz; `/registry/agents` zaten "agent" diyor.
3. Arayüz dili: Türkçe/İngilizce seçimi bu planla mı, ayrı adımda mı gelsin?
4. Demo filo varsayılan olarak açık mı gelsin? Öneri: hayır, kurulumda seçilir.

## Riskler

- **"Taslak çıkar" küçük modelle zayıf olabilir.** JSON düzeltme turu var;
  yine de form her zaman elle düzenlenebilir kalmalı.
- **OpenAPI belgeleri büyük olabilir.** Yalnızca seçilen endpoint'ler kataloğa
  girer; katalog büyüdükçe planlama yavaşlar (CPU'da asıl maliyet bu).
- **Sahte cevaplar gerçek sanılabilir.** Demo etiketi ve cevap notu şart.
