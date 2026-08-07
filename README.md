# 🚢 Maritime NVR CCTV Snapshot Agent — Technical Documentation & Architecture Manual

Dokumentasi resmi dan manual arsitektur teknis untuk **Dynamic NVR RTSP Snapshot Agent**. Sistem ini dirancang khusus untuk lingkungan maritim (kapal laut) dengan konektivitas VSAT terbatas, menjamin pengiriman foto pemantauan CCTV jernih murni secara konsisten **< 10.0 KB per snapshot** tanpa mengurangi resolusi statik 360x270 piksel.

---

## 📌 Daftar Isi
1. [Ikhtisar Sistem & Masalah yang Diselesaikan](#1-ikhtisar-sistem--masalah-yang-diselesaikan)
2. [Arsitektur & Alur Kerja Utama](#2-arsitektur--alur-kerja-utama)
3. [Detail Metode Kompresi Adaptif (Tingkat Lanjut)](#3-detail-metode-kompresi-adaptif-tingkat-lanjut)
   - [A. Mengapa Algoritma Quality-Based (-q) Mentok?](#a-mengapa-algoritma-quality-based--q-mentok)
   - [B. Preprocessing Entropi OpenCV](#b-preprocessing-entropi-opencv)
   - [C. Target-Size Encoding cwebp](#c-target-size-encoding-cwebp)
   - [D. Rincian Level Kompresi & Parameter Matematis](#d-rincian-level-kompresi--parameter-matematis-d-sigma-k)
   - [E. Pembagian Peran & Alur Kerja Eksekusi (OpenCV vs cwebp)](#e-pembagian-peran--alur-kerja-eksekusi-opencv-vs-cwebp)
4. [Mekanisme Offline-First & Store-and-Forward (SQLite WAL)](#4-mekanisme-offline-first--store-and-forward-sqlite-wal)
5. [Skema Konfigurasi Dinamis (config.json)](#5-skema-konfigurasi-dinamis-configjson)
6. [Format Data & Kontrak API Server Darat](#6-format-data--kontrak-api-server-darat)
7. [Panduan Instalasi & Depresiasi Dependencies](#7-panduan-instalasi--depresiasi-dependencies)
8. [Panduan Pengoperasian Sistem](#8-panduan-pengoperasian-sistem)
9. [Troubleshooting & FAQ (Tingkat Dasar s.d. Expert)](#9-troubleshooting--faq-tingkat-dasar-sd-expert)

---

## 1. Ikhtisar Sistem & Masalah yang Diselesaikan

Di lingkungan kapal maritim, transmisi data gambar melalui satelit VSAT menghadapi 3 tantangan utama:
1. **Bandwidth VSAT Sangat Terbatas & Mahal**: Mengirim foto HD berukuran ~500 KB per menit akan menghabiskan kuota giga-byte dalam hitungan hari.
2. **Koneksi Sering Terputus (*Blank Spot*)**: Kapal sering berada di luar jangkauan sinyal satelit saat cuaca buruk atau berpindah zona coverage.
3. **Variasi Kompleksitas Visual Kamera**: Kamera outdoor (melihat pepohonan, jalan raya, atau riak air laut yang kompleks) menghasilkan entropi visual tinggi yang membuat encoder WebP standar gagal menembus batas < 2.0 KB pada resolusi statik.

### Solusi Sistem:
- **Dual-Storage Strategy**: Foto HD Asli (400-500 KB) tetap disimpan di penyimpanan lokal kapal untuk keperluan audit/bukti investigasi, sedangkan foto terkompresi (< 2.0 KB) dikirim ke server darat.
- **Adaptive Entropy Preprocessing + Target-Size Search**: Menghapus noise frekuensi-tinggi (daun, riak air, bintik aspal) menggunakan filter matematis edge-preserving tanpa mengubah resolusi statik 360x270.
- **Offline Store-and-Forward**: Menggunakan database SQLite berteknologi WAL (*Write-Ahead Logging*) agar data tidak korup saat kapal kehilangan daya secara mendadak.

---

## 2. Arsitektur & Alur Kerja Utama

```
                     ┌─────────────────────────────────────────┐
                     │          CCTV NVR (Hikvision/Dahua)     │
                     └────────────────────┬────────────────────┘
                                          │ RTSP Stream (Mainstream HD)
                                          ▼
                     ┌─────────────────────────────────────────┐
                     │          1. RTSP Capture (FFmpeg)       │
                     └────────────────────┬────────────────────┘
                                          │ Frame HD (.jpg ~500KB)
                                          ▼
                     ┌─────────────────────────────────────────┐
                     │    2. Local Storage (snapshots_hd/)    │
                     └────────────────────┬────────────────────┘
                                          │
                                          ▼
 ┌─────────────────────────────────────────────────────────────────────────┐
 │ 3. Adaptive Compression Pipeline (snapshotcompress.py)                  │
 │                                                                         │
 │  ┌─────────────────────────┐    ┌────────────────────────────────────┐  │
 │  │ OpenCV Preprocessing    │ ──>│ cwebp Target-Size Binary Search    │  │
 │  │ (Bilateral/Median/Bits) │    │ (-size 1950 -m 6 -pass 10 -sns 100)│  │
 │  └─────────────────────────┘    └────────────────────────────────────┘  │
 └────────────────────────────────────┬────────────────────────────────────┘
                                      │ WebP Ringan (< 2.0 KB)
                                      ▼
                     ┌─────────────────────────────────────────┐
                     │ 4. Offline Queue (SQLite WAL queue.db)  │
                     └────────────────────┬────────────────────┘
                                          │
                   ┌──────────────────────┴──────────────────────┐
                   │                                             │
      🔴 Server Offline / Blank Spot               🟢 Server Online (HTTP 201)
                   │                                             │
                   ▼                                             ▼
        [Uploader Tidur 0% CPU]                  [Kuras Antrean & Post ISO Data]
        [Data Aman di SQLite]                    [Hapus WebP Lokal Setelah Sukses]
```

---

## 3. Detail Metode Kompresi Adaptif (Tingkat Lanjut)

### A. Mengapa Algoritma Quality-Based (`-q`) Mentok?
Parameter `-q` (Quality) pada encoder gambar bekerja dengan skala kualitas relatif terhadap entropi konten. Pada gambar kompleks (pepohonan, riak laut, aspal), pada kualitas `q=0` (terendah), encoder tetap harus menyimpan garis-garis tepi noise frekuensi tinggi. Akibatnya, file tertahan di **2.11 KB – 2.24 KB** (di atas target < 2.0 KB).

### B. Preprocessing Entropi OpenCV
Untuk menembus batas fisik tersebut tanpa mengurangi resolusi (tetap 360x270), sistem menerapkan **filter reduksi entropi matematis**:

1. **Bilateral Filter (`cv2.bilateralFilter`)**:
   - Filter *edge-preserving noise reduction*.
   - Meredam variasi warna pada area bertekstur (daun, air) namun **menjaga ketajaman garis tepi/siluet objek utama** (kapal, kendaraan, orang).
2. **Median Blur (`cv2.medianBlur`)**:
   - Meredam *speckle noise* / bintik-bintik acak tanpa memberikan efek kabur (*blur*) berlebih seperti Gaussian blur biasa.
3. **Kuantisasi Kedalaman Warna (Color Bit-Depth Reduction)**:
   - Mengurangi bit per channel warna (misal 8-bit $\rightarrow$ 6-bit / 5-bit) menggunakan bitwise shift: `((img >> shift) << shift)`.
   - Mengurangi variasi gradien halus yang tidak terlihat oleh mata manusia, menekan overhead header WebP hingga 30%.

### C. Target-Size Encoding `cwebp`
Sistem menggunakan `cwebp` (Google WebP Encoder) dengan flag optimisasi khusus:
- `-size 1950`: Memaksa encoder melakukan *binary-search* internal untuk menyesuaikan kualitas gambar secara otomatis agar ukuran file **pasti $\le 1950$ bytes** (memberikan margin aman dari batas keras 2048 bytes).
- `-m 6`: Metode eksplorasi kompresi paling lambat & paling teliti (efisiensi tertinggi).
- `-pass 10`: Menjalankan 10 putaran analisis optimisasi ukuran.
- `-sns 100`: *Spatial Noise Shaping* maksimal (meredistribusi bit ke area terpenting).
- `-f 100` & `-sharpness 0`: Strength filter deblocking maksimal untuk menghaluskan blok DCT.
- `-pre 4`: Preprocessing khusus untuk membuang entropi bit warna terakhir sebelum konversi YUV.

### D. Rincian Level Kompresi & Parameter Matematis (`d`, `sigma`, `k`)

Proses kompresi bersifat **adaptif bertahap (Level 0 $\rightarrow$ Level 4)**. Kamera sederhana (indoor/anjungan) akan berhenti di Level 0 (kualitas jernih 100%), sedangkan kamera kompleks (outdoor) akan otomatis naik level hingga ukuran file $\le 2048$ bytes.

#### 📐 Penjelasan Parameter Matematis OpenCV:

1. **`d` (Diameter Jangkauan Piksel / Pixel Neighborhood Diameter)**:
   - **Formula**: `d = 5 + (level * 2)` $\rightarrow$ Lvl 1: `d=7`, Lvl 2: `d=9`.
   - **Arti Matematis**: Menentukan luas radius area piksel tetangga yang diperhitungkan saat meleburkan warna.
   - **Fungsi Praktis**: `d=9` berarti OpenCV mengambil area matriks $9 \times 9$ piksel di sekitar titik pusat untuk meratakan variasi warna.

2. **`sigmaColor` & `sigmaSpace` (Toleransi Perbedaan Warna & Jarak Spasial)**:
   - **Formula**: `sigmaColor = 25 + (level * 20)` & `sigmaSpace = 25 + (level * 20)` $\rightarrow$ Lvl 1: `45`, Lvl 2: `65`.
   - **Arti Matematis**: Menentukan "ambang batas batas perbedaan warna" di mana perataan warna diizinkan terjadi.
   - **Fungsi Praktis (Edge-Preserving)**:
     - Jika perbedaan warna dua piksel $< 65$ (misal: gradien hijau daun atau bintik aspal), maka warnanya **diratakan/dilebur**.
     - Jika perbedaan warna $> 65$ (misal: batas tepi antara mobil putih dan jalanan hitam), maka garis batas tersebut **SAMA SEKALI TIDAK DI-BLUR** (garis tepi/siluet objek tetap tajam 100%).

3. **`k` (Kernel Size Median Blur)**:
   - **Formula**: `k = 3` (pada Lvl 2 & 3) dan `k = 5` (pada Lvl 4).
   - **Arti Matematis**: Ukuran matriks persegi $k \times k$ tempat algoritma mengurutkan piksel dan mengambil **nilai tengah (median)**.
   - **Fungsi Praktis**: Sangat ampuh menghancurkan *speckle noise* / bintik-bintik acak kecil (bintik dedaunan, kerikil aspal, atau percikan air) tanpa menimbulkan efek "gambar kabut" seperti Gaussian blur.

4. **Kuantisasi Warna / Bit-Depth Reduction (8-bit vs 6-bit vs 5-bit)**:
   - **8-bit Warna (Asli Standar / True Color)**: Setiap channel warna (Red, Green, Blue) memiliki $2^8 = 256$ tingkat variasi. Total kombinasi warna = $256 \times 256 \times 256 = 16,7\text{ Juta Warna}$. Encoder WebP harus menyimpan tabel dari jutaan variasi warna mikro ini, sehingga memakan overhead bytes.
   - **6-bit Warna (Level 3 Preprocessing)**: Variasi warna dikurangi menjadi $2^6 = 64$ tingkat per channel (`shift=2`). Total kombinasi warna = $64 \times 64 \times 64 = 262.144\text{ Warna}$. Mereduksi variasi gradien tipis yang tidak terlihat oleh mata manusia, menekan ukuran file WebP $\sim 20\%$.
   - **5-bit Warna (Level 4 Preprocessing)**: Variasi warna dikurangi menjadi $2^5 = 32$ tingkat per channel (`shift=3`). Total kombinasi warna = $32 \times 32 \times 32 = 32.768\text{ Warna}$. Sangat agresif menekan entropi warna, membuat ukuran file WebP anjlok ke **1.1 – 1.3 KB** untuk scene ekstrem.
   - **Formula Bitwise Shift**: `out = ((out >> shift) << shift)` $\rightarrow$ membuang bit-bit warna bernoise paling kanan lalu mengembalikan skala piksel.

#### 📊 Tabel Ringkasan Parameter per Level:

| Level        | `d` (Diameter) | `sigmaColor` & `sigmaSpace` | `k` (Median Kernel) | Bits Warna       | Hasil Ukuran WebP                   |                                   |
| :------------:| :--------------:| :---------------------------:| :-------------------:| :----------------:| :-----------------------------------:| -----------------------------------|
| **Lvl 0**    | - (Polos)      | -                           | -                   | 8-bit (Asli)     | **1.2 – 1.7 KB** ✅                  |                                   |
| **Lvl 1**    | `7`            | `45`                        | -                   | 8-bit (Asli)     | **1.7 – 1.9 KB** ✅                  |                                   |
| **Lvl 2**    | `9`            | `65`                        | `3` ($3 \times 3$)  | 8-bit (Asli)     | **1.8 – 1.96 KB** ✅ *(Cam1 Lolos!)* |                                   |
| **Lvl 3**    | `11`           | `85`                        | `3` ($3 \times 3$)  | 6-bit (64 warna) | **1.4 – 1.6 KB** ✅                  |                                   |
| **Fallback** | `13`           | `105`                       | `5` ($5 \times 5$)  | 5-bit            | Forced `-size 1700/1400/1100 B`     | **< 1.5 KB** ✅ *(Guarantee 100%)* |

#### 🛡️ Penjelasan Angka Fallback (`1700`, `1400`, `1100` Bytes):

Angka-angka ini adalah **Target Ukuran File dalam satuan Bytes** (bukan KiloBytes/KB) yang dipaksa ke `cwebp` jika pada Level 4 file gambar masih berada di atas batas 2048 bytes (2.0 KB):
- **Target Normal (`SAFE_TARGET_BYTES = 1950`)**: Target default `cwebp` memasang ukuran $\le 1.950$ Bytes ($\approx 1,90$ KB).
- **Fallback Stage 1 (`-size 1700`)**: Memaksa `cwebp` mengejar target $\le 1.700$ Bytes ($\approx 1,66$ KB).
- **Fallback Stage 2 (`-size 1400`)**: Jika 1700 meleset, paksa target $\le 1.400$ Bytes ($\approx 1,36$ KB).
- **Fallback Stage 3 (`-size 1100`)**: Benteng pertahanan paling akhir $\le 1.100$ Bytes ($\approx 1,07$ KB).

> **Tujuan**: Menjamin **100% (Safety Net)** bahwa tidak ada satu pun snapshot dari kamera manapun (se-ekstrem apapun cuaca/pepohonan/riak airnya) yang akan pernah lolos melebihi batas 2048 Bytes (2.0 KB).

### E. Pembagian Peran & Alur Kerja Eksekusi (OpenCV vs cwebp)

Untuk memperjelas cara kerja sistem di setiap level, berikut adalah **pembagian tugas utama**:
- 🎨 **OpenCV** = **Pre-processor (Tukang Bersih Noise)**: Merapikan & mereduksi variasi entropi gambar sebelum di-compress.
- ⚡ **`cwebp`** = **Encoder (Tukang Kompres WebP)**: Mengubah gambar menjadi file `.webp` dengan target `-size 1950` bytes.

`cwebp` **bekerja di SELURUH Level (Level 0 s.d. Fallback)**. Perbedaannya terletak pada tingkat kebersihan gambar yang disiapkan oleh OpenCV:

```text
Level 0:
[OpenCV] Resize 360x270 (Polos)  ───> [cwebp] Encode (-size 1950) ───> Ukuran < 2KB? ──> (Selesai/Lanjut?)

Level 1:
[OpenCV] Lvl 0 + Bilateral       ───> [cwebp] Encode (-size 1950) ───> Ukuran < 2KB? ──> (Selesai/Lanjut?)

Level 2:
[OpenCV] Lvl 1 + Median Blur     ───> [cwebp] Encode (-size 1950) ───> Ukuran < 2KB? ──> (✅ Cam1 Lolos!)

Level 3:
[OpenCV] Lvl 2 + 6-bit Warna     ───> [cwebp] Encode (-size 1950) ───> Ukuran < 2KB? ──> (Selesai/Lanjut?)

Level 4:
[OpenCV] Lvl 2 + 5-bit Warna     ───> [cwebp] Encode (-size 1950) ───> Ukuran < 2KB? ──> (Selesai/Lanjut?)

Fallback (Jika Lvl 4 belum cukup):
[OpenCV] Pakai Gambar Lvl 4      ───> [cwebp] Encode Paksa (-size 1700/1400/1100) ───> (✅ Pasti Lolos 100%)
```

---

## 4. Mekanisme Offline-First & Store-and-Forward (SQLite WAL)

Sistem menggunakan database SQLite `queue.db` dengan arsitektur **Store-and-Forward**:

### Fitur Utama:
1. **WAL Mode (`PRAGMA journal_mode=WAL`)**:
   - Memungkinkan operasi *read* dan *write* berjalan bersamaan tanpa saling mengunci (*lock*).
   - Melindungi database dari korupsi data akibat listrik kapal mati mendadak (*abrupt power loss*).
2. **Flag Koneksi Server (`server_connected`)**:
   - Jika koneksi ke server darat terputus / HTTP Gagal (401/403/500/Timeout), flag koneksi otomatis di-clear (`False`).
   - Thread Uploader akan **TIDUR TOTAL** (0% HTTP POST Spam & 0% konsumsi CPU berlebih).
   - Thread `ConnChecker` memantau kesehatan koneksi secara berkala (misal per 60/300 detik).
3. **Auto-Pruning Queue**:
   - Jika antrean menumpuk lebih dari 5.000 file akibat hilang sinyal berbulan-bulan, sistem otomatis menghapus 500 antrean tertua untuk menjaga kapasitas disk mini-PC kapal.

---

## 5. Skema Konfigurasi Dinamis (`config.json`)

Seluruh sistem dikendalikan secara eksternal melalui file `config.json`. Anda tidak perlu mengubah kode Python saat berpindah NVR, menambah kamera, atau mengubah server endpoint.

```json
{
  "nvr": {
    "ip": "192.168.7.184",
    "port": 554,
    "user": "admin",
    "pass": "ptspts2023",
    "brand": "hikvision"
  },
  "server": {
    "base_url": "http://192.168.7.129:3000/api/v1"
  },
  "agent": {
    "snapshot_interval_sec": 60,
    "connection_check_interval_sec": 60,
    "mock_mode": false,
    "mock_video": "cctv.kapal.mp4"
  },
  "cameras": [
    {
      "channel": 1,
      "name": "cam1",
      "token": "9ycS912ABqUAkCcdjC9NGCSXqMk6GB8q"
    },
    {
      "channel": 10,
      "name": "cam10",
      "token": "ZmmCq5G9RnvMRbopdTasZUaQ1dV5SR8c"
    }
  ]
}
```

### Penjelasan Parameter:
- `nvr.brand`: Mendukung `"hikvision"`, `"hilook"`, `"dahua"`, `"uniview"`, `"xmeye"`, `"generic"`. Sistem akan otomatis membentuk URL RTSP yang sesuai.
- `server.base_url`: URL dasar Gateway API Server Darat (contoh: `http://192.168.7.129:3000/api/v1` atau `https://vmsapi.domain.com/api/v1`).
- `agent.mock_mode`: Set `true` untuk simulasi lokal menggunakan file video MP4 tanpa NVR fisik.
- `cameras[].token`: `cameraToken` unik 32-karakter per kamera yang diterbitkan oleh database server darat.

---

## 6. Format Data & Kontrak API Server Darat

### Endpoint Uploader:
`POST {base_url}/cctv/worker/cameras/{cameraToken}/snapshots`

### Request Header & Body (Multipart Form-Data):
- **Headers**: `Content-Type: multipart/form-data`
- **Form Data**:
  1. `file`: File gambar WebP terkompresi (`image/webp`, ukuran < 2.0 KB).
  2. `captured_at`: Timestamp ISO 8601 UTC String (`YYYY-MM-DDTHH:MM:SZ`, contoh: `"2026-08-07T04:17:39Z"`).

### Contoh Respons Server (HTTP 201 Created):
```json
{
  "status": 201,
  "message": "Snapshot uploaded successfully",
  "data": {
    "vessel_id": 28,
    "camera_name": "cam1",
    "date": "2026-08-07",
    "captured_at": "2026-08-07T04:17:39.000Z",
    "file_path": "snapshots/28/9ycS912ABqUAkCcdjC9NGCSXqMk6GB8q/2026-08-07/04:17:39.webp"
  }
}
```

---

## 7. Panduan Instalasi & Depresiasi Dependencies

Sistem membutuhkan beberapa *library* sistem dan Python:

### 1. Install Sistem Package (Linux / Raspberry Pi OS):
```bash
sudo apt-get update
sudo apt-get install -y ffmpeg webp
```

### 2. Install Python Dependencies:
```bash
pip install opencv-python-headless numpy requests Pillow --break-system-packages
```

---

## 8. Panduan Pengoperasian Sistem

### A. Uji Coba Simulasi Lokal (Tanpa Kirim Server):
```bash
python3 test.py
```
*(Hasil foto HD dan WebP akan disimpan di subfolder `test_output/` untuk verifikasi).*

### B. Menjalankan Sistem Produksi Utama:
```bash
python3 snapshotcompress.py
```

### C. Menjalankan sebagai Service Background (Systemd Service):
Buat file `/etc/systemd/system/cctv-snapshot.service`:
```ini
[Unit]
Description=Maritime NVR CCTV Snapshot Agent
After=network.target

[Service]
Type=simple
User=Mirage
WorkingDirectory=/home/Mirage/NVR-KP/CCTV-SNAPSHOT-Vsemar
ExecStart=/usr/bin/python3 /home/Mirage/NVR-KP/CCTV-SNAPSHOT-Vsemar/snapshotcompress.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
Aktifkan service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable cctv-snapshot
sudo systemctl start cctv-snapshot
```

---

## 9. Troubleshooting & FAQ (Tingkat Dasar s.d. Expert)

#### Q1: Mengapa muncul error `HTTP 403 Cross-site POST form submissions are forbidden`?
> **Jawab (Expert)**: Ini terjadi jika request POST dikirimkan ke port UI SvelteKit (port `8082`) tanpa header `Origin`. Pastikan `base_url` di `config.json` mengarah langsung ke **API Gateway Backend (Port `3000/api/v1`)**, bukan port UI frontend.

#### Q2: Mengapa muncul error `HTTP 401 Invalid camera token`?
> **Jawab**: Token kamera pada `config.json` tidak cocok dengan token yang terdaftar di database server darat untuk kapal tersebut. Pastikan `cameraToken` 32-karakter di-copy dengan benar dari portal admin VMS.

#### Q3: Apakah OpenCV menggunakan Machine Learning / AI yang memberatkan CPU Raspberry Pi?
> **Jawab**: **Sama sekali TIDAK.** OpenCV di sini hanya menjalankan operasi matematika citra tradisional (`bilateralFilter` & `medianBlur`). Tidak ada Neural Network/Model AI yang berjalan, sehingga penggunaan CPU sangat rendah (< 5% per cycle) dan sangat lancar di Raspberry Pi 3/4/5.

#### Q4: Bagaimana jika NVR CCTV di kapal menggunakan brand Dahua atau Generic?
> **Jawab**: Cukup ubah `"brand": "dahua"` pada `config.json`. Sistem akan otomatis membentuk URL RTSP `/cam/realmonitor?channel={N}&subtype=0`.

#### Q5: Mengapa snapshot lokal HD tetap berukuran ~500 KB tetapi di server < 2.0 KB?
> **Jawab**: Ini adalah fitur *Dual-Storage*. Foto HD asli disimpan di disk lokal kapal sebagai bukti hukum / audit jika terjadi insiden di kapal, sedangkan foto WebP < 2.0 KB dikirim ke server darat untuk menghemat kuota satelit VSAT.

---
*Created & Maintained for Maritime Fleet Management System (VMS).*
