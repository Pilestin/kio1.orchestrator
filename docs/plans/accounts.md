# Kullanıcı hesapları ve kullanım başına ücretlendirme

> Durum: **taslak** · Hazırlanma: 2026-10-28
>
> Şimdilik (deneme/geliştirme aşaması) tek bir konsol şifresi var:
> `core/console_auth.py`, `python start.py --set-password`. Hesaplar bu plana göre sonra.

## Amaç

KIO1 bir sunucuda herkese açılabilsin: her kişinin kendi hesabı, kendi
sohbetleri ve kendi harcaması olsun. Model çağrıları kullanıcının adına
LiteLLM üzerinden gitsin; LiteLLM her kullanıcının harcamasını tutsun, bütçe
dolunca dursun. Ücretlendirme LiteLLM'in harcama kayıtlarından yapılır.

## Bugün

- Sohbetler, çalışmalar, adımlar, dosyalar `kio1.db`'de (ya da PostgreSQL'de);
  hiçbirinde sahip yok.
- Yönetim uçları (agent ekleme, ayarlar) tek bir `KIO1_REGISTRY_TOKEN` ister;
  iş çalıştırma, geçmiş, dosya yükleme, canlı akış hiçbir şey istemez.
- Model çağrıları tek bir sağlayıcı anahtarıyla yapılır.
- `start.py` yalnızca `127.0.0.1`'i dinler.

## Tasarım

### Hesaplar

- `users` tablosu: kimlik, e-posta, parola özeti (`hashlib.scrypt`, ek bağımlılık
  yok), rol (`admin` | `user`), durum (etkin/askıda), oluşturulma zamanı,
  LiteLLM anahtarının kimliği.
- İlk yönetici: `start.py` ilk açılışta kurulum ekranında oluşturur (bugünkü
  kurulum diyaloğunun bir adımı). `KIO1_REGISTRY_TOKEN` yalnızca bu ilk adım ve
  kurtarma için kalır.
- Kayıt: yönetici davet eder (bağlantı) ya da açık kayıt — ayarla seçilir.

### Oturum

- Giriş: e-posta + parola → sunucu tarafında `user_sessions` kaydı, tarayıcıya
  `HttpOnly; Secure; SameSite=Lax` çerez. Canlı akış (SSE) da çerezle çalışır.
- Programdan erişim için kişisel API anahtarları (`Authorization: Bearer`),
  özeti saklanır, kullanıcı iptal edebilir.
- Her istek bir ara katmandan geçer: `/health` ve giriş sayfası dışında oturum
  ya da anahtar yoksa 401. Yerel adres dışında dinlerken kapatılamaz.
- Değiştiren isteklerde CSRF koruması (çerez + başlık), girişte hız sınırı.

### Sahiplik

- `conversations`, `sessions`, dosyalar bir `user_id` taşır; her okuma ve
  yazma onunla süzülür. Yönetici herkesinkini görebilir.
- Agent'lar ve ayarlar sistem genelidir, yalnızca yönetici değiştirir.
  Komut (cli) agent'ları sunucuda çalıştığı için varsayılan olarak yalnızca
  yöneticiye açık.

### LiteLLM ve ücretlendirme

- KIO1'e `litellm` sağlayıcısı eklenir (OpenAI uyumlu uç nokta).
- Kullanıcı oluşturulunca KIO1, LiteLLM'de ona bir sanal anahtar açar
  (`/key/generate`, `user_id`, `max_budget`, izinli modeller). Anahtar KIO1'de
  şifreli saklanır.
- Bir çalışmanın bütün model çağrıları (planlama, değerlendirme, cevap, `llm`
  agent'ları) o kullanıcının anahtarıyla gider; LiteLLM harcamayı ona yazar.
- Bütçe dolunca LiteLLM reddeder; KIO1 bunu "bütçeniz doldu" diye gösterir.
- Konsolda kullanıcıya kendi harcaması, yöneticiye herkesinki (LiteLLM'in
  `/spend` uçlarından).
- KIO'ların kendi maliyetleri (ileride) aynı kullanıcıya etiketlenir.

### Güvenlik ayrıntıları

- HTTPS: önünde Caddy/nginx; belgeye kısa bir bölüm.
- Denetim kaydı: giriş, anahtar oluşturma, agent değişikliği kim tarafından.
- Parola sıfırlama: ilk sürümde yönetici sıfırlar; e-postayla sıfırlama sonra.

## Adımlar

| # | İş | Kabul |
|---|---|---|
| 1 ✅ | Ara katman ve tek şifreyle giriş (2026-10-28); yerel adres dışında şifresiz başlamaz | `--host 0.0.0.0` ile açılınca girişsiz hiçbir veri dönmez |
| 2 | `users`, parola, oturum çerezi, giriş/çıkış ekranı, ilk yönetici | İki kullanıcı ayrı tarayıcılarda girer |
| 3 | Sahiplik: sohbet/çalışma/dosya `user_id`; eski kayıtlar yöneticiye | A, B'nin sohbetini göremez (testli) |
| 4 | Roller: agent/ayar/cli yalnızca yönetici; kullanıcı yönetimi sayfası | Kullanıcı rolü yönetim uçlarında 403 alır |
| 5 | `litellm` sağlayıcısı, kullanıcı başına sanal anahtar ve bütçe | Bir çalışmanın harcaması LiteLLM'de doğru kullanıcıya yazılır |
| 6 | Harcama görünümü, bütçe dolunca anlaşılır hata, kişisel API anahtarları | Bütçesi biten kullanıcı net bir mesaj görür |

## Açık sorular

1. Başka ortamda yazmaya başladığınız kod nerede, neyi kapsıyor? Aynı
   tasarımı tekrar yazmak yerine onu almak gerekebilir.
2. LiteLLM proxy'niz hazır mı (adres, yönetici anahtarı)? Bütçe kullanıcı
   başına mı, takım başına mı?
3. Kayıt açık mı olacak, davetle mi?
4. Veritabanı: tek sunucu için SQLite yeter; çok kullanıcıda PostgreSQL
   (zaten destekli) önerilir.
