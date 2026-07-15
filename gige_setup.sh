#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE_DEFAULT="$SCRIPT_DIR/.gige.env"

# ==============================================================================
# GigE camera precondition setup for The Imaging Source cameras
# ------------------------------------------------------------------------------
# Normal operation:
#   Edit only CAM1_SERIAL / CAM2_SERIAL / CAM3_SERIAL below.
#   Leave a serial empty to skip that camera slot completely.
#
# Slot defaults:
#   CAM1: enp134s0, host 192.168.14.1/24, camera 192.168.14.2
#   CAM2: enp135s0, host 192.168.15.1/24, camera 192.168.15.2
#   CAM3: enp136s0, host 192.168.16.1/24, camera 192.168.16.2
# ===============================================================================

CAM1_SERIAL="${CAM1_SERIAL:-21620595}"
CAM2_SERIAL="${CAM2_SERIAL:-08621016}"
CAM3_SERIAL="${CAM3_SERIAL:-}"

CAM1_NIC="${CAM1_NIC:-enp134s0}"
CAM2_NIC="${CAM2_NIC:-enp135s0}"
CAM3_NIC="${CAM3_NIC:-enp136s0}"

CAM1_HOST_IP_CIDR="${CAM1_HOST_IP_CIDR:-192.168.16.1/24}"
CAM2_HOST_IP_CIDR="${CAM2_HOST_IP_CIDR:-192.168.25.1/24}"
CAM3_HOST_IP_CIDR="${CAM3_HOST_IP_CIDR:-192.168.26.1/24}"

CAM1_CAMERA_IP="${CAM1_CAMERA_IP:-192.168.16.2}"
CAM2_CAMERA_IP="${CAM2_CAMERA_IP:-192.168.25.2}"
CAM3_CAMERA_IP="${CAM3_CAMERA_IP:-192.168.26.2}"

DEFAULT_MODE="${DEFAULT_MODE:-max}"
DEFAULT_MARKER="${DEFAULT_MARKER:-aruco}"

ENV_FILE="$ENV_FILE_DEFAULT"
CAMERA_NETMASK="${CAMERA_NETMASK:-255.255.255.0}"
CAMERA_GATEWAY="${CAMERA_GATEWAY:-0.0.0.0}"
TISCAMERA_DEB=""
TISCAMERA_INDEX_URL="${TISCAMERA_INDEX_URL:-https://dl.theimagingsource.com/8943050e-ec37-5fcb-8cdb-3f14e1fbe6e7/}"

APPLY_NET=1
BRING_UP=1
BRING_DOWN=0
SET_RP_FILTER=1
SET_CAMERA_STATIC=1
INSTALL_TISCAMERA=1
AUTO_INSTALL_SYSTEM_DEPS=1
DRY_RUN=0
DISCOVER_LINKS_ONLY=0
FORCE_RESCUE=0
SKIP_MISSING=1
ONLY_SLOT=""
PING_COUNT=2

info() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*"; }
err() { printf '[ERROR] %s\n' "$*" >&2; }

run() {
    if [[ "$DRY_RUN" -eq 1 ]]; then
        printf '[DRY-RUN]'
        printf ' %q' "$@"
        printf '\n'
    else
        "$@"
    fi
}

sudo_run() {
    run sudo "$@"
}

usage() {
    cat <<USAGE
Usage: $(basename "$0") [options]

Purpose:
  Prepare host NICs and TIS GigE camera static IPs.
  In normal use, edit only CAM1_SERIAL / CAM2_SERIAL / CAM3_SERIAL.
  Empty serials are skipped.

Default slot map:
  CAM1: ${CAM1_NIC}, host ${CAM1_HOST_IP_CIDR}, camera ${CAM1_CAMERA_IP}, serial ${CAM1_SERIAL:-<empty>}
  CAM2: ${CAM2_NIC}, host ${CAM2_HOST_IP_CIDR}, camera ${CAM2_CAMERA_IP}, serial ${CAM2_SERIAL:-<empty>}
  CAM3: ${CAM3_NIC}, host ${CAM3_HOST_IP_CIDR}, camera ${CAM3_CAMERA_IP}, serial ${CAM3_SERIAL:-<empty>}

Core options:
  --dry-run                       Print operations without changing the system
  --discover-links                Show NIC link/IP state and exit
  --only cam1|cam2|cam3           Process only one camera slot
  --skip-missing                  Skip missing/empty serials instead of failing (default)
  --no-skip-missing               Fail when a configured serial is not found
  --no-cam-static                 Configure NICs only; do not change camera IPs
  --force-rescue                  Use tcam-gigetool rescue before set
  --bring-down                    Bring configured NIC links down and flush IPs
  --no-apply-net                  Do not configure host NIC IPs
  --no-rp-filter                  Do not set rp_filter=0

Camera network options:
  --camera-netmask <mask>         Camera netmask, default ${CAMERA_NETMASK}
  --camera-gateway <ip>           Camera gateway, default ${CAMERA_GATEWAY}

System dependency options:
  --install-tiscamera             Install tiscamera deb when tcam-gigetool is missing (default)
  --no-install-tiscamera          Do not install tiscamera automatically
  --no-auto-system-deps           Do not auto-install missing system packages
  --tiscamera-deb <path>          .deb path used with --install-tiscamera
  --tiscamera-index-url <url>     Index URL to discover downloadable tiscamera deb

Output:
  --env-file <path>               Output env file, default ${ENV_FILE_DEFAULT}

Slot overrides:
  --cam1-serial <serial>          Default: ${CAM1_SERIAL:-<empty>}
  --cam2-serial <serial>          Default: ${CAM2_SERIAL:-<empty>}
  --cam3-serial <serial>          Default: ${CAM3_SERIAL:-<empty>}

  --cam1-nic <name>               Default: ${CAM1_NIC}
  --cam2-nic <name>               Default: ${CAM2_NIC}
  --cam3-nic <name>               Default: ${CAM3_NIC}

  --cam1-host-ip-cidr <cidr>      Default: ${CAM1_HOST_IP_CIDR}
  --cam2-host-ip-cidr <cidr>      Default: ${CAM2_HOST_IP_CIDR}
  --cam3-host-ip-cidr <cidr>      Default: ${CAM3_HOST_IP_CIDR}

  --cam1-camera-ip <ip>           Default: ${CAM1_CAMERA_IP}
  --cam2-camera-ip <ip>           Default: ${CAM2_CAMERA_IP}
  --cam3-camera-ip <ip>           Default: ${CAM3_CAMERA_IP}

Examples:
  $(basename "$0")
  $(basename "$0") --cam1-serial 16620659 --cam2-serial 08621016 --cam3-serial ""
  $(basename "$0") --only cam2 --force-rescue
  $(basename "$0") --discover-links
  $(basename "$0") --dry-run
USAGE
}

require_cmd() {
    local cmd="$1"
    if ! command -v "$cmd" >/dev/null 2>&1; then
        err "Missing command: $cmd"
        return 1
    fi
}

is_valid_slot() {
    case "${1,,}" in
        cam1|cam2|cam3|1|2|3) return 0 ;;
        *) return 1 ;;
    esac
}

normalize_slot() {
    case "${1,,}" in
        cam1|1) printf '1' ;;
        cam2|2) printf '2' ;;
        cam3|3) printf '3' ;;
        *) return 1 ;;
    esac
}

slot_name() {
    printf 'CAM%s' "$1"
}

get_slot_serial() {
    case "$1" in
        1) printf '%s' "$CAM1_SERIAL" ;;
        2) printf '%s' "$CAM2_SERIAL" ;;
        3) printf '%s' "$CAM3_SERIAL" ;;
        *) return 1 ;;
    esac
}

get_slot_nic() {
    case "$1" in
        1) printf '%s' "$CAM1_NIC" ;;
        2) printf '%s' "$CAM2_NIC" ;;
        3) printf '%s' "$CAM3_NIC" ;;
        *) return 1 ;;
    esac
}

get_slot_host_cidr() {
    case "$1" in
        1) printf '%s' "$CAM1_HOST_IP_CIDR" ;;
        2) printf '%s' "$CAM2_HOST_IP_CIDR" ;;
        3) printf '%s' "$CAM3_HOST_IP_CIDR" ;;
        *) return 1 ;;
    esac
}

get_slot_camera_ip() {
    case "$1" in
        1) printf '%s' "$CAM1_CAMERA_IP" ;;
        2) printf '%s' "$CAM2_CAMERA_IP" ;;
        3) printf '%s' "$CAM3_CAMERA_IP" ;;
        *) return 1 ;;
    esac
}

slot_enabled() {
    local slot="$1"
    local serial=""
    local only_norm=""

    serial="$(get_slot_serial "$slot")"

    if [[ -n "$ONLY_SLOT" ]]; then
        only_norm="$(normalize_slot "$ONLY_SLOT")"
        [[ "$slot" == "$only_norm" ]] || return 1
    fi

    [[ -n "$serial" ]] || return 1
}

for_enabled_slots() {
    local s=""
    for s in 1 2 3; do
        if slot_enabled "$s"; then
            printf '%s\n' "$s"
        fi
    done
}

ip_prefix24() {
    local ip="$1"
    printf '%s' "$ip" | awk -F. 'NF==4 { printf "%s.%s.%s", $1, $2, $3 }'
}

host_ip_from_cidr() {
    local cidr="$1"
    printf '%s' "${cidr%%/*}"
}

compatible_host_cidr_for_ip() {
    local camera_ip="$1"
    local prefix=""
    local host_ip=""

    prefix="$(ip_prefix24 "$camera_ip")"
    [[ -n "$prefix" ]] || return 1

    host_ip="${prefix}.1"
    if [[ "$host_ip" == "$camera_ip" ]]; then
        host_ip="${prefix}.254"
    fi
    printf '%s/24' "$host_ip"
}

extract_serials_from_tcam_list() {
    awk '{
             for (i = 1; i <= NF; i++) {
                 if ($i ~ /^[0-9][0-9][0-9][0-9][0-9][0-9][0-9]*$/ && length($i) <= 16) print $i
             }
         }' | sort -u
}

get_current_ip_for_serial() {
    local list_output="$1"
    local serial="$2"

    [[ -z "$serial" ]] && return 0

    printf '%s\n' "$list_output" | awk -v serial="$serial" '
        {
            if (index($0, serial) == 0) next
            found_serial=0
            for (i = 1; i <= NF; i++) {
                if ($i == serial) {
                    found_serial=1
                    continue
                }
                if (found_serial && split($i, oct, ".") == 4) {
                    print $i
                    exit
                }
            }
        }'
}

get_persistent_ip_for_serial() {
    local list_output="$1"
    local serial="$2"

    [[ -z "$serial" ]] && return 0

    printf '%s\n' "$list_output" | awk -v serial="$serial" '
        {
            if (index($0, serial) == 0) next
            found_serial=0
            ip_count=0
            for (i = 1; i <= NF; i++) {
                if ($i == serial) {
                    found_serial=1
                    continue
                }
                if (found_serial && split($i, oct, ".") == 4) {
                    ip_count++
                    if (ip_count == 2) {
                        print $i
                        exit
                    }
                }
            }
        }'
}

serial_exists_in_list() {
    local list_output="$1"
    local serial="$2"
    [[ -n "$serial" ]] || return 1
    printf '%s\n' "$list_output" | grep -Eq "(^|[[:space:]])${serial}([[:space:]]|$)"
}

refresh_tcam_sii() {
    if command -v tcam-gigetool >/dev/null 2>&1; then
        tcam-gigetool list --format siI 2>/dev/null || true
    fi
}

print_tcam_sii() {
    local out=""
    out="$(refresh_tcam_sii)"
    if [[ -n "$out" ]]; then
        printf '%s\n' "$out"
    else
        warn "No output from: tcam-gigetool list --format siI"
    fi
}

show_link_discovery() {
    local nic=""
    info "NIC link discovery"
    for nic in "$CAM1_NIC" "$CAM2_NIC" "$CAM3_NIC"; do
        [[ -n "$nic" ]] || continue
        echo "=== $nic ==="
        ip -br link show "$nic" 2>/dev/null || true
        ip -4 addr show "$nic" 2>/dev/null | grep -E 'inet ' || true
    done
    echo
    info "Current tcam-gigetool list --format siI"
    print_tcam_sii
}

install_system_deps_if_needed() {
    local missing=()

    command -v ip >/dev/null 2>&1 || missing+=("iproute2")
    command -v ping >/dev/null 2>&1 || missing+=("iputils-ping")
    command -v python3 >/dev/null 2>&1 || missing+=("python3")
    command -v gst-inspect-1.0 >/dev/null 2>&1 || missing+=("gstreamer1.0-tools")

    if [[ ${#missing[@]} -eq 0 ]]; then
        return 0
    fi

    if [[ "$AUTO_INSTALL_SYSTEM_DEPS" -ne 1 ]]; then
        err "Missing system packages: ${missing[*]}"
        return 1
    fi

    info "Installing missing system packages: ${missing[*]}"
    sudo_run apt update
    sudo_run apt install -y "${missing[@]}"
}

install_tiscamera_if_needed() {
    local page=""
    local deb_url=""
    local deb_name=""

    if command -v tcam-gigetool >/dev/null 2>&1; then
        info "tcam-gigetool already installed"
        return 0
    fi

    if [[ "$INSTALL_TISCAMERA" -ne 1 ]]; then
        warn "tcam-gigetool is missing; automatic installation is disabled"
        return 0
    fi

    if [[ -z "$TISCAMERA_DEB" ]]; then
        if compgen -G "$PWD/tiscamera*.deb" >/dev/null 2>&1; then
            TISCAMERA_DEB="$(ls -1 "$PWD"/tiscamera*.deb 2>/dev/null | head -n1)"
        elif compgen -G "$SCRIPT_DIR/tiscamera*.deb" >/dev/null 2>&1; then
            TISCAMERA_DEB="$(ls -1 "$SCRIPT_DIR"/tiscamera*.deb 2>/dev/null | head -n1)"
        fi
    fi

    if [[ -z "$TISCAMERA_DEB" && "$DRY_RUN" -eq 0 ]]; then
        if command -v curl >/dev/null 2>&1; then
            info "Attempting to discover tiscamera deb from: $TISCAMERA_INDEX_URL"
            page="$(curl -fsSL "$TISCAMERA_INDEX_URL" || true)"
            if [[ -n "$page" ]]; then
                deb_url="$(
                    printf '%s\n' "$page" \
                    | grep -Eo 'https?://[^"'"'"' ]+\.deb|href="[^"]+\.deb"' \
                    | sed -E 's/^href="([^"]+)"$/\1/' \
                    | sed -E "s#^/#https://dl.theimagingsource.com/#" \
                    | sed -E "s#^([^h].*)$#${TISCAMERA_INDEX_URL}\1#" \
                    | grep -E 'tiscamera.*amd64.*\.deb' \
                    | grep -E 'ubuntu_2204|ubuntu_1804|ubuntu' \
                    | head -n1
                )"
                if [[ -n "$deb_url" ]]; then
                    deb_name="$(basename "$deb_url")"
                    TISCAMERA_DEB="/tmp/$deb_name"
                    info "Downloading tiscamera deb: $deb_url"
                    run curl -fL "$deb_url" -o "$TISCAMERA_DEB"
                fi
            fi
        fi
    fi

    if [[ "$DRY_RUN" -eq 1 && -z "$TISCAMERA_DEB" ]]; then
        warn "tcam-gigetool is missing; dry-run cannot download/install tiscamera"
        return 0
    fi

    if [[ -z "$TISCAMERA_DEB" || ! -f "$TISCAMERA_DEB" ]]; then
        err "tcam-gigetool is missing and no tiscamera deb was found/downloaded. Set --tiscamera-deb <path>."
        exit 1
    fi

    info "Installing tiscamera from deb: $TISCAMERA_DEB"
    sudo_run apt update
    sudo_run apt install -y "$TISCAMERA_DEB"
}

bring_link() {
    local nic="$1"
    local state="$2"

    [[ -n "$nic" ]] || return 0

    if [[ "$state" == "down" ]]; then
        info "Bringing NIC down with IP flush: $nic"
        sudo_run ip addr flush dev "$nic"
        sudo_run ip link set dev "$nic" down
    else
        info "Bringing NIC up: $nic"
        sudo_run ip link set dev "$nic" up
    fi
}

apply_host_ip_for_slot() {
    local slot="$1"
    local name=""
    local nic=""
    local host_cidr=""

    name="$(slot_name "$slot")"
    nic="$(get_slot_nic "$slot")"
    host_cidr="$(get_slot_host_cidr "$slot")"

    if [[ -z "$nic" || -z "$host_cidr" ]]; then
        warn "$name host IP skipped: NIC or host CIDR is empty"
        return 0
    fi

    info "$name applying NIC IP: $nic <- $host_cidr"
    sudo_run ip addr flush dev "$nic"
    sudo_run ip addr add "$host_cidr" dev "$nic"
    if [[ "$SET_RP_FILTER" -eq 1 ]]; then
        sudo_run sysctl -w "net.ipv4.conf.${nic}.rp_filter=0" >/dev/null
    fi
}

add_compat_ip_to_slot_nic() {
    local slot="$1"
    local current_ip="$2"
    local nic=""
    local compat_cidr=""

    nic="$(get_slot_nic "$slot")"
    compat_cidr="$(compatible_host_cidr_for_ip "$current_ip")"

    if ip -4 addr show dev "$nic" 2>/dev/null | grep -Eq "\b${compat_cidr%/*}/"; then
        info "$(slot_name "$slot") temporary compatible host IP already exists on $nic: $compat_cidr"
        return 0
    fi

    info "$(slot_name "$slot") adding temporary compatible host IP on $nic: $compat_cidr"
    sudo_run ip addr add "$compat_cidr" dev "$nic"
}

restore_slot_host_ip() {
    local slot="$1"
    local nic=""
    local host_cidr=""

    nic="$(get_slot_nic "$slot")"
    host_cidr="$(get_slot_host_cidr "$slot")"
    [[ -n "$nic" && -n "$host_cidr" ]] || return 0

    info "$(slot_name "$slot") restoring final host IP on $nic: $host_cidr"
    sudo_run ip addr flush dev "$nic"
    sudo_run ip addr add "$host_cidr" dev "$nic"
    if [[ "$SET_RP_FILTER" -eq 1 ]]; then
        sudo_run sysctl -w "net.ipv4.conf.${nic}.rp_filter=0" >/dev/null
    fi
}

ensure_serial_visible_or_skip() {
    local name="$1"
    local serial="$2"
    local list_sii="$3"

    if serial_exists_in_list "$list_sii" "$serial"; then
        return 0
    fi

    if [[ "$SKIP_MISSING" -eq 1 ]]; then
        warn "$name serial not visible; skipping: $serial"
        return 1
    fi

    err "$name serial not visible: $serial"
    exit 1
}

set_camera_by_set() {
    local name="$1"
    local serial="$2"
    local target_ip="$3"

    info "$name running tcam-gigetool set: serial=$serial -> $target_ip"
    if [[ "$DRY_RUN" -eq 1 ]]; then
        run tcam-gigetool set --ip "$target_ip" --netmask "$CAMERA_NETMASK" --gateway "$CAMERA_GATEWAY" --mode static "$serial"
        return 0
    fi

    if tcam-gigetool set --ip "$target_ip" --netmask "$CAMERA_NETMASK" --gateway "$CAMERA_GATEWAY" --mode static "$serial" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

set_camera_by_rescue() {
    local name="$1"
    local serial="$2"
    local target_ip="$3"

    info "$name running tcam-gigetool rescue: serial=$serial -> $target_ip"
    if [[ "$DRY_RUN" -eq 1 ]]; then
        run tcam-gigetool rescue "$serial" --ip "$target_ip" --netmask "$CAMERA_NETMASK" --gateway "$CAMERA_GATEWAY" --yes
        return 0
    fi

    if tcam-gigetool rescue "$serial" --ip "$target_ip" --netmask "$CAMERA_NETMASK" --gateway "$CAMERA_GATEWAY" --yes >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

verify_camera_ip_state() {
    local name="$1"
    local serial="$2"
    local target_ip="$3"
    local list_sii=""
    local current_ip=""
    local persistent_ip=""

    if [[ "$DRY_RUN" -eq 1 ]]; then
        warn "$name verification skipped in dry-run"
        return 0
    fi

    sleep 1
    list_sii="$(refresh_tcam_sii)"
    current_ip="$(get_current_ip_for_serial "$list_sii" "$serial" || true)"
    persistent_ip="$(get_persistent_ip_for_serial "$list_sii" "$serial" || true)"

    if [[ "$current_ip" == "$target_ip" ]]; then
        info "$name current IP verified: $serial -> $current_ip"
    else
        warn "$name current IP not verified: serial=$serial expected=$target_ip actual=${current_ip:-<missing>}"
        return 1
    fi

    if [[ "$persistent_ip" == "$target_ip" ]]; then
        info "$name persistent IP verified: $serial -> $persistent_ip"
    elif [[ -z "$persistent_ip" ]]; then
        warn "$name persistent IP is empty; current IP is usable but may not survive power cycle"
    else
        warn "$name persistent IP mismatch: expected=$target_ip actual=$persistent_ip"
    fi

    return 0
}

configure_camera_slot() {
    local slot="$1"
    local name=""
    local serial=""
    local nic=""
    local host_cidr=""
    local target_ip=""
    local list_sii=""
    local current_ip=""
    local current_prefix=""
    local target_prefix=""
    local ok=0

    name="$(slot_name "$slot")"
    serial="$(get_slot_serial "$slot")"
    nic="$(get_slot_nic "$slot")"
    host_cidr="$(get_slot_host_cidr "$slot")"
    target_ip="$(get_slot_camera_ip "$slot")"

    if [[ -z "$serial" ]]; then
        info "$name skipped: serial is empty"
        return 0
    fi
    if [[ -z "$nic" || -z "$host_cidr" || -z "$target_ip" ]]; then
        warn "$name skipped: NIC, host CIDR, or target camera IP is empty"
        return 0
    fi

    list_sii="$(refresh_tcam_sii)"
    ensure_serial_visible_or_skip "$name" "$serial" "$list_sii" || return 0

    current_ip="$(get_current_ip_for_serial "$list_sii" "$serial" || true)"
    current_prefix="$(ip_prefix24 "$current_ip")"
    target_prefix="$(ip_prefix24 "$target_ip")"

    info "$name target: serial=$serial nic=$nic host=$host_cidr camera=$target_ip current=${current_ip:-<unknown>}"

    # When the camera is currently on another /24, add that current /24 only to
    # the target NIC. Do not assign it to other NICs; otherwise Linux routing can
    # select the wrong interface.
    if [[ -n "$current_prefix" && -n "$target_prefix" && "$current_prefix" != "$target_prefix" ]]; then
        add_compat_ip_to_slot_nic "$slot" "$current_ip"
    fi

    if [[ "$FORCE_RESCUE" -eq 1 ]]; then
        set_camera_by_rescue "$name" "$serial" "$target_ip" || warn "$name rescue failed"
    fi

    if set_camera_by_set "$name" "$serial" "$target_ip"; then
        ok=1
    else
        warn "$name set failed; trying rescue"
        if set_camera_by_rescue "$name" "$serial" "$target_ip"; then
            ok=1
        fi
    fi

    restore_slot_host_ip "$slot"

    if [[ "$ok" -ne 1 ]]; then
        warn "$name static IP command failed: serial=$serial target=$target_ip"
        return 0
    fi

    verify_camera_ip_state "$name" "$serial" "$target_ip" || true
}

check_ping_for_slot() {
    local slot="$1"
    local name=""
    local serial=""
    local nic=""
    local host_cidr=""
    local host_ip=""
    local camera_ip=""

    name="$(slot_name "$slot")"
    serial="$(get_slot_serial "$slot")"
    nic="$(get_slot_nic "$slot")"
    host_cidr="$(get_slot_host_cidr "$slot")"
    camera_ip="$(get_slot_camera_ip "$slot")"

    if [[ -z "$serial" || -z "$nic" || -z "$host_cidr" || -z "$camera_ip" ]]; then
        info "$name ping skipped"
        return 0
    fi

    host_ip="$(host_ip_from_cidr "$host_cidr")"
    if [[ "$DRY_RUN" -eq 1 ]]; then
        run ping -I "$nic" -c "$PING_COUNT" -W 1 "$camera_ip"
        return 0
    fi

    if ping -I "$nic" -c "$PING_COUNT" -W 1 "$camera_ip" >/dev/null 2>&1; then
        info "$name ping OK: $camera_ip via $nic source $host_ip"
    else
        warn "$name ping NG: $camera_ip via $nic source $host_ip"
    fi
}

write_env_file() {
    if [[ "$DRY_RUN" -eq 1 ]]; then
        info "Dry-run: env file would be written to $ENV_FILE"
    else
        mkdir -p "$(dirname "$ENV_FILE")"
        cat > "$ENV_FILE" <<ENV
# Generated by $(basename "$0")
# GigE camera precondition map

CAM1_NIC="${CAM1_NIC}"
CAM1_HOST_IP_CIDR="${CAM1_HOST_IP_CIDR}"
CAM1_CAMERA_IP="${CAM1_CAMERA_IP}"
CAM1_SERIAL="${CAM1_SERIAL}"

CAM2_NIC="${CAM2_NIC}"
CAM2_HOST_IP_CIDR="${CAM2_HOST_IP_CIDR}"
CAM2_CAMERA_IP="${CAM2_CAMERA_IP}"
CAM2_SERIAL="${CAM2_SERIAL}"

CAM3_NIC="${CAM3_NIC}"
CAM3_HOST_IP_CIDR="${CAM3_HOST_IP_CIDR}"
CAM3_CAMERA_IP="${CAM3_CAMERA_IP}"
CAM3_SERIAL="${CAM3_SERIAL}"

DEFAULT_MODE="${DEFAULT_MODE}"
DEFAULT_MARKER="${DEFAULT_MARKER}"
ENV
        info "Wrote env file: $ENV_FILE"
    fi
}

print_summary() {
    echo
    echo "=== setup.sh ip/serial ==="
    echo "CAM1: NIC=${CAM1_NIC:-<empty>} HOST=${CAM1_HOST_IP_CIDR:-<empty>} CAM_IP=${CAM1_CAMERA_IP:-<empty>} SERIAL=${CAM1_SERIAL:-<empty>}"
    echo "CAM2: NIC=${CAM2_NIC:-<empty>} HOST=${CAM2_HOST_IP_CIDR:-<empty>} CAM_IP=${CAM2_CAMERA_IP:-<empty>} SERIAL=${CAM2_SERIAL:-<empty>}"
    echo "CAM3: NIC=${CAM3_NIC:-<empty>} HOST=${CAM3_HOST_IP_CIDR:-<empty>} CAM_IP=${CAM3_CAMERA_IP:-<empty>} SERIAL=${CAM3_SERIAL:-<empty>}"

    echo
    echo "=== current tcam-gigetool list --format siI ==="
    print_tcam_sii || true

    echo
    echo "=== next steps ==="
    echo "source \"$ENV_FILE\""
    echo "./setup.sh"
    echo "source .venv/bin/activate"
    echo "python viewer.py --serial \"\$CAM1_SERIAL\" --mode \"\$DEFAULT_MODE\""
    echo "python continue_calib-gige.py --serial \"\$CAM1_SERIAL\" --mode \"\$DEFAULT_MODE\" --marker \"\$DEFAULT_MARKER\""
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --discover-links) DISCOVER_LINKS_ONLY=1; shift ;;
        --only)
            if ! is_valid_slot "${2:-}"; then
                err "--only expects cam1, cam2, or cam3"
                exit 1
            fi
            ONLY_SLOT="${2:-}"
            shift 2
            ;;
        --skip-missing) SKIP_MISSING=1; shift ;;
        --no-skip-missing) SKIP_MISSING=0; shift ;;
        --no-cam-static) SET_CAMERA_STATIC=0; shift ;;
        --force-rescue) FORCE_RESCUE=1; shift ;;
        --bring-down) BRING_DOWN=1; BRING_UP=0; shift ;;
        --no-apply-net) APPLY_NET=0; shift ;;
        --no-rp-filter) SET_RP_FILTER=0; shift ;;
        --camera-netmask) CAMERA_NETMASK="${2:-}"; shift 2 ;;
        --camera-gateway) CAMERA_GATEWAY="${2:-}"; shift 2 ;;
        --install-tiscamera) INSTALL_TISCAMERA=1; shift ;;
        --no-install-tiscamera) INSTALL_TISCAMERA=0; shift ;;
        --no-auto-system-deps) AUTO_INSTALL_SYSTEM_DEPS=0; shift ;;
        --tiscamera-deb) TISCAMERA_DEB="${2:-}"; shift 2 ;;
        --tiscamera-index-url) TISCAMERA_INDEX_URL="${2:-}"; shift 2 ;;
        --env-file) ENV_FILE="${2:-}"; shift 2 ;;

        --cam1-serial) CAM1_SERIAL="${2:-}"; shift 2 ;;
        --cam2-serial) CAM2_SERIAL="${2:-}"; shift 2 ;;
        --cam3-serial) CAM3_SERIAL="${2:-}"; shift 2 ;;

        --cam1-nic) CAM1_NIC="${2:-}"; shift 2 ;;
        --cam2-nic) CAM2_NIC="${2:-}"; shift 2 ;;
        --cam3-nic) CAM3_NIC="${2:-}"; shift 2 ;;

        --cam1-host-ip-cidr) CAM1_HOST_IP_CIDR="${2:-}"; shift 2 ;;
        --cam2-host-ip-cidr) CAM2_HOST_IP_CIDR="${2:-}"; shift 2 ;;
        --cam3-host-ip-cidr) CAM3_HOST_IP_CIDR="${2:-}"; shift 2 ;;

        --cam1-camera-ip) CAM1_CAMERA_IP="${2:-}"; shift 2 ;;
        --cam2-camera-ip) CAM2_CAMERA_IP="${2:-}"; shift 2 ;;
        --cam3-camera-ip) CAM3_CAMERA_IP="${2:-}"; shift 2 ;;

        -h|--help) usage; exit 0 ;;
        *)
            err "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
done

info "GigE precondition setup"

if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    info "OS: ${PRETTY_NAME:-unknown}"
fi

if [[ "$DISCOVER_LINKS_ONLY" -eq 1 ]]; then
    require_cmd ip || exit 1
    show_link_discovery
    exit 0
fi

require_cmd sudo || warn "sudo is missing; privileged steps may fail"
install_system_deps_if_needed || exit 1
require_cmd ip || exit 1
require_cmd ping || exit 1
require_cmd python3 || exit 1

install_tiscamera_if_needed

if ! command -v tcam-gigetool >/dev/null 2>&1 && [[ "$DRY_RUN" -eq 0 ]]; then
    err "tcam-gigetool is required"
    exit 1
fi

if command -v gst-inspect-1.0 >/dev/null 2>&1; then
    if gst-inspect-1.0 tcamsrc >/dev/null 2>&1; then
        info "GStreamer tcamsrc: available"
    else
        err "tcamsrc is not available. Verify tiscamera installation."
        exit 1
    fi
else
    err "gst-inspect-1.0 is missing"
    exit 1
fi

mapfile -t ENABLED_SLOTS < <(for_enabled_slots)
if [[ ${#ENABLED_SLOTS[@]} -eq 0 ]]; then
    warn "No enabled camera slots. Set at least one CAM*_SERIAL or pass --camN-serial."
fi

if [[ "$BRING_DOWN" -eq 1 ]]; then
    for slot in "${ENABLED_SLOTS[@]}"; do
        bring_link "$(get_slot_nic "$slot")" down
    done
fi

if [[ "$BRING_UP" -eq 1 ]]; then
    for slot in "${ENABLED_SLOTS[@]}"; do
        bring_link "$(get_slot_nic "$slot")" up
    done
fi

if [[ "$APPLY_NET" -eq 1 ]]; then
    for slot in "${ENABLED_SLOTS[@]}"; do
        apply_host_ip_for_slot "$slot"
    done

    if [[ "$SET_RP_FILTER" -eq 1 ]]; then
        info "Disabling global rp_filter"
        sudo_run sysctl -w net.ipv4.conf.all.rp_filter=0 >/dev/null
        sudo_run sysctl -w net.ipv4.conf.default.rp_filter=0 >/dev/null
    fi
else
    info "Network apply skipped"
fi

if [[ "$SET_CAMERA_STATIC" -eq 1 ]]; then
    for slot in "${ENABLED_SLOTS[@]}"; do
        configure_camera_slot "$slot"
    done
else
    info "Camera static IP setup skipped"
fi

for slot in "${ENABLED_SLOTS[@]}"; do
    check_ping_for_slot "$slot"
done

write_env_file
print_summary
