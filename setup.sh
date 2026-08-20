#!/bin/bash
# =============================================================================
# 🚢 AUTOMATED SETUP SCRIPT — MARITIME NVR CCTV SNAPSHOT AGENT
# =============================================================================

set -e

echo "====================================================================="
echo "  🚢 MEMULAI INSTALLASI OTOMATIS AGEN CCTV SNAPSHOT (DEBIAN/UBUNTU)"
echo "====================================================================="

# Pengecekan Hak Akses Root / Sudo (Support mode 'su' maupun 'sudo')
SUDO_CMD=""
if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO_CMD="sudo"
    else
        echo "❌ Error: 'sudo' belum terinstall dan Anda bukan root."
        echo "   Silakan masuk sebagai root dengan perintah 'su -' lalu jalankan kembali script ini."
        exit 1
    fi
fi

# 1. Update & Install OS Dependencies (Termasuk sudo & btop untuk monitoring)
echo "📦 [1/5] Menginstall OS Packages (sudo, btop, git, python3, ffmpeg, webp, sqlite3)..."
$SUDO_CMD apt update -y
$SUDO_CMD apt install -y sudo btop git python3 python3-pip python3-venv ffmpeg webp sqlite3 curl ca-certificates

# 2. Install Python Libraries
echo "🐍 [2/5] Menginstall Library Python (requests, opencv-python-headless, numpy, pillow)..."
pip3 install requests opencv-python-headless numpy pillow --break-system-packages || pip install requests opencv-python-headless numpy pillow

# 3. Fix Git Security Ownership Permission
CURRENT_DIR=$(pwd)
CURRENT_USER=$(whoami)
echo "🔒 [3/5] Mengatur Hak Akses & Git Safe Directory di: $CURRENT_DIR"
git config --global --add safe.directory "$CURRENT_DIR" || true
git config --global --add safe.directory "*" || true
$SUDO_CMD chown -R $CURRENT_USER:$CURRENT_USER "$CURRENT_DIR"

# 4. Fix DNS Resolv jika offline/gagal resolve domain
echo "🌐 [4/5] Memastikan DNS Server (8.8.8.8 & 1.1.1.1) terkonfigurasi..."
if ! grep -q "8.8.8.8" /etc/resolv.conf; then
    echo "nameserver 8.8.8.8" | $SUDO_CMD tee -a /etc/resolv.conf > /dev/null
    echo "nameserver 1.1.1.1" | $SUDO_CMD tee -a /etc/resolv.conf > /dev/null
fi

# 5. Buat dan Aktifkan Auto Systemd Service (User=root untuk keamanan izin I/O)
echo "⚙️ [5/5] Membuat dan Mengaktifkan Service Background (Systemd)..."
SERVICE_PATH="/etc/systemd/system/cctv-snapshot.service"

$SUDO_CMD bash -c "cat <<EOF > $SERVICE_PATH
[Unit]
Description=Maritime NVR CCTV Snapshot Agent
After=network.target network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$CURRENT_DIR
ExecStart=/usr/bin/python3 $CURRENT_DIR/snapshotcompress.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF"

$SUDO_CMD systemctl daemon-reload

echo "====================================================================="
echo "  ✅ INSTALLASI SELESAI! (Service Systemd: Disabled & Inactive)"
echo "====================================================================="
echo "  📌 LANGKAH SELANJUTNYA:"
echo "     1. Monitor System (CPU/RAM)   : btop"
echo "     2. Edit IP NVR & Token Kamera : nano config.json"
echo "     3. Uji Coba Manual Terlebih Dulu : python3 snapshotcompress.py"
echo "     4. Aktifkan Auto-Start Boot   : $SUDO_CMD systemctl enable cctv-snapshot"
echo "     5. Jalankan Service           : $SUDO_CMD systemctl start cctv-snapshot"
echo "     6. Cek Status Service         : $SUDO_CMD systemctl status cctv-snapshot"
echo "     7. Lihat Log Realtime         : tail -f logs/agent_\$(date +%Y-%m-%d).log"
echo "====================================================================="
