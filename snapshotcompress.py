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
HD_RETENTION_DAYS         = CFG["agent"].get("hd_retention_days", 30)
MOCK_MODE                 = CFG["agent"].get("mock_mode", False)
MOCK_VIDEO                = CFG["agent"].get("mock_video", "cctv.kapal.mp4")

CAMERAS                   = CFG["cameras"]

# Saklar Koneksi Server Darat & Lock Thread-Safe Database
server_connected = threading.Event()
db_lock          = threading.Lock()

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
    with db_lock:
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
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
                      retry_count  INTEGER DEFAULT 0,
                      is_uploading INTEGER DEFAULT 0)''')
        # Migrasi otomatis jika kolom camera_token atau is_uploading belum ada di DB lama
        c.execute("PRAGMA table_info(queue)")
        columns = [row[1] for row in c.fetchall()]
        if "camera_token" not in columns:
            c.execute("ALTER TABLE queue ADD COLUMN camera_token TEXT")
        if "is_uploading" not in columns:
            c.execute("ALTER TABLE queue ADD COLUMN is_uploading INTEGER DEFAULT 0")
        # Reset is_uploading flag saat startup jika ada crash/restart sebelumnya
        c.execute("UPDATE queue SET is_uploading = 0 WHERE is_uploading = 1")
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

    # 1. Capture Frame HD Asli (Perintah Asli & Stabil dari Laptop)
    hd_cmd = [FFMPEG_CMD, "-y"]
    if MOCK_MODE:
        mock_sec = (channel_num * 5) % 25
        hd_cmd.extend(["-ss", f"00:00:{mock_sec:02d}", "-i", source_input, "-vframes", "1", "-q:v", "2", hd_filepath])
    else:
        hd_cmd.extend([
            "-rtsp_transport", "tcp",
            "-timeout", "5000000",
            "-i", source_input,
            "-vf", r"select=eq(pict_type\,I)",  # Filter I-Frame khusus HEVC H.265 (Anti-Grey Frame)
            "-vframes", "1",
            "-q:v", "2",
            hd_filepath
        ])

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
    with db_lock:
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        c = conn.cursor()
        c.execute(
            "INSERT INTO queue (channel_id, camera_token, hd_path, webp_path, payload, timestamp, is_uploading) VALUES (?, ?, ?, ?, ?, ?, 0)",
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

def purge_old_hd_snapshots():
    """Menghapus otomatis subfolder HD lokal dan file log yang melebihi retensi hari (default 30 hari)."""
    try:
        now = datetime.now()
        # 1. Purge Subfolder HD Lokal (snapshots_hd_lokal/YYYY-MM-DD)
        if os.path.exists(OUTPUT_HD_DIR):
            for folder_name in os.listdir(OUTPUT_HD_DIR):
                folder_path = os.path.join(OUTPUT_HD_DIR, folder_name)
                if os.path.isdir(folder_path):
                    try:
                        folder_date = datetime.strptime(folder_name, "%Y-%m-%d")
                        delta_days = (now - folder_date).days
                        if delta_days > HD_RETENTION_DAYS:
                            import shutil
                            shutil.rmtree(folder_path)
                            print(f"  [Auto-Purge HD] Hapus folder HD tua (> {HD_RETENTION_DAYS} hari): {folder_name}")
                    except ValueError:
                        pass
        # 2. Purge Log Tua (logs/agent_YYYY-MM-DD.log)
        log_dir = os.path.join(BASE_DIR, "logs")
        if os.path.exists(log_dir):
            for file_name in os.listdir(log_dir):
                if file_name.startswith("agent_") and file_name.endswith(".log"):
                    date_str = file_name.replace("agent_", "").replace(".log", "")
                    try:
                        log_date = datetime.strptime(date_str, "%Y-%m-%d")
                        if (now - log_date).days > HD_RETENTION_DAYS:
                            os.remove(os.path.join(log_dir, file_name))
                            print(f"  [Auto-Purge Log] Hapus log tua (> {HD_RETENTION_DAYS} hari): {file_name}")
                    except ValueError:
                        pass
    except Exception as e:
        print(f"[Auto-Purge Error]: {e}")

def capture_all_cctv_job():
    """Tugas berkala mengambil snapshot seluruh CCTV NVR sesuai config.json."""
    purge_old_hd_snapshots()
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
# THREAD 2: MULTI-THREAD UPLOADER WORKERS (PER-CAMERA TOKEN ENDPOINT)
# =============================================================================
def upload_worker(worker_id: int):
    """
    Thread Pengirim Snapshot WebP ke Server Darat (Multi-Thread Paralel).
    Tiap worker memakai persistent requests.Session() dengan HTTP Keep-Alive connection pooling.
    """
    worker_name = f"⚡ Uploader-{worker_id}"

    # Inisialisasi HTTP Session dengan Keep-Alive Connection Pooling
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=5, pool_maxsize=5, max_retries=1)
    session.mount('https://', adapter)
    session.mount('http://', adapter)

    while True:
        # ━━ 1. TUNGGU FLAG KONEKSI SERVER TRUE (HEMAT CPU & ANTI-SPAM) ━━
        server_connected.wait()

        q_id = None
        camera_token = None
        webp_path = None
        payload = None

        try:
            # Ambil 1 item terlama secara thread-safe dan tandai is_uploading = 1
            with db_lock:
                conn = sqlite3.connect(DB_PATH, timeout=30.0)
                c = conn.cursor()
                c.execute("SELECT id, camera_token, webp_path, payload FROM queue WHERE is_uploading = 0 ORDER BY timestamp ASC LIMIT 1")
                item = c.fetchone()
                if item:
                    q_id, camera_token, webp_path, payload_str = item
                    c.execute("UPDATE queue SET is_uploading = 1 WHERE id = ?", (q_id,))
                    conn.commit()
                conn.close()

            if not item:
                time.sleep(1)
                continue

            payload = json.loads(payload_str)
            cam_token = camera_token or payload.get("camera_token")
            channel_id = payload.get("channel_id", "?")

            # Abaikan/hapus data antrean lama yang tidak memiliki camera_token valid
            if not cam_token or cam_token == "None" or str(cam_token).startswith("token_cam"):
                print(f"[{worker_name}] Hapus antrean lama tanpa token valid (ID: {q_id})...")
                with db_lock:
                    conn = sqlite3.connect(DB_PATH, timeout=30.0)
                    c = conn.cursor()
                    c.execute("DELETE FROM queue WHERE id=?", (q_id,))
                    conn.commit()
                    conn.close()
                continue

            if not os.path.exists(webp_path):
                with db_lock:
                    conn = sqlite3.connect(DB_PATH, timeout=30.0)
                    c = conn.cursor()
                    c.execute("DELETE FROM queue WHERE id=?", (q_id,))
                    conn.commit()
                    conn.close()
                continue

            # MEMBENTUK ENDPOINT DINAMIS BERDASARKAN cameraToken
            clean_base_url = SERVER_BASE_URL.rstrip('/')
            endpoint_url = f"{clean_base_url}/cctv/worker/cameras/{cam_token}/snapshots"

            print(f"[{datetime.now()}] [{worker_name}] Uploading WebP <10KB (Queue ID: {q_id}, Ch: {channel_id})...")

            with open(webp_path, 'rb') as img:
                res = session.post(
                    endpoint_url,
                    data={"captured_at": payload["captured_at"]},
                    files={"file": (os.path.basename(webp_path), img, "image/webp")},
                    timeout=30
                )

            if res.status_code in [200, 201]:
                print(f"  -> [{worker_name}] ✅ Upload Berhasil ke Server Darat (HTTP {res.status_code})!")
                print(f"     Response: {res.text}")
                if os.path.exists(webp_path):
                    try:
                        os.remove(webp_path)  # Hapus file WebP temporer setelah sukses diupload
                    except Exception:
                        pass
                with db_lock:
                    conn = sqlite3.connect(DB_PATH, timeout=30.0)
                    c = conn.cursor()
                    c.execute("DELETE FROM queue WHERE id=?", (q_id,))
                    conn.commit()
                    conn.close()
            else:
                # Gagal HTTP -> Matikan Flag server_connected (clear) & reset is_uploading = 0
                server_connected.clear()
                print(f"  -> [{worker_name}] ❌ Upload Gagal (HTTP {res.status_code}). Response: {res.text}. Flag → False.")
                with db_lock:
                    conn = sqlite3.connect(DB_PATH, timeout=30.0)
                    c = conn.cursor()
                    c.execute("UPDATE queue SET is_uploading = 0, retry_count = retry_count + 1 WHERE id=?", (q_id,))
                    conn.commit()
                    conn.close()

            time.sleep(0.5)

        except requests.exceptions.RequestException as req_err:
            # Gagal Koneksi -> Matikan Flag server_connected (clear) & reset is_uploading = 0
            server_connected.clear()
            print(f"[{datetime.now()}] [{worker_name}] ⚠️ Koneksi ke Server Darat Terputus! Flag → False.")
            if q_id:
                with db_lock:
                    conn = sqlite3.connect(DB_PATH, timeout=30.0)
                    c = conn.cursor()
                    c.execute("UPDATE queue SET is_uploading = 0 WHERE id=?", (q_id,))
                    conn.commit()
                    conn.close()
            time.sleep(3)
        except Exception as e:
            print(f"[{worker_name}] [ERROR] {e}")
            if q_id:
                with db_lock:
                    conn = sqlite3.connect(DB_PATH, timeout=30.0)
                    c = conn.cursor()
                    c.execute("UPDATE queue SET is_uploading = 0 WHERE id=?", (q_id,))
                    conn.commit()
                    conn.close()
            time.sleep(3)

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

    # Thread 2: Dynamic Multi-Thread WebP Uploader Workers (Otomatis sesuai jumlah kamera)
    num_workers = max(1, len(CAMERAS))
    print(f"🧵 Menjalankan {num_workers} Thread Uploader Paralel (Sesuai {len(CAMERAS)} Kamera)...")
    for i in range(num_workers):
        worker_id = i + 1
        t_up = threading.Thread(target=upload_worker, args=(worker_id,), daemon=True, name=f"Uploader-{worker_id}")
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
