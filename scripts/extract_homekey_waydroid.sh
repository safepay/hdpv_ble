#!/usr/bin/env bash
#
# powerview-extract.sh
#
# Interactive helper for running the Hunter Douglas PowerView Android app
# inside Waydroid on Linux, in order to read the local `home_key` out of the
# app's own SQLite database.
#
# Why this exists: the app stores the home key locally, but the APK sets
# allowBackup="false", so `adb backup` cannot reach it and a real phone would
# need root. Waydroid keeps Android's /data as ordinary directories on the
# host, so the database is readable with no root on the Android side at all.
#
# Tested on: Ubuntu 24.04 LTS (live session and installed), x86_64.
#
# This only reads data from YOUR OWN account on YOUR OWN hardware. The key it
# recovers is a credential - treat it like a password.
#
# Licence: public domain / CC0. No warranty.
#
# ---------------------------------------------------------------------------
# Provenance: contributed by Sander Dekker (@sdekker90) in
# https://github.com/safepay/hdpv_ble/issues/34 and vendored here unchanged
# apart from this note and one variable rename. Not tested by the maintainer,
# who runs neither Linux nor Waydroid - report problems on that issue.
#
# Step 2 installs Waydroid by piping https://repo.waydro.id to `sudo bash`,
# which is upstream Waydroid's own documented install method. It runs as root
# and is only as trustworthy as that host; the step is prompted, so decline it
# and install Waydroid through your distro if you would rather not.
#
# The rest of the repository is Apache 2.0 - this file keeps the CC0 its author
# released it under.
# ---------------------------------------------------------------------------

set -uo pipefail

# ---------------------------------------------------------------------------
# Configuration (override with environment variables)
# ---------------------------------------------------------------------------

PKG="${PKG:-com.hunterdouglas.powerview}"
WAYLAND_SOCK="${WAYLAND_SOCK:-waydroid}"
WESTON_W="${WESTON_W:-1280}"
WESTON_H="${WESTON_H:-800}"
WESTON_LOG="${WESTON_LOG:-/tmp/weston-waydroid.log}"
OUTDIR="${OUTDIR:-$HOME/powerview-extract}"

DATA_DIR="$HOME/.local/share/waydroid/data/data/$PKG"
BASE_PROP="/var/lib/waydroid/waydroid_base.prop"

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

if [ -t 1 ] && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
    C_RESET=$(tput sgr0); C_BOLD=$(tput bold)
    C_RED=$(tput setaf 1); C_GREEN=$(tput setaf 2)
    C_YELLOW=$(tput setaf 3); C_BLUE=$(tput setaf 4)
else
    C_RESET=""; C_BOLD=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""
fi

info()  { printf '%s==>%s %s\n' "$C_BLUE$C_BOLD" "$C_RESET" "$*"; }
ok()    { printf '%s  ok%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn()  { printf '%swarn%s %s\n' "$C_YELLOW" "$C_RESET" "$*"; }
fail()  { printf '%sFAIL%s %s\n' "$C_RED$C_BOLD" "$C_RESET" "$*" >&2; }
note()  { printf '     %s\n' "$*"; }
hr()    { printf '%s\n' "-------------------------------------------------------------"; }

die() { fail "$*"; exit 1; }

confirm() {
    # confirm "question" [default y|n]
    local q="$1" def="${2:-y}" reply prompt
    case "$def" in
        y) prompt="[Y/n]" ;;
        n) prompt="[y/N]" ;;
    esac
    while true; do
        read -r -p "$(printf '%s?%s %s %s ' "$C_BOLD" "$C_RESET" "$q" "$prompt")" reply
        reply="${reply:-$def}"
        case "${reply,,}" in
            y|yes) return 0 ;;
            n|no)  return 1 ;;
            *)     echo "Please answer y or n." ;;
        esac
    done
}

pause() { read -r -p "Press Enter to continue... " _; }

need_cmd() { command -v "$1" >/dev/null 2>&1; }

# Ask for a file path with readline tab-completion. Handy because APKMirror
# filenames contain parentheses, which are a nightmare to type by hand.
ask_path() {
    local prompt="$1" __out="$2" path
    read -r -e -p "$prompt" path
    path="${path/#\~/$HOME}"
    printf -v "$__out" '%s' "$path"
}

sudo_keepalive() {
    if ! sudo -n true 2>/dev/null; then
        info "Some steps need sudo. Authenticating once up front."
        sudo -v || die "sudo authentication failed."
    fi
}

# ---------------------------------------------------------------------------
# Step 1: preflight
# ---------------------------------------------------------------------------

IS_LIVE=0
NEEDS_SW_RENDER=0
SESSION_IS_WAYLAND=0

step_preflight() {
    info "Preflight checks"
    hr

    # --- OS ---
    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        note "Distro:  ${PRETTY_NAME:-unknown}"
    fi
    note "Kernel:  $(uname -r)"

    # --- live session? ---
    if findmnt -n -o SOURCE / 2>/dev/null | grep -qi 'cow\|overlay' \
       || grep -q casper /proc/cmdline 2>/dev/null; then
        IS_LIVE=1
        warn "This looks like a live/ISO session."
        note "Root filesystem is RAM-backed. NOTHING here survives a reboot -"
        note "not the packages, not the container, not the app's login."
        if ! grep -qw persistent /proc/cmdline 2>/dev/null; then
            note "No 'persistent' flag on the kernel cmdline, so persistence is OFF."
        fi
        note "Snapshot to real storage the moment the app is signed in (step 9)."
    else
        ok "Installed system (not a live session)."
    fi

    # --- root filesystem space ---
    local avail
    avail=$(df -BG --output=avail / 2>/dev/null | tail -1 | tr -dc '0-9')
    if [ -n "$avail" ]; then
        note "Free on /: ${avail}G"
        if [ "$avail" -lt 12 ]; then
            warn "Under 12G free on /. The Android image is ~1G but the container"
            note "grows as the app runs. Consider step 3 (external storage)."
        fi
    fi

    # --- binder: the go/no-go test ---
    if [ -e /dev/binder ] || [ -d /dev/binderfs ]; then
        ok "Binder already present."
    elif sudo modprobe binder_linux devices=binder,hwbinder,vndbinder 2>/dev/null; then
        ok "binder_linux module loaded."
    elif modinfo binder_linux >/dev/null 2>&1; then
        warn "binder_linux exists but would not load. Check dmesg."
    else
        fail "binder_linux not found - Waydroid cannot run on this kernel."
        note "This is the hard blocker. Immutable/gaming distros (SteamOS, some"
        note "Chromebook kernels) commonly lack it. Options: use a mainline"
        note "Ubuntu/Fedora kernel, or install waydroid-dkms if your distro has it."
        return 1
    fi

    # --- GPU ---
    local gpu
    gpu=$(lspci 2>/dev/null | grep -iE 'vga|3d controller' | head -3)
    if [ -n "$gpu" ]; then
        note "GPU:     $(echo "$gpu" | sed 's/.*: //' | head -1)"
    fi
    if echo "$gpu" | grep -qi nvidia; then
        NEEDS_SW_RENDER=1
        warn "NVIDIA GPU detected."
        note "Waydroid's hardware GL path frequently fails here - the symptom is"
        note "Android's surface dying and respawning, i.e. rapid flashing that"
        note "never reaches a home screen. Step 4 forces software rendering."
    else
        ok "Non-NVIDIA GPU - hardware rendering will probably work."
    fi

    # --- session type ---
    if [ "${XDG_SESSION_TYPE:-}" = "wayland" ]; then
        SESSION_IS_WAYLAND=1
        ok "Already a Wayland session - no nested compositor needed."
    else
        note "Session: ${XDG_SESSION_TYPE:-unknown} (not Wayland)"
        note "Waydroid requires Wayland. Step 5 runs a nested Weston for it."
    fi

    hr
    ok "Preflight complete."
    return 0
}

# ---------------------------------------------------------------------------
# Step 2: install packages
# ---------------------------------------------------------------------------

step_install() {
    info "Installing Waydroid and helpers"

    if need_cmd waydroid; then
        ok "waydroid already installed ($(waydroid --version 2>/dev/null || echo 'version unknown'))."
    else
        if ! need_cmd apt; then
            fail "This installer step is Debian/Ubuntu only (apt not found)."
            note "On Arch: yay -S waydroid. On Fedora: see docs.waydro.id."
            return 1
        fi
        confirm "Install waydroid from the official repo (repo.waydro.id)?" y || return 1
        sudo apt-get update -qq || return 1
        sudo apt-get install -y curl ca-certificates || return 1
        curl -fsSL https://repo.waydro.id | sudo bash || return 1
        sudo apt-get install -y waydroid || return 1
        ok "waydroid installed."
    fi

    # weston only needed if we're not already on Wayland
    if [ "$SESSION_IS_WAYLAND" -eq 0 ] && ! need_cmd weston; then
        sudo apt-get install -y weston || return 1
        ok "weston installed."
    fi

    need_cmd sqlite3 || sudo apt-get install -y sqlite3 || return 1
    need_cmd aapt    || sudo apt-get install -y aapt || warn "aapt unavailable - APK inspection will be skipped."

    ok "Packages ready."
}

# ---------------------------------------------------------------------------
# Step 3: optional external storage for the Android image
# ---------------------------------------------------------------------------

step_storage() {
    info "Storage location for the Android image"
    note "Default is /var/lib/waydroid on your root filesystem. On a live"
    note "session that is RAM, so a ~1G image plus a running container can"
    note "fill it. Pointing it at a real disk avoids that."
    echo

    if [ -e /var/lib/waydroid/images/system.img ]; then
        warn "An Android image already exists at /var/lib/waydroid/images."
        note "Redirecting storage now would hide it. Skip this step unless you"
        note "intend to start over."
        confirm "Continue anyway?" n || return 0
    fi

    lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINTS 2>/dev/null | sed 's/^/     /'
    echo
    confirm "Bind-mount an existing partition for Waydroid storage?" n || {
        ok "Using the default location."
        return 0
    }

    local dev mnt
    read -r -e -p "Device to use (e.g. /dev/sda4): " dev
    [ -b "$dev" ] || { fail "$dev is not a block device."; return 1; }

    read -r -e -p "Mountpoint [/mnt/wd]: " mnt
    mnt="${mnt:-/mnt/wd}"

    sudo mkdir -p "$mnt" || return 1
    if ! mountpoint -q "$mnt"; then
        sudo mount "$dev" "$mnt" || { fail "mount failed - see the error above."; return 1; }
    fi
    ok "Mounted $dev at $mnt"

    sudo mkdir -p "$mnt/waydroid-data" /var/lib/waydroid || return 1
    sudo mount --bind "$mnt/waydroid-data" /var/lib/waydroid || return 1

    local avail
    avail=$(df -BG --output=avail /var/lib/waydroid | tail -1 | tr -dc '0-9')
    note "Available at /var/lib/waydroid: ${avail}G"
    if [ "${avail:-0}" -lt 10 ]; then
        fail "That does not look right - the bind mount may not have taken."
        return 1
    fi
    ok "Waydroid storage now on $dev."
    note "Note: bind mounts do not survive a reboot. Re-run this step if you restart."
}

# ---------------------------------------------------------------------------
# Step 4: renderer configuration
# ---------------------------------------------------------------------------

step_renderer() {
    info "Renderer configuration"

    if ! sudo test -f "$BASE_PROP"; then
        warn "$BASE_PROP does not exist yet."
        note "Run 'waydroid init' first (step 4), then come back to this step."
        return 1
    fi

    if sudo grep -q 'ro.hardware.egl=swiftshader' "$BASE_PROP" 2>/dev/null; then
        ok "Software rendering already configured."
        return 0
    fi

    if [ "$NEEDS_SW_RENDER" -eq 1 ]; then
        note "Recommended for your GPU: software rendering (swiftshader)."
    else
        note "Your GPU should manage hardware rendering. Only force software"
        note "rendering if Android flashes and never boots."
    fi
    echo
    confirm "Force software rendering?" "$([ "$NEEDS_SW_RENDER" -eq 1 ] && echo y || echo n)" || {
        ok "Leaving hardware rendering in place."
        return 0
    }

    sudo cp "$BASE_PROP" "$BASE_PROP.bak.$(date +%s)" 2>/dev/null
    sudo tee -a "$BASE_PROP" >/dev/null <<'EOF'
ro.hardware.gralloc=default
ro.hardware.egl=swiftshader
EOF
    ok "Software rendering enabled. (Backup saved alongside the original.)"
    note "Expect a slow, choppy Android - fine for reading a database."
    note "Undo by deleting those two lines from $BASE_PROP."
}

# ---------------------------------------------------------------------------
# Step 5: initialise the container
# ---------------------------------------------------------------------------

step_init() {
    info "Initialising Waydroid"

    if [ -e /var/lib/waydroid/images/system.img ]; then
        ok "Android image already present."
    else
        note "This downloads roughly 1GB (system + vendor images)."
        note "Choosing VANILLA (no Google Play). Play Store adds a device"
        note "certification dance you do not need when sideloading an APK."
        confirm "Download and initialise now?" y || return 1
        sudo waydroid init || { fail "waydroid init failed."; return 1; }
        ok "Images downloaded and extracted."
    fi

    sudo systemctl start waydroid-container 2>/dev/null
    if [ "$(sudo systemctl is-active waydroid-container 2>/dev/null)" = "active" ]; then
        ok "Container service active."
    else
        warn "waydroid-container is not active. On systems without systemd,"
        note "start it manually: sudo waydroid container start"
    fi
}

# ---------------------------------------------------------------------------
# Step 6: compositor + session
# ---------------------------------------------------------------------------

start_weston() {
    if pgrep -f "weston.*--socket=$WAYLAND_SOCK" >/dev/null; then
        ok "Weston already running."
        return 0
    fi

    need_cmd weston || { fail "weston not installed - run step 2."; return 1; }

    # setsid detaches it, so closing this terminal does not kill the
    # compositor. That is a common way to lose a working session.
    setsid weston \
        --socket="$WAYLAND_SOCK" \
        --width="$WESTON_W" --height="$WESTON_H" \
        --idle-time=0 \
        >"$WESTON_LOG" 2>&1 &

    local sock="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/$WAYLAND_SOCK"
    local waited=0
    while [ "$waited" -lt 20 ]; do
        [ -S "$sock" ] && break
        sleep 0.5
        waited=$((waited + 1))
    done

    if [ -S "$sock" ]; then
        ok "Weston running, socket at $sock"
        note "A chequerboard window will appear - that is an empty Wayland"
        note "desktop, not an error. Android draws into it shortly."
        return 0
    fi

    fail "Weston did not create its socket."
    note "Last lines of $WESTON_LOG:"
    tail -15 "$WESTON_LOG" 2>/dev/null | sed 's/^/     /'
    note "Exit code 127 there means weston is not installed (run step 2)."
    return 1
}

step_launch() {
    info "Launching Android"

    if [ "$SESSION_IS_WAYLAND" -eq 0 ]; then
        start_weston || return 1
        export WAYLAND_DISPLAY="$WAYLAND_SOCK"
    fi

    note "Starting the full Android UI. On software rendering, expect 2-5"
    note "minutes of black screen on first boot. Black is normal; rapid"
    note "flashing is not - that means the renderer needs step 4."
    echo
    note "This runs in the foreground. Ctrl+C stops the UI; choose 6 again"
    note "to relaunch, or 7 to install the app once Android has booted."
    echo
    pause

    waydroid show-full-ui
}

step_launch_bg() {
    info "Launching Android in the background"

    if [ "$SESSION_IS_WAYLAND" -eq 0 ]; then
        start_weston || return 1
        export WAYLAND_DISPLAY="$WAYLAND_SOCK"
    fi

    setsid waydroid show-full-ui >/tmp/waydroid-ui.log 2>&1 &
    sleep 5
    ok "Android starting - log at /tmp/waydroid-ui.log"
    note "Give it 2-5 minutes on software rendering, then look for the window."
}

# ---------------------------------------------------------------------------
# Step 7: install the APK
# ---------------------------------------------------------------------------

inspect_apk() {
    local apk="$1"
    need_cmd aapt || return 0

    local ver bt feat
    ver=$(aapt dump badging "$apk" 2>/dev/null | grep -o "versionName='[^']*'" | head -1)
    bt=$(aapt dump badging "$apk" 2>/dev/null | grep -ci bluetooth)
    feat=$(aapt dump badging "$apk" 2>/dev/null | grep -o "uses-feature[^']*'[^']*'" | sort -u)

    note "Version: ${ver:-unknown}"
    if [ "$bt" -gt 0 ]; then
        warn "This build requests Bluetooth permissions."
        note "Waydroid ships NO Bluetooth HAL - there is no radio to enable, and"
        note "no command will create one. If the app hard-blocks on a Bluetooth"
        note "screen, try an older version that predates BLE onboarding."
    else
        ok "No Bluetooth permissions requested - onboarding should not block."
    fi
    if echo "$feat" | grep -qi 'bluetooth_le'; then
        warn "Declares bluetooth_le as a hardware feature. Likely a hard wall."
    fi
}

step_app() {
    info "Installing the PowerView APK"
    note "Waydroid was initialised without Google Play, so the app is"
    note "sideloaded. Get the APK from a source you trust and check the"
    note "signing certificate matches the vendor's official builds."
    echo

    local apk
    ask_path "Path to the APK (Tab completes): " apk
    [ -f "$apk" ] || { fail "No such file: $apk"; return 1; }

    inspect_apk "$apk"
    echo

    # An upgrade over an existing lower version PRESERVES the data directory.
    # Uninstalling wipes it - including a working login.
    if sudo waydroid shell -- pm list packages 2>/dev/null | grep -q "$PKG"; then
        warn "$PKG is already installed."
        note "Installing a NEWER version over the top keeps the app's data,"
        note "including any existing login. Uninstalling first destroys it."
        if confirm "Uninstall first (destroys existing app data)?" n; then
            sudo waydroid shell -- pm uninstall "$PKG"
        fi
    fi

    waydroid app install "$apk" || { fail "Install failed."; return 1; }
    ok "APK installed."

    if confirm "Pre-grant Bluetooth/location permissions?" y; then
        step_permissions
    fi
}

step_permissions() {
    info "Granting runtime permissions"
    note "Some apps check permission state rather than probing for a radio."
    note "Granting up front occasionally gets past a Bluetooth prompt. It is"
    note "not a substitute for a real adapter."

    local p
    for p in BLUETOOTH_SCAN BLUETOOTH_CONNECT ACCESS_FINE_LOCATION ACCESS_COARSE_LOCATION; do
        if sudo waydroid shell -- pm grant "$PKG" "android.permission.$p" 2>/dev/null; then
            ok "granted $p"
        else
            note "skipped $p (not declared by this build)"
        fi
    done

    sudo waydroid shell -- am force-stop "$PKG" 2>/dev/null
    ok "App force-stopped - reopen it from the launcher."
}

# ---------------------------------------------------------------------------
# Step 8: extract
# ---------------------------------------------------------------------------

# Locate the app's SQLite database by SCHEMA, not by filename. The filename is
# randomly generated per install (e.g. KEtGvcVK7At4J28dtIMZ_7), so any specific
# name found in a forum post belongs to that person's install, not yours.
find_home_db() {
    local dir="$1" f tables
    for f in "$dir"/databases/*; do
        [ -f "$f" ] || continue
        case "$f" in *-journal|*-wal|*-shm|*.lck) continue ;; esac
        tables=$(sqlite3 "$f" ".tables" 2>/dev/null) || continue
        if grep -qw homes <<<"$tables" && grep -qw gateways <<<"$tables"; then
            printf '%s' "$f"
            return 0
        fi
    done
    return 1
}

step_extract() {
    info "Extracting the home key"

    if ! sudo test -d "$DATA_DIR"; then
        fail "No app data at $DATA_DIR"
        note "The app writes its database only after signing in and syncing."
        note "Open the app, log in, and WAIT until your rooms and shades are"
        note "listed. Checking too early is the usual reason for an empty result."
        return 1
    fi

    mkdir -p "$OUTDIR"
    local dump
    dump="$OUTDIR/pv-dump-$(date +%Y%m%d-%H%M%S)"

    # Copy before reading. The live DB is in WAL mode, so reading it in place
    # risks writing into the container's data.
    sudo cp -r "$DATA_DIR" "$dump" || return 1
    sudo chown -R "$(id -u):$(id -g)" "$dump" || return 1
    chmod -R go-rwx "$dump"
    ok "Snapshot: $dump"

    local db
    if ! db=$(find_home_db "$dump"); then
        fail "No database with a 'homes' table found."
        note "Files present:"
        find "$dump/databases" -type f 2>/dev/null | sed 's/^/       /' | head -20
        echo
        note "If the only databases are Google/Firebase ones, the app has not"
        note "synced yet, or this app version stores config in shared_prefs."
        note "Checking shared_prefs for anything key-shaped:"
        grep -rhoE '[A-Fa-f0-9]{32}|[A-Za-z0-9_-]{24,}' "$dump/shared_prefs/" 2>/dev/null \
            | sort -u | head -10 | sed 's/^/       /'
        return 1
    fi
    ok "Database: $(basename "$db")"

    local report
    report="$OUTDIR/report-$(date +%Y%m%d-%H%M%S).txt"
    umask 077
    {
        echo "PowerView extraction - $(date -Is)"
        echo "Source: $db"
        echo
        echo "=== homes ==="
        sqlite3 -line "$db" "select * from homes;" 2>/dev/null
        echo
        echo "=== gateways ==="
        sqlite3 -line "$db" "select * from gateways;" 2>/dev/null
        echo
        echo "=== rooms ==="
        sqlite3 -line "$db" "select * from rooms;" 2>/dev/null
        echo
        echo "=== shades ==="
        sqlite3 -line "$db" "select * from shades;" 2>/dev/null
    } > "$report"
    chmod 600 "$report"

    # Pull out any column on `homes` whose name looks like a key.
    hr
    local cols col val found=0
    cols=$(sqlite3 "$db" "select name from pragma_table_info('homes');" 2>/dev/null)
    while IFS= read -r col; do
        [ -n "$col" ] || continue
        case "${col,,}" in
            *key*)
                val=$(sqlite3 "$db" "select \"$col\" from homes limit 1;" 2>/dev/null)
                if [ -n "$val" ]; then
                    printf '%s  %s%s = %s%s%s\n' "$C_BOLD" "$col" "$C_RESET" \
                           "$C_GREEN$C_BOLD" "$val" "$C_RESET"
                    found=1
                fi
                ;;
        esac
    done <<< "$cols"
    hr

    if [ "$found" -eq 0 ]; then
        warn "No column on 'homes' had 'key' in its name. Full schema:"
        sqlite3 "$db" ".schema homes" | sed 's/^/     /'
        note "Full table contents are in the report below."
    fi

    ok "Report: $report  (mode 600)"
    echo
    warn "That key is a credential. It authenticates control of your shades."
    note "Keep it out of public repos, pasted logs and screenshots. Rotating it"
    note "most likely means re-onboarding the gateway."

    if [ "$IS_LIVE" -eq 1 ]; then
        echo
        warn "You are on a live session - $OUTDIR is in RAM and dies on reboot."
        note "Run step 9 now to copy it to real storage."
    fi
}

# ---------------------------------------------------------------------------
# Step 9: snapshot to persistent storage
# ---------------------------------------------------------------------------

step_snapshot() {
    info "Copy results to persistent storage"

    [ -d "$OUTDIR" ] || { fail "Nothing at $OUTDIR yet - run step 8 first."; return 1; }

    lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINTS 2>/dev/null | sed 's/^/     /'
    echo
    local dest
    read -r -e -p "Destination directory (Tab completes): " dest
    dest="${dest/#\~/$HOME}"
    [ -n "$dest" ] || return 1

    sudo mkdir -p "$dest" || return 1
    sudo cp -r "$OUTDIR" "$dest/" || return 1
    ok "Copied to $dest/$(basename "$OUTDIR")"
    note "Also worth writing the key on paper. Paper survives reboots."
}

# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

step_status() {
    info "Current state"
    hr
    printf '     %-24s %s\n' "waydroid installed:" \
        "$(need_cmd waydroid && echo yes || echo no)"
    printf '     %-24s %s\n' "android image:" \
        "$([ -e /var/lib/waydroid/images/system.img ] && echo present || echo missing)"
    printf '     %-24s %s\n' "container service:" \
        "$(sudo systemctl is-active waydroid-container 2>/dev/null || echo inactive)"
    printf '     %-24s %s\n' "weston running:" \
        "$(pgrep -f "weston.*--socket=$WAYLAND_SOCK" >/dev/null && echo yes || echo no)"
    printf '     %-24s %s\n' "software rendering:" \
        "$(sudo grep -q swiftshader "$BASE_PROP" 2>/dev/null && echo on || echo off)"
    printf '     %-24s %s\n' "app installed:" \
        "$(sudo waydroid shell -- pm list packages 2>/dev/null | grep -q "$PKG" && echo yes || echo no)"
    printf '     %-24s %s\n' "app data present:" \
        "$(sudo test -d "$DATA_DIR" && echo yes || echo no)"
    hr
    waydroid status 2>/dev/null | sed 's/^/     /'
}

step_teardown() {
    info "Stopping session"
    waydroid session stop 2>/dev/null
    pkill -f "weston.*--socket=$WAYLAND_SOCK" 2>/dev/null
    ok "Session and compositor stopped. Extracted data is untouched."
}

# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------

run_all() {
    step_preflight || return 1
    echo; step_install  || return 1
    echo; step_storage  || true
    echo; step_init     || return 1
    echo; step_renderer || true
    echo
    info "Setup done. Android needs to boot before the app can be installed."
    note "Choose 6 to launch it, then 7 to install the APK, sign in, and only"
    note "then 8 to extract."
}

menu() {
    cat <<EOF

$C_BOLD  PowerView home key extraction via Waydroid$C_RESET

  Setup
    1) Preflight checks            (safe, read-only)
    2) Install Waydroid + helpers
    3) Point storage at a disk     (before step 4 - optional)
    4) Initialise Android          (~1GB download)
    5) Renderer settings           (needed on NVIDIA)

  Run
    6) Launch Android
    7) Install APK / grant permissions

  Extract
    8) Extract home key
    9) Copy results to real storage

  Other
    s) Status                      t) Stop session
    a) Run steps 1-5 in order      q) Quit

EOF
}

main() {
    [ "$(id -u)" -eq 0 ] && die "Do not run this as root - it calls sudo where needed."
    need_cmd sudo || die "sudo is required."

    printf '%s\n' "PowerView / Waydroid extraction helper"
    printf '%s\n' "Reads the home key from your own account's local app data."
    sudo_keepalive

    # Preflight sets flags the other steps rely on.
    step_preflight || warn "Preflight reported a problem - later steps may fail."

    local choice
    while true; do
        menu
        read -r -p "Choice: " choice
        echo
        case "$choice" in
            1) step_preflight ;;
            2) step_install ;;
            3) step_storage ;;
            4) step_init ;;
            5) step_renderer ;;
            6) if confirm "Run in background (keeps this menu usable)?" y; then
                   step_launch_bg
               else
                   step_launch
               fi ;;
            7) step_app ;;
            8) step_extract ;;
            9) step_snapshot ;;
            s|S) step_status ;;
            t|T) step_teardown ;;
            a|A) run_all ;;
            q|Q) echo "Bye."; exit 0 ;;
            *) warn "Unrecognised choice: $choice" ;;
        esac
        echo
        pause
    done
}

main "$@"
