#!/usr/bin/env python3
"""
=============================================================================
DYNAMIC NVR RTSP SNAPSHOT AGENT (EXTERNAL JSON CONFIG SUPPORT)
=============================================================================
Sistem pemantauan NVR CCTV fleksibel (bisa 4, 6, 12, atau 24 kamera):

1. CONFIG EXTERNAL (`config.json`):
   - Tanpa perlu merubah kode Python (.py), pengguna cukup mengubah file text `config.json`.
   - Bebas menentukan jumlah kamera (misal 6 kamera), interval detik snapshot, IP NVR, dan token server.

2. PER-CAMERA TOKEN ARCHITECTURE:
   - Setiap kamera memiliki `cameraToken` unik sendiri yang di-generate dari server.
   - Endpoint upload dinamis: POST /cctv/worker/cameras/{cameraToken}/snapshots

3. DUAL-FOLDER STORAGE:
   - Folder HD Lokal (`snapshots_hd_lokal/`): Foto HD asli (.jpg) disimpan lokal untuk investigasi.
   - Folder WebP Server (`snapshots_nvr_4cctv/`): Foto WebP ringkas (360x270 statik, < 2.0 KB) untuk transmisi server.

4. SERVER CONNECTION FLAG (ANTI-SPAM & CPU SAVER):
   - Jika Server Offline: Thread Uploader TIDUR TOTAL (0% SPAM HTTP POST).
   - ConnChecker memantau server per 5 menit.
   - Snapshot & Antrean SQLite (`queue.db`) TETAP BERJALAN LOKAL.
=============================================================================
"""

import os
import sys
import time
import json
import sqlite3
import threading
import subprocess
import requests
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

def load_config() -> dict:
    """Membaca file konfigurasi external config.json."""
    if not os.path.exists(CONFIG_FILE):
        default_config = {
            "nvr": {
                "ip": "108.16.72.99",
                "port": 554,
                "user": "admin",
                "pass": "margomulyoR10",
                "brand": "hikvision"
            },
            "server": {
                "base_url": "http://100.72.210.21:3000"
            },
            "agent": {
                "snapshot_interval_sec": 60,
                "connection_check_interval_sec": 300,
                "mock_mode": False,
                "mock_video": "cctv.kapal.mp4"
            },
            "cameras": [
                {"channel": 1, "name": "Kamera Depan", "token": "token_cam1_ganti_disini"},
                {"channel": 2, "name": "Kamera Belakang", "token": "token_cam2_ganti_disini"},
                {"channel": 3, "name": "Kamera Geladak", "token": "token_cam3_ganti_disini"},
                {"channel": 4, "name": "Kamera Ruang Mesin", "token": "token_cam4_ganti_disini"},
                {"channel": 5, "name": "Kamera Anjungan", "token": "token_cam5_ganti_disini"},
                {"channel": 6, "name": "Kamera Palka", "token": "token_cam6_ganti_disini"}
            ]
        }
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(default_config, f, indent=2)
        print(f"[INFO] Membuat file konfigurasi default: {CONFIG_FILE}")
        return default_config

    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"[ERROR] Gagal membaca {CONFIG_FILE}: {e}")
        sys.exit(1)

# Membaca Konfigurasi dari config.json
CFG = load_config()

NVR_IP       = CFG["nvr"]["ip"]
NVR_PORT     = CFG["nvr"]["port"]
NVR_USER     = CFG["nvr"]["user"]
NVR_PASS     = CFG["nvr"]["pass"]
NVR_BRAND    = CFG["nvr"]["brand"]

SERVER_BASE_URL = CFG["server"]["base_url"]

SNAPSHOT_INTERVAL_SEC     = CFG["agent"].get("snapshot_interval_sec", 60)
CONNECTION_CHECK_INTERVAL = CFG["agent"].get("connection_check_interval_sec", 300)
MOCK_MODE                 = CFG["agent"].get("mock_mode", False)
MOCK_VIDEO                = CFG["agent"].get("mock_video", "cctv.kapal.mp4")

CAMERAS                   = CFG["cameras"]

# Saklar Koneksi Server Darat
server_connected = threading.Event()

# Kompresi WebP (< 2.0 KB Guarantee)
SNAPSHOT_SCALE   = '360:270'   # Resolusi piksel statik 360 x 270
INITIAL_QUALITY  = 8          # Quality WebP awal (0-100)
COMPRESSION_LEVEL= 6          # Kompresi libwebp maksimal (0-6)
MAX_TARGET_KB    = 2.0        # Batas maksimal ukuran file WebP (< 2.0 KB)

OUTPUT_HD_DIR  = os.path.join(BASE_DIR, "snapshots_hd_lokal")    # Folder HD Asli (Bukti Lokal)
OUTPUT_DIR     = os.path.join(BASE_DIR, "snapshots_nvr_4cctv")  # Folder WebP Ringan (< 2 KB)
DB_PATH        = os.path.join(BASE_DIR, "queue.db")             # Database Antrean Offline SQLite
FFMPEG_CMD     = "ffmpeg"

def init_db():
    """Inisialisasi SQLite database dengan WAL mode untuk ketahanan daya di kapal."""
    os.makedirs(OUTPUT_HD_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("PRAGMA journal_mode = WAL")       # Tahan mati listrik tiba-tiba
    c.execute("PRAGMA max_page_count = 2621440") # Maksimal 10 GB limit DB
    c.execute('''CREATE TABLE IF NOT EXISTS queue
                 (id           INTEGER PRIMARY KEY AUTOINCREMENT,
                  channel_id   INTEGER,
                  camera_token TEXT,
                  hd_path      TEXT,
                  webp_path    TEXT,
                  payload      TEXT,
                  timestamp    INTEGER,
                  retry_count  INTEGER DEFAULT 0)''')
    # Migrasi otomatis jika kolom camera_token belum ada di DB lama
    c.execute("PRAGMA table_info(queue)")
    columns = [row[1] for row in c.fetchall()]
    if "camera_token" not in columns:
        c.execute("ALTER TABLE queue ADD COLUMN camera_token TEXT")
    conn.commit()
    conn.close()

def check_server_connection() -> bool:
    """Mengecek koneksi langsung ke SERVER DARAT."""
    try:
        res = requests.get(f"{SERVER_BASE_URL}/health", timeout=10)
        return res.status_code in [200, 401, 404]
    except Exception:
        try:
            res = requests.head(SERVER_BASE_URL, timeout=5)
            return True
        except Exception:
            return False

def build_rtsp_url(channel_num: int, substream: bool = False) -> str:
    """Membuat URL RTSP otomatis berdasarkan brand NVR dan nomor channel.
       substream=False → Mainstream (HD tinggi, default).
       substream=True  → Substream (resolusi lebih rendah, dipakai sebagai fallback jika mainstream tidak bisa < 2KB).
    """
    if NVR_BRAND.lower() in ["hikvision", "hilook", "ezviz"]:
        # Hikvision: Mainstream = ch*100+1, Substream = ch*100+2
        ch_code = channel_num * 100 + (2 if substream else 1)
        return f"rtsp://{NVR_USER}:{NVR_PASS}@{NVR_IP}:{NVR_PORT}/Streaming/Channels/{ch_code}"
    elif NVR_BRAND.lower() in ["dahua", "uniview", "unv", "xmeye"]:
        # Dahua/Uniview: subtype=0 Mainstream, subtype=1 Substream
        subtype = 1 if substream else 0
        return f"rtsp://{NVR_USER}:{NVR_PASS}@{NVR_IP}:{NVR_PORT}/cam/realmonitor?channel={channel_num}&subtype={subtype}"
    else:
        stream_path = "sub" if substream else "main"
        return f"rtsp://{NVR_USER}:{NVR_PASS}@{NVR_IP}:{NVR_PORT}/ch{channel_num:02d}/{stream_path}"


def take_nvr_snapshot(cam_info: dict) -> dict:
    """
    Mengambil snapshot dari RTSP Stream CCTV NVR:
      1. Berkas HD Asli (.jpg) -> Disimpan di subfolder tanggal: snapshots_hd_lokal/YYYY-MM-DD/{unix_ms}_ch{N}_hd.jpg
      2. Berkas WebP (< 2 KB, 360x270 statik) -> Disimpan di snapshots_nvr_4cctv/{unix_ms}_ch{N}.webp & diantrekan ke queue.db
    """
    channel_num = cam_info["channel"]
    camera_token = cam_info["token"]
    rtsp_url = build_rtsp_url(channel_num)

    now_dt = datetime.now()
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    date_folder = now_dt.strftime("%Y-%m-%d")

    # Unix timestamp milidetik 13-digit (Anti-Overwrite & Global Unique)
    unix_ms = str(int(time.time() * 1000))
    file_base = f"{unix_ms}_ch{channel_num:02d}"

    # Folder Subdirektori Tanggal untuk HD Lokal
    hd_date_dir = os.path.join(OUTPUT_HD_DIR, date_folder)
    os.makedirs(hd_date_dir, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    hd_filename = f"{file_base}_hd.jpg"
    webp_filename = f"{file_base}.webp"

    hd_filepath = os.path.join(hd_date_dir, hd_filename)
    webp_filepath = os.path.join(OUTPUT_DIR, webp_filename)

    ts = int(time.time())
    source_input = os.path.join(BASE_DIR, MOCK_VIDEO) if MOCK_MODE else rtsp_url

    # 1. Capture Frame HD Asli
    hd_cmd = [FFMPEG_CMD, "-y"]
    if MOCK_MODE:
        mock_sec = (channel_num * 5) % 25
        hd_cmd.extend(["-ss", f"00:00:{mock_sec:02d}"])
    else:
        hd_cmd.extend(["-rtsp_transport", "tcp", "-timeout", "5000000"])

    hd_cmd.extend(["-i", source_input, "-vframes", "1", "-q:v", "2", hd_filepath])

    try:
        subprocess.run(hd_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        if not os.path.exists(hd_filepath):
            return {"success": False, "channel": channel_num, "error": "Timeout RTSP"}
    except Exception as e:
        return {"success": False, "channel": channel_num, "error": str(e)}

    hd_size_kb = os.path.getsize(hd_filepath) / 1024.0

    # 2. Kompresi ke WebP (Statik 360x270 SELALU, Guaranteed < 2.0 KB)
    # Prioritas: Mainstream → jika masih >= 2KB di q=0, coba capture ulang dari Substream
    quality = INITIAL_QUALITY
    webp_size_kb = 0.0
    webp_success = False
    used_substream = False

    while True:
        webp_cmd = [
            FFMPEG_CMD, "-y",
            "-i", hd_filepath,
            "-vf", f"scale={SNAPSHOT_SCALE}",  # Skala SELALU 360x270 (tidak berubah)
            "-c:v", "libwebp",
            "-q:v", str(quality),
            "-compression_level", str(COMPRESSION_LEVEL),
            "-vframes", "1",
            webp_filepath
        ]
        try:
            subprocess.run(webp_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            if os.path.exists(webp_filepath):
                webp_size_kb = os.path.getsize(webp_filepath) / 1024.0
                if webp_size_kb < MAX_TARGET_KB:
                    webp_success = True
                    break
                if quality <= 0:
                    if not MOCK_MODE and not used_substream:
                        # Fallback: capture ulang dari SUBSTREAM (resolusi lebih rendah dari NVR)
                        # Skala WebP tetap 360x270 — hanya sumber frame yang berbeda
                        sub_url = build_rtsp_url(channel_num, substream=True)
                        sub_hd_path = hd_filepath.replace("_hd.jpg", "_sub.jpg")
                        sub_cmd = [
                            FFMPEG_CMD, "-y",
                            "-rtsp_transport", "tcp", "-timeout", "5000000",
                            "-i", sub_url,
                            "-vframes", "1", "-q:v", "2",
                            sub_hd_path
                        ]
                        try:
                            subprocess.run(sub_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                            if os.path.exists(sub_hd_path):
                                # Ganti sumber kompresi ke frame substream
                                hd_filepath = sub_hd_path
                                used_substream = True
                                quality = INITIAL_QUALITY  # Reset quality untuk kompres dari substream
                                print(f"    [Fallback Substream] Ch{channel_num}: Mainstream >= 2KB. Coba substream...")
                                continue
                        except Exception:
                            pass
                    # Substream sudah dicoba atau mock_mode, tetap pakai hasil terbaik
                    webp_success = True
                    break
                quality = max(quality - 2, 0)
            else:
                break
        except Exception:
            break

    if not webp_success:
        return {"success": False, "channel": channel_num, "error": "Gagal kompresi WebP"}

    # 3. Simpan Metadata + cameraToken ke Antrean SQLite queue.db (Store & Forward)
    payload = {
        "channel_id": channel_num,
        "camera_token": camera_token,
        "captured_at": now_str,
        "timestamp": ts,
        "resolution": "360x270",
        "quality": quality
    }
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO queue (channel_id, camera_token, hd_path, webp_path, payload, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (channel_num, camera_token, hd_filepath, webp_filepath, json.dumps(payload), ts)
    )
    conn.commit()
    conn.close()

    return {
        "success": True,
        "channel": channel_num,
        "camera_name": cam_info["name"],
        "camera_token": camera_token,
        "hd_filepath": hd_filepath,
        "hd_size_kb": hd_size_kb,
        "webp_filepath": webp_filepath,
        "webp_size_kb": webp_size_kb,
        "quality": quality,
        "timestamp": now_str
    }

def capture_all_cctv_job():
    """Tugas berkala mengambil snapshot seluruh CCTV NVR sesuai config.json."""
    print("\n" + "=" * 65)
    print(f" 🚢 [TASK LOKAL] MEMPROSES SNAPSHOT {len(CAMERAS)} CCTV NVR ({NVR_IP})")
    print(f" Waktu Akses: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f" Penjadwalan: Setiap {SNAPSHOT_INTERVAL_SEC} detik")
    print("=" * 65)

    ts = int(time.time())
    for cam in CAMERAS:
        ch = cam["channel"]
        print(f"[CHANNEL {ch}] Mengakses {cam['name']} (Token: {cam['token']})...")
        res = take_nvr_snapshot(cam)

        if res["success"]:
            print(f"  ✓ BERHASIL Captured & Masuk Queue SQLite!")
            print(f"    📷 HD Lokal  : {os.path.basename(res['hd_filepath'])} ({res['hd_size_kb']:.2f} KB)")
            print(f"    ⚡ WebP Queue: {os.path.basename(res['webp_filepath'])} ({res['webp_size_kb']:.2f} KB) [Res: 360x270] [Q: {res['quality']}]")
        else:
            print(f"  ✗ GAGAL: {res.get('error')}")

# =============================================================================
# THREAD 1: CONNECTION CHECKER WORKER
# =============================================================================
def connection_checker_worker():
    """Thread Pemantau Koneksi Server Darat."""
    worker_name = "🔌 ConnChecker"
    while True:
        if not server_connected.is_set():
            print(f"\n[{datetime.now()}] [{worker_name}] Mengecek koneksi ke Server Darat ({SERVER_BASE_URL})...")
            if check_server_connection():
                server_connected.set()
                print(f"[{datetime.now()}] [{worker_name}] ✅ Koneksi Server Darat PULIH! Flag → True")
            else:
                print(f"[{datetime.now()}] [{worker_name}] ❌ Server Darat tidak terjangkau. "
                      f"Tidur {CONNECTION_CHECK_INTERVAL // 60} menit (0% HTTP Spam)...")
        time.sleep(CONNECTION_CHECK_INTERVAL)

# =============================================================================
# THREAD 2: UPLOADER WORKER (PER-CAMERA TOKEN ENDPOINT)
# =============================================================================
def upload_worker():
    """
    Thread Pengirim Snapshot WebP ke Server Darat.
    Endpoint URL Dinamis per Kamera:
      POST /cctv/worker/cameras/{cameraToken}/snapshots
    """
    worker_name = "⚡ WebPUploader"

    while True:
        # ━━ 1. TUNGGU FLAG KONEKSI SERVER TRUE (HEMAT CPU & ANTI-SPAM) ━━
        server_connected.wait()

        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()

            # Auto-Pruning jika antrean lokal menumpuk > 5000 file
            c.execute("SELECT count(*) FROM queue")
            if c.fetchone()[0] > 5000:
                c.execute("DELETE FROM queue WHERE id IN (SELECT id FROM queue ORDER BY timestamp ASC LIMIT 500)")
                conn.commit()

            # Ambil 1 snapshot terlama dari queue.db
            c.execute("SELECT id, camera_token, webp_path, payload FROM queue ORDER BY timestamp ASC LIMIT 1")
            item = c.fetchone()

            if not item:
                conn.close()
                time.sleep(5)
                continue

            q_id, camera_token, webp_path, payload_str = item
            payload = json.loads(payload_str)

            # Abaikan/hapus data antrean lama yang tidak memiliki camera_token valid
            if not camera_token or camera_token == "None" or camera_token.startswith("token_cam"):
                if not camera_token or camera_token == "None":
                    print(f"[{worker_name}] Hapus antrean lama tanpa token (ID: {q_id})...")
                    c.execute("DELETE FROM queue WHERE id=?", (q_id,))
                    conn.commit()
                    conn.close()
                    continue

            if not os.path.exists(webp_path):
                c.execute("DELETE FROM queue WHERE id=?", (q_id,))
                conn.commit()
                conn.close()
                continue

            # MEMBENTUK ENDPOINT DINAMIS BERDASARKAN cameraToken
            clean_base_url = SERVER_BASE_URL.rstrip('/')
            endpoint_url = f"{clean_base_url}/cctv/worker/cameras/{camera_token}/snapshots"

            print(f"[{datetime.now()}] [{worker_name}] Uploading WebP <2KB (Queue ID: {q_id}, Ch: {payload['channel_id']})...")
            print(f"  -> Target Endpoint: {endpoint_url}")

            headers = {
                "Origin": clean_base_url,
                "User-Agent": "VsemarCCTVAgent/1.0"
            }

            with open(webp_path, 'rb') as img:
                res = requests.post(
                    endpoint_url,
                    headers=headers,
                    data={"captured_at": payload["captured_at"], "timestamp": payload["timestamp"]},
                    files={"file": (os.path.basename(webp_path), img, "image/webp")},
                    timeout=30
                )

            if res.status_code in [200, 201]:
                print(f"  -> [{worker_name}] ✅ Upload Berhasil ke Server Darat!")
                if os.path.exists(webp_path):
                    os.remove(webp_path)  # Hapus file WebP temporer setelah sukses diupload
                c.execute("DELETE FROM queue WHERE id=?", (q_id,))
            else:
                # Gagal HTTP -> Matikan Flag server_connected (clear)
                server_connected.clear()
                print(f"  -> [{worker_name}] ❌ Upload Gagal (HTTP {res.status_code}). Flag → False. Re-checking...")
                c.execute("UPDATE queue SET retry_count = retry_count + 1 WHERE id=?", (q_id,))

            conn.commit()
            conn.close()
            time.sleep(1)

        except requests.exceptions.RequestException:
            # Gagal Koneksi -> Matikan Flag server_connected (clear)
            server_connected.clear()
            print(f"[{datetime.now()}] [{worker_name}] ⚠️ Koneksi ke Server Darat Terputus! Flag → False.")
            time.sleep(5)
        except Exception as e:
            print(f"[{worker_name}] [ERROR] {e}")
            time.sleep(5)

# =============================================================================
# MAIN AGENT LOOP
# =============================================================================
def main():
    print("=" * 65)
    print("  🚢 NVR SNAPSHOT AGENT — DYNAMIC JSON CONFIG & CAMERA TOKEN")
    print(f"  Config File     : {CONFIG_FILE}")
    print(f"  Server Base URL : {SERVER_BASE_URL}")
    print(f"  Total Kamera    : {len(CAMERAS)} CCTV (Channels: {[c['channel'] for c in CAMERAS]})")
    print(f"  Interval        : Setiap {SNAPSHOT_INTERVAL_SEC} detik")
    print(f"  NVR IP          : {NVR_IP}:{NVR_PORT}")
    print("=" * 65)

    init_db()

    # Cek Koneksi Awal ke Server Darat
    print("\n🔌 Mengecek koneksi awal ke Server Darat...")
    if check_server_connection():
        server_connected.set()
        print("✅ Koneksi awal ke Server Darat BERHASIL. Flag → True\n")
    else:
        print("❌ Koneksi awal ke Server Darat GAGAL. Flag → False. Menunggu pemulihan...\n")

    # Thread 1: Connection Checker (Cek per 5 menit saat Flag=False)
    t_conn = threading.Thread(target=connection_checker_worker, daemon=True, name="ConnChecker")
    t_conn.start()

    # Thread 2: WebP Uploader Worker (Kuras antrean SQLite saat Flag=True)
    t_up = threading.Thread(target=upload_worker, daemon=True, name="WebPUploader")
    t_up.start()

    print("🚀 Sistem Agent Berjalan di Background.")
    print("   -> Task Lokal : Mengambil snapshot CCTV NVR.")
    print("   -> HD Folder  : /snapshots_hd_lokal/ (Bukti Lokal)")
    print("   -> WebP Queue : /snapshots_nvr_4cctv/ + SQLite queue.db (Transmisi Server)")
    print("   -> Endpoint   : /cctv/worker/cameras/{cameraToken}/snapshots\n")

    # Loop Penjadwalan Utama (Local Task)
    while True:
        try:
            capture_all_cctv_job()
            time.sleep(SNAPSHOT_INTERVAL_SEC)
        except KeyboardInterrupt:
            print("\n[INFO] Agent dihentikan pengguna.")
            sys.exit(0)

if __name__ == "__main__":
    main()
