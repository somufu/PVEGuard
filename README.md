# PVEGuard: Proxmox VE Snapshot ve Kaynak Yönetim Aracı

PVEGuard, Proxmox Sanallaştırma Ortamı (PVE) kullanıcıları için geliştirilmiş, web tabanlı bir yönetim ve optimizasyon aracıdır. Temel amacı, sanal makine (VM) ve konteyner (CT) snapshot'larının yönetimini kolaylaştırmak, kaynak kullanımını izlemek ve optimizasyon önerileri sunarak PVE ortamınızın daha verimli çalışmasına yardımcı olmaktır.

## ✨ Temel Özellikler

*   **Snapshot Yönetimi:**
    *   Tüm VM ve CT snapshot'larını merkezi bir arayüzden listeleme.
    *   Tarihe, kaynak adına veya snapshot adına göre sıralama ve filtreleme.
    *   Belirli bir tarihten eski snapshot'ları kolayca görüntüleme.
    *   "Sahipsiz Eski Kök", "Durdurulmuş VM'lerdeki Eski Snapshot'lar" gibi özel durumları tespit etme.
    *   Seçili snapshot'ları toplu olarak silme.
*   **VM Performans İzleme ve Optimizasyon (QEMU):**
    *   Çalışan QEMU VM'lerinin anlık, ortalama ve maksimum CPU/RAM kullanımını takip etme.
    *   Disk G/Ç (Okuma/Yazma Bps) ve Ağ trafiği (Gelen/Giden Bps) metriklerini izleme.
    *   Detaylı geçmiş performans grafikleri (saatlik, günlük, haftalık vb.).
    *   Yetersiz kullanılan VM'ler için vCPU ve RAM azaltma önerileri alma.
    *   Uzun süredir kapalı olan "potansiyel atıl" VM'leri tespit etme.
    *   Temel VM eylemleri (Başlat, Kapat, Durdur, Yeniden Başlat).
*   **Kullanıcı Dostu Arayüz:**
    *   AJAX tabanlı işlemlerle hızlı ve akıcı kullanıcı deneyimi.
    *   Anlık geri bildirimler ve uyarılar.

## 🚀 Kurulum ve Kullanım

### Gereksinimler
*   Python 3.x
*   Flask ve diğer Python kütüphaneleri (bkz. `requirements.txt` )
*   Proxmox VE sunucusuna API erişimi

### Kurulum Adımları

1.  **Projeyi Klonlayın veya İndirin:**
    ```bash
    git clone https://github.com/somufu/PVEGuard_v0.3.git
    cd PVEGuard_v0.3
    ```

2.  **(Önerilen) Sanal Ortam Oluşturun ve Aktif Edin:**
    ```bash
    python -m venv .venv
    # Windows için:
    .\.venv\Scripts\activate
    # Linux/macOS için:
    # source .venv/bin/activate
    ```

3.  **Bağımlılıkları Yükleyin:**
    [Eğer `requirements.txt` dosyanız varsa, aşağıdaki komutu kullanın. Yoksa, `app.py` dosyasındaki importlara göre manuel kurulum yapın: `pip install Flask Flask-WTF python-dotenv proxmoxer requests`]
    ```bash
    pip install Flask Flask-WTF python-dotenv proxmoxer requests
    # veya
    # pip install -r requirements.txt
    ```

4.  **`.env` Dosyasını Yapılandırın:**
    Proje ana dizininde `.env.example` dosyasını kopyalayarak `.env` adında yeni bir dosya oluşturun veya doğrudan `.env` dosyası oluşturun. Ardından aşağıdaki bilgileri kendi Proxmox VE sunucu bilgilerinizle güncelleyin:
    ```dotenv
    PROXMOX_HOST=sizin_proxmox_ip_veya_hostadiniz
    PROXMOX_USER=kullanici_adiniz@pve # veya @pam vb.
    PROXMOX_PASSWORD=parolaniz
    PROXMOX_VERIFY_SSL=False # veya True (SSL sertifikanız geçerliyse)
    APP_SECRET_KEY=cok_guclu_ve_rastgele_bir_anahtar_uretmek_icin_os_urandom(24)_kullanin
    ```
    **Not:** `APP_SECRET_KEY` için Python'da `import os; print(os.urandom(24))` komutunu çalıştırarak rastgele bir anahtar üretebilirsiniz.

5.  **Uygulamayı Çalıştırın:**
    ```bash
    python app.py
    ```

6.  **Erişim:**
    Web tarayıcınızda `http://127.0.0.1:5000` (veya sunucunuzun IP adresiyle `http://<sunucu_ipsi>:5000`) adresine gidin.

## 🛠️ Kullanılan Teknolojiler

*   **Backend:** Python, Flask
*   **Proxmox API İletişimi:** `proxmoxer` kütüphanesi
*   **Frontend:** HTML, CSS, JavaScript
*   **JavaScript Kütüphaneleri:** jQuery, Peity (sparklines için), Chart.js (detaylı grafikler için)
*   **Veri Yönetimi:** Proxmox RRD (Round Robin Database) ve uygulama içi bellek tabanlı geçmiş verileri.

## 🖼️ Ekran Görüntüleri

**

## 🤝 Katkıda Bulunma

Katkılarınız her zaman beklerim! Lütfen bir "issue" açarak veya bir "pull request" göndererek katkıda bulunun.

1.  Projeyi Forklayın.
2.  Yeni bir özellik dalı oluşturun (`git checkout -b ozellik/yeni-bir-ozellik`).
3.  Değişikliklerinizi commit edin (`git commit -am 'Yeni bir özellik eklendi'`).
4.  Dalınızı push edin (`git push origin ozellik/yeni-bir-ozellik`).
5.  Bir Pull Request oluşturun.

## 📜 Lisans

Bu proje Copyright (c) 2025 Muhammed Furkan SOYLU Lisansı altında lisanslanmıştır. Daha fazla bilgi için `LICENSE` dosyasına bakın.

## 💡 Gelecek Planları

*   Daha gelişmiş analitikler ve raporlama.
*   Proxmox Backup Server (PBS) entegrasyonu.
*   Kullanıcı tarafından yapılandırılabilen otomatik snapshot temizleme politikaları.
*   VM ve snapshot etiketleme/gruplama özellikleri.
*   Belirli olaylar için bildirim (alerting) mekanizmaları.
*   Kullanıcı rolleri ve yetkilendirme.
*   Konteyner (LXC) kaynak kullanımı izleme ve optimizasyon önerileri.
*   Grafik modalında özel zaman aralığı seçimi ve veri kaynağı şeffaflığı.

---

Muhammed Furkan SOYLU/Ankara Üniversitesi Siber Güvenlik MYO/Siber Güvenlik Analistliği ve Operatörlüğü
