#!/usr/bin/env python3
# Maritime NVR CCTV Snapshot Agent (<10KB WebP + SQLite WAL Queue)

import os
import sys
import time
import json
import sqlite3
import tempfile
import threading
import subprocess
import requests
from datetime import datetime, timezone
import cv2
import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")

# Class Logger untuk menulis log berganti file otomatis per hari (logs/agent_YYYY-MM-DD.log)
class DailyRotatedLogger:
    def __init__(self, log_dir):
        self.terminal = sys.stdout
        self.log_dir = log_dir
        os.makedirs(self.log_dir, exist_ok=True)
        self.current_date = None
        self.logfile = None

    def _get_logfile(self):
        now_date = datetime.now().strftime("%Y-%m-%d")
        if now_date != self.current_date or self.logfile is None or self.logfile.closed:
            if self.logfile and not self.logfile.closed:
                self.logfile.close()
            self.current_date = now_date
            filepath = os.path.join(self.log_dir, f"agent_{self.current_date}.log")
            self.logfile = open(filepath, "a", encoding="utf-8")
        return self.logfile

    def write(self, message):
        self.terminal.write(message)
        f = self._get_logfile()
        f.write(message)
        f.flush()

    def flush(self):
        self.terminal.flush()
        if self.logfile and not self.logfile.closed:
            self.logfile.flush()

sys.stdout = DailyRotatedLogger(LOG_DIR)
sys.stderr = sys.stdout

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

# Kompresi WebP (< 10.0 KB Guarantee)
SNAPSHOT_SCALE   = '360:270'   # Resolusi piksel statik 360 x 270
INITIAL_QUALITY  = 8          # Quality WebP awal (0-100)
COMPRESSION_LEVEL= 6          # Kompresi libwebp maksimal (0-6)
MAX_TARGET_KB    = 10.0       # Batas maksimal ukuran file WebP (< 10.0 KB)

OUTPUT_HD_DIR  = os.path.join(BASE_DIR, "snapshots_hd_lokal")    # Folder HD Asli (Bukti Lokal)
OUTPUT_DIR     = os.path.join(BASE_DIR, "snapshots_nvr_4cctv")  # Folder WebP Ringan (< 10 KB)
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
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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

    # 2. Kompresi ke WebP Adaptif (Statik 360x270, cwebp -size 9500 & OpenCV preprocessing)
    # Menjamin file SELALU < 10.0 KB (10240 Bytes) jernih murni tanpa menurunkan resolusi 360x270.
    TARGET_W, TARGET_H = 360, 270
    MAX_BYTES = 10240
    SAFE_TARGET_BYTES = 9500

    def preprocess_image(img_bgr: np.ndarray, level: int) -> np.ndarray:
        if level == 0:
            return img_bgr
        out = img_bgr
        d = 5 + level * 2
        sigma_color = 25 + level * 20
        sigma_space = 25 + level * 20
        out = cv2.bilateralFilter(out, d, sigma_color, sigma_space)
        if level >= 2:
            k = 3 if level < 4 else 5
            out = cv2.medianBlur(out, k)
        if level >= 3:
            bits = 6 if level == 3 else 5
            shift = 8 - bits
            out = ((out.astype(np.uint16) >> shift) << shift).astype(np.uint8)
        return out

    def encode_cwebp_size(png_path: str, out_path: str, target_bytes: int):
        cmd = [
            "cwebp",
            "-size", str(target_bytes),
            "-m", "6",
            "-pass", "10",
            "-sns", "80",
            "-mt",
            png_path,
            "-o", out_path
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    webp_size_kb = 0.0
    webp_success = False
    used_level = 0

    img_bgr = cv2.imread(hd_filepath)
    if img_bgr is not None:
        img_resized = cv2.resize(img_bgr, (TARGET_W, TARGET_H), interpolation=cv2.INTER_LANCZOS4)
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_stage_png = os.path.join(tmpdir, "stage.png")

            # Loop adaptif Level 0 -> 4
            for lvl in range(0, 5):
                proc = preprocess_image(img_resized, lvl)
                cv2.imwrite(tmp_stage_png, proc)
                try:
                    encode_cwebp_size(tmp_stage_png, webp_filepath, SAFE_TARGET_BYTES)
                    final_bytes = os.path.getsize(webp_filepath)
                    if final_bytes <= MAX_BYTES:
                        webp_size_kb = final_bytes / 1024.0
                        webp_success = True
                        used_level = lvl
                        print(f"    [Adaptive cwebp] Ch{channel_num}: Lvl {lvl} → {webp_size_kb:.2f} KB ({final_bytes} B) ✅ < 10KB!")
                        break
                except Exception as e:
                    print(f"    [Adaptive cwebp ERROR Lvl {lvl}]: {e}")

            # Fallback ekstrem jika Lvl 4 belum cukup
            if not webp_success:
                proc = preprocess_image(img_resized, 4)
                cv2.imwrite(tmp_stage_png, proc)
                for forced_t in (8500, 7000, 5500):
                    try:
                        encode_cwebp_size(tmp_stage_png, webp_filepath, forced_t)
                        final_bytes = os.path.getsize(webp_filepath)
                        if final_bytes <= MAX_BYTES:
                            webp_size_kb = final_bytes / 1024.0
                            webp_success = True
                            used_level = 4
                            print(f"    [Adaptive cwebp Fallback] Ch{channel_num}: Forced {forced_t} → {webp_size_kb:.2f} KB ✅")
                            break
                    except Exception:
                        pass

    if not webp_success:
        return {"success": False, "channel": channel_num, "error": "Gagal kompresi WebP"}

    # 3. Simpan Metadata + cameraToken ke Antrean SQLite queue.db (Store & Forward)
    payload = {
        "channel_id": channel_num,
        "camera_token": camera_token,
        "captured_at": now_str,
        "timestamp": ts,
        "resolution": "360x270",
        "level": used_level
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
        "quality": used_level,
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

            print(f"[{datetime.now()}] [{worker_name}] Uploading WebP <10KB (Queue ID: {q_id}, Ch: {payload['channel_id']})...")
            print(f"  -> Target Endpoint: {endpoint_url}")

            with open(webp_path, 'rb') as img:
                res = requests.post(
                    endpoint_url,
                    data={"captured_at": payload["captured_at"]},
                    files={"file": (os.path.basename(webp_path), img, "image/webp")},
                    timeout=30
                )

            if res.status_code in [200, 201]:
                print(f"  -> [{worker_name}] ✅ Upload Berhasil ke Server Darat (HTTP {res.status_code})!")
                print(f"     Response: {res.text}")
                if os.path.exists(webp_path):
                    os.remove(webp_path)  # Hapus file WebP temporer setelah sukses diupload
                c.execute("DELETE FROM queue WHERE id=?", (q_id,))
            else:
                # Gagal HTTP -> Matikan Flag server_connected (clear)
                server_connected.clear()
                print(f"  -> [{worker_name}] ❌ Upload Gagal (HTTP {res.status_code}). Response: {res.text}. Flag → False.")
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
