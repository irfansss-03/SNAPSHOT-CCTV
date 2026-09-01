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
echo "📦 [1/6] Menginstall OS Packages (sudo, btop, git, python3, ffmpeg, webp, sqlite3, gnupg)..."
$SUDO_CMD apt update -y
$SUDO_CMD apt install -y sudo btop git python3 python3-pip python3-venv ffmpeg webp sqlite3 curl ca-certificates gnupg

# 2. Install NetBird (Remote Management & Mesh VPN)
echo "🦅 [2/6] Menginstall NetBird VPN..."
curl -sSL https://pkgs.netbird.io/debian/public.key | $SUDO_CMD gpg --dearmor --yes --output /usr/share/keyrings/netbird-archive-keyring.gpg
echo 'deb [signed-by=/usr/share/keyrings/netbird-archive-keyring.gpg] https://pkgs.netbird.io/debian stable main' | $SUDO_CMD tee /etc/apt/sources.list.d/netbird.list > /dev/null
$SUDO_CMD apt update -y
$SUDO_CMD apt install -y netbird

# 3. Install Python Libraries
echo "🐍 [3/6] Menginstall Library Python (requests, opencv-python-headless, numpy, pillow)..."
pip3 install requests opencv-python-headless numpy pillow --break-system-packages || pip install requests opencv-python-headless numpy pillow

# 4. Fix Git Security Ownership Permission
CURRENT_DIR=$(pwd)
CURRENT_USER=$(whoami)
echo "🔒 [4/6] Mengatur Hak Akses & Git Safe Directory di: $CURRENT_DIR"
git config --global --add safe.directory "$CURRENT_DIR" || true
git config --global --add safe.directory "*" || true
$SUDO_CMD chown -R $CURRENT_USER:$CURRENT_USER "$CURRENT_DIR"

# 5. Fix DNS Resolv jika offline/gagal resolve domain
echo "🌐 [5/6] Memastikan DNS Server (8.8.8.8 & 1.1.1.1) terkonfigurasi..."
if ! grep -q "8.8.8.8" /etc/resolv.conf; then
    echo "nameserver 8.8.8.8" | $SUDO_CMD tee -a /etc/resolv.conf > /dev/null
    echo "nameserver 1.1.1.1" | $SUDO_CMD tee -a /etc/resolv.conf > /dev/null
fi

# 6. Buat dan Konfigurasi Auto Systemd Service (User=root untuk keamanan izin I/O)
echo "⚙️ [6/6] Membuat File Service Background Systemd (Disabled & Inactive)..."
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
echo "     1. Hubungkan NetBird VPN      : netbird up --setup-key [minta key]"
echo "     2. Monitor System (CPU/RAM)   : btop"
echo "     3. Edit IP NVR & Token Kamera : nano config.json"
echo "     4. Uji Coba Manual Terlebih Dulu : python3 snapshotcompress.py"
echo "     5. Aktifkan Auto-Start Boot   : $SUDO_CMD systemctl enable cctv-snapshot"
echo "     6. Jalankan Service           : $SUDO_CMD systemctl start cctv-snapshot"
echo "     7. Cek Status Service         : $SUDO_CMD systemctl status cctv-snapshot"
echo "     8. Lihat Log Realtime         : tail -f logs/agent_\$(date +%Y-%m-%d).log"
echo "====================================================================="
