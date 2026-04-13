#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE_DEFAULT="$SCRIPT_DIR/.gige.env"

# Default mapping for 3 dedicated NICs (override via env or CLI)
CAM1_NIC="${CAM1_NIC:-enp134s0}"
CAM2_NIC="${CAM2_NIC:-enp135s0}"
CAM3_NIC="${CAM3_NIC:-enp136s0}"

CAM1_HOST_IP_CIDR="${CAM1_HOST_IP_CIDR:-192.168.14.1/24}"
CAM2_HOST_IP_CIDR="${CAM2_HOST_IP_CIDR:-192.168.15.1/24}"
CAM3_HOST_IP_CIDR="${CAM3_HOST_IP_CIDR:-192.168.16.1/24}"

# Camera-side fixed IP (optional but strongly recommended for stable mapping)
CAM1_CAMERA_IP="${CAM1_CAMERA_IP:-192.168.14.2}"
CAM2_CAMERA_IP="${CAM2_CAMERA_IP:-192.168.15.2}"
CAM3_CAMERA_IP="${CAM3_CAMERA_IP:-192.168.16.2}"

# Optional fixed serials; if blank, auto-detection is attempted.
CAM1_SERIAL="${CAM1_SERIAL:-}"
CAM2_SERIAL="${CAM2_SERIAL:-}"
CAM3_SERIAL="${CAM3_SERIAL:-}"

DEFAULT_MODE="${DEFAULT_MODE:-max}"
DEFAULT_MARKER="${DEFAULT_MARKER:-aruco}"

ENV_FILE="$ENV_FILE_DEFAULT"
APPLY_NET=1
SET_RP_FILTER=1
AUTO_DETECT_SERIAL=1
AUTO_DISCOVER_SERIAL_BY_NIC=0
INSTALL_TISCAMERA=1
AUTO_INSTALL_SYSTEM_DEPS=1
TISCAMERA_DEB=""
TISCAMERA_INDEX_URL="${TISCAMERA_INDEX_URL:-https://dl.theimagingsource.com/8943050e-ec37-5fcb-8cdb-3f14e1fbe6e7/}"
BRING_UP=1
BRING_DOWN=0
SET_CAMERA_STATIC=1
CAMERA_NETMASK="${CAMERA_NETMASK:-255.255.255.0}"
CAMERA_GATEWAY="${CAMERA_GATEWAY:-0.0.0.0}"

usage() {
    cat <<USAGE
Usage: $(basename "$0") [options]

Purpose:
  Prepare and validate preconditions before running setup.sh for a 3-camera GigE setup.
  The script can apply host NIC IPs, verify tcamsrc/tcam-gigetool, auto-detect serials,
  set camera static IPs, and write a reusable env file (.gige.env).

Options:
  --bring-down                    Bring CAM1-3 NIC links down (and flush IP)
  --camera-netmask <mask>         Netmask for camera static IP setup (default: ${CAMERA_NETMASK})
  --camera-gateway <ip>           Gateway for camera static IP setup (default: ${CAMERA_GATEWAY})
  --no-rp-filter                  Do not set rp_filter=0
  --no-auto-serial                Disable serial auto-detection
  --no-nic-serial-discovery       Disable per-NIC isolated serial discovery
  --install-tiscamera             Install tiscamera deb when tcam-gigetool is missing (default: enabled)
  --no-auto-system-deps           Do not auto-install required system packages
  --tiscamera-deb <path>          .deb path used with --install-tiscamera
  --tiscamera-index-url <url>     Index URL to discover downloadable tiscamera deb
  --env-file <path>               Output env file (default: $ENV_FILE_DEFAULT)

  --cam1-nic <name>               Default: ${CAM1_NIC}
  --cam2-nic <name>               Default: ${CAM2_NIC}
  --cam3-nic <name>               Default: ${CAM3_NIC}

  --cam1-host-ip-cidr <cidr>      Default: ${CAM1_HOST_IP_CIDR}
  --cam2-host-ip-cidr <cidr>      Default: ${CAM2_HOST_IP_CIDR}
  --cam3-host-ip-cidr <cidr>      Default: ${CAM3_HOST_IP_CIDR}

  --cam1-camera-ip <ip>           Default: ${CAM1_CAMERA_IP}
  --cam2-camera-ip <ip>           Default: ${CAM2_CAMERA_IP}
  --cam3-camera-ip <ip>           Default: ${CAM3_CAMERA_IP}

  --cam1-serial <serial>          Optional fixed serial override
  --cam2-serial <serial>          Optional fixed serial override
  --cam3-serial <serial>          Optional fixed serial override

  -h, --help                      Show help

Examples:
  $(basename "$0")
  $(basename "$0") --cam1-serial 08520932 --cam2-serial 05620902 --cam3-serial 05620909
  $(basename "$0") --cam1-nic enp134s0 --cam2-nic enp135s0 --cam3-nic enp136s0
  $(basename "$0") --tiscamera-deb ./tiscamera_1.1.1.4142_amd64_ubuntu_1804.deb
USAGE
}

info() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*"; }
err() { printf '[ERROR] %s\n' "$*" >&2; }

require_cmd() {
    local cmd="$1"
    if ! command -v "$cmd" >/dev/null 2>&1; then
        err "Missing command: $cmd"
        return 1
    fi
    return 0
}

extract_serials_from_tcam_list() {
    awk '{
             for (i = 1; i <= NF; i++) {
                 if ($i ~ /^[0-9][0-9][0-9][0-9][0-9][0-9][0-9]*$/ && length($i) <= 16) print $i
             }
         }' | sort -u
}

extract_serials_in_order() {
    awk '{
             for (i = 1; i <= NF; i++) {
                 if ($i ~ /^[0-9][0-9][0-9][0-9][0-9][0-9][0-9]*$/ && length($i) <= 16) {
                     if (!seen[$i]++) {
                         print $i
                     }
                     break
                 }
             }
         }'
}

detect_serial_for_ip() {
    local list_output="$1"
    local ip="$2"
    [[ -z "$ip" ]] && return 0

    # Works for both table output and `--format si`.
    printf '%s\n' "$list_output" | awk -v ip="$ip" '
        {
            serial=""
            has_ip=0
            for (i = 1; i <= NF; i++) {
                if ($i ~ /^[0-9][0-9][0-9][0-9][0-9][0-9][0-9]*$/ && length($i) <= 16 && serial == "") {
                    serial=$i
                }
                if ($i == ip) {
                    has_ip=1
                }
            }
            if (has_ip && serial != "") {
                print serial
                exit
            }
        }'
}

ip_prefix24() {
    local ip="$1"
    printf '%s' "$ip" | awk -F. 'NF==4 { printf "%s.%s.%s", $1, $2, $3 }'
}

get_current_ip_for_serial() {
    local list_output="$1"
    local serial="$2"

    [[ -z "$serial" ]] && return 0

    # Works with `tcam-gigetool list --format siI`.
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

apply_nic_ip() {
    local nic="$1"
    local host_ip_cidr="$2"

    [[ -z "$nic" || -z "$host_ip_cidr" ]] && return 0

    info "Applying NIC IP: $nic <- $host_ip_cidr"
    sudo ip addr flush dev "$nic"
    sudo ip addr add "$host_ip_cidr" dev "$nic"

    if [[ "$SET_RP_FILTER" -eq 1 ]]; then
        sudo sysctl -w "net.ipv4.conf.${nic}.rp_filter=0" >/dev/null
    fi
}

set_nic_link_state() {
    local nic="$1"
    local state="$2"

    [[ -z "$nic" ]] && return 0

    if [[ "$state" == "down" ]]; then
        info "Bringing NIC down (with flush): $nic"
        sudo ip addr flush dev "$nic"
        sudo ip link set dev "$nic" down
    else
        info "Bringing NIC up: $nic"
        sudo ip link set dev "$nic" up
    fi
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
        warn "tcam-gigetool is missing (installation is disabled)"
        return 0
    fi

    if [[ -z "$TISCAMERA_DEB" ]]; then
        # Best-effort auto-discovery.
        if compgen -G "$PWD/tiscamera*.deb" >/dev/null 2>&1; then
            TISCAMERA_DEB="$(ls -1 "$PWD"/tiscamera*.deb 2>/dev/null | head -n1)"
        elif compgen -G "$SCRIPT_DIR/tiscamera*.deb" >/dev/null 2>&1; then
            TISCAMERA_DEB="$(ls -1 "$SCRIPT_DIR"/tiscamera*.deb 2>/dev/null | head -n1)"
        fi
    fi

    if [[ -z "$TISCAMERA_DEB" ]]; then
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
                    curl -fL "$deb_url" -o "$TISCAMERA_DEB"
                fi
            fi
        fi
    fi

    if [[ -z "$TISCAMERA_DEB" || ! -f "$TISCAMERA_DEB" ]]; then
        err "tcam-gigetool is missing and no tiscamera deb was found/downloaded. Set --tiscamera-deb <path>."
        exit 1
    fi

    info "Installing tiscamera from deb: $TISCAMERA_DEB"
    sudo apt update
    sudo apt install -y "$TISCAMERA_DEB"
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
    sudo apt update
    sudo apt install -y "${missing[@]}"
}

set_camera_static_ip() {
    local name="$1"
    local serial="$2"
    local target_ip="$3"

    if [[ -z "$serial" || -z "$target_ip" ]]; then
        warn "$name static IP skipped (serial or target IP is empty)"
        return 0
    fi

    info "$name setting static IP: serial=$serial -> $target_ip"
    if tcam-gigetool set --ip "$target_ip" --netmask "$CAMERA_NETMASK" --gateway "$CAMERA_GATEWAY" --mode static "$serial" >/dev/null 2>&1; then
        info "$name static IP set OK"
    else
        warn "$name static IP set failed (serial=$serial, target=$target_ip)"
    fi
}

set_camera_static_with_compat_nic() {
    local name="$1"
    local nic="$2"
    local host_ip_cidr="$3"
    local target_ip="$4"
    local serial="$5"
    local list_siI="$6"
    local current_ip=""
    local current_prefix=""
    local target_prefix=""
    local compat_ip=""

    if [[ -z "$serial" || -z "$target_ip" ]]; then
        warn "$name static IP skipped (serial or target IP is empty)"
        return 0
    fi

    current_ip="$(get_current_ip_for_serial "$list_siI" "$serial" || true)"
    target_prefix="$(ip_prefix24 "$target_ip")"
    current_prefix="$(ip_prefix24 "$current_ip")"

    # If current and target networks differ, add a temporary compatible host IP.
    if [[ -n "$current_prefix" && "$current_prefix" != "$target_prefix" ]]; then
        compat_ip="${current_prefix}.1"
        if [[ "$compat_ip" == "$current_ip" ]]; then
            compat_ip="${current_prefix}.254"
        fi

        if ! ip -4 addr show dev "$nic" | grep -Eq "\\b${compat_ip}/"; then
            info "$name adding temporary compatible host IP on $nic: ${compat_ip}/24"
            sudo ip addr add "${compat_ip}/24" dev "$nic"
        fi
    fi

    set_camera_static_ip "$name" "$serial" "$target_ip"

    # Keep only configured host CIDR as the final state for this NIC.
    if [[ -n "$host_ip_cidr" ]]; then
        sudo ip addr flush dev "$nic"
        sudo ip addr add "$host_ip_cidr" dev "$nic"
    fi
}

discover_serial_via_nic_isolation() {
    local name="$1"
    local nic="$2"
    local host_ip_cidr="$3"
    local camera_ip="$4"
    local other1="$5"
    local other2="$6"
    local discovered=""
    local list_output=""
    local serials=""
    local count="0"

    info "$name NIC-isolated serial discovery on $nic"

    # Isolate this NIC so discovery result maps to one camera.
    sudo ip link set dev "$other1" down >/dev/null 2>&1 || true
    sudo ip link set dev "$other2" down >/dev/null 2>&1 || true
    sudo ip link set dev "$nic" up >/dev/null 2>&1 || true

    # Keep host NIC address aligned during discovery.
    if [[ -n "$host_ip_cidr" ]]; then
        sudo ip addr flush dev "$nic" >/dev/null 2>&1 || true
        sudo ip addr add "$host_ip_cidr" dev "$nic" >/dev/null 2>&1 || true
    fi

    sleep 1
    list_output="$(tcam-gigetool list 2>/dev/null || true)"
    serials="$(printf '%s\n' "$list_output" | extract_serials_from_tcam_list || true)"
    count="$(printf '%s\n' "$serials" | sed '/^$/d' | wc -l | tr -d ' ')"

    if [[ "$count" == "1" ]]; then
        discovered="$(printf '%s\n' "$serials" | head -n1)"
        info "$name discovered serial=$discovered"
    elif [[ "$count" -gt 1 && -n "$camera_ip" ]]; then
        discovered="$(detect_serial_for_ip "$list_output" "$camera_ip" || true)"
        if [[ -n "$discovered" ]]; then
            info "$name discovered by camera IP match: serial=$discovered"
        fi
    fi

    # Bring all links back up for normal flow.
    sudo ip link set dev "$other1" up >/dev/null 2>&1 || true
    sudo ip link set dev "$other2" up >/dev/null 2>&1 || true
    sudo ip link set dev "$nic" up >/dev/null 2>&1 || true

    printf '%s' "$discovered"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bring-down) BRING_DOWN=1; BRING_UP=0; shift ;;
        --camera-netmask) CAMERA_NETMASK="${2:-}"; shift 2 ;;
        --camera-gateway) CAMERA_GATEWAY="${2:-}"; shift 2 ;;
        --no-rp-filter) SET_RP_FILTER=0; shift ;;
        --no-auto-serial) AUTO_DETECT_SERIAL=0; shift ;;
        --no-nic-serial-discovery) AUTO_DISCOVER_SERIAL_BY_NIC=0; shift ;;
        --no-auto-system-deps) AUTO_INSTALL_SYSTEM_DEPS=0; shift ;;
        --install-tiscamera) INSTALL_TISCAMERA=1; shift ;;
        --tiscamera-deb) TISCAMERA_DEB="${2:-}"; shift 2 ;;
        --tiscamera-index-url) TISCAMERA_INDEX_URL="${2:-}"; shift 2 ;;
        --env-file) ENV_FILE="${2:-}"; shift 2 ;;

        --cam1-nic) CAM1_NIC="${2:-}"; shift 2 ;;
        --cam2-nic) CAM2_NIC="${2:-}"; shift 2 ;;
        --cam3-nic) CAM3_NIC="${2:-}"; shift 2 ;;

        --cam1-host-ip-cidr) CAM1_HOST_IP_CIDR="${2:-}"; shift 2 ;;
        --cam2-host-ip-cidr) CAM2_HOST_IP_CIDR="${2:-}"; shift 2 ;;
        --cam3-host-ip-cidr) CAM3_HOST_IP_CIDR="${2:-}"; shift 2 ;;

        --cam1-camera-ip) CAM1_CAMERA_IP="${2:-}"; shift 2 ;;
        --cam2-camera-ip) CAM2_CAMERA_IP="${2:-}"; shift 2 ;;
        --cam3-camera-ip) CAM3_CAMERA_IP="${2:-}"; shift 2 ;;

        --cam1-serial) CAM1_SERIAL="${2:-}"; shift 2 ;;
        --cam2-serial) CAM2_SERIAL="${2:-}"; shift 2 ;;
        --cam3-serial) CAM3_SERIAL="${2:-}"; shift 2 ;;

        -h|--help) usage; exit 0 ;;
        *)
            err "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
done

info "GigE precondition setup (3 cameras)"

if [[ -r /etc/os-release ]]; then
    . /etc/os-release
    info "OS: ${PRETTY_NAME:-unknown}"
fi

require_cmd sudo || warn "sudo is missing; privileged steps may fail"

install_system_deps_if_needed || exit 1

require_cmd ip || exit 1
require_cmd ping || exit 1
require_cmd python3 || exit 1

install_tiscamera_if_needed

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

if [[ "$BRING_DOWN" -eq 1 ]]; then
    set_nic_link_state "$CAM1_NIC" "down"
    set_nic_link_state "$CAM2_NIC" "down"
    set_nic_link_state "$CAM3_NIC" "down"
fi

if [[ "$BRING_UP" -eq 1 ]]; then
    set_nic_link_state "$CAM1_NIC" "up"
    set_nic_link_state "$CAM2_NIC" "up"
    set_nic_link_state "$CAM3_NIC" "up"
fi

if [[ "$APPLY_NET" -eq 1 ]]; then
    apply_nic_ip "$CAM1_NIC" "$CAM1_HOST_IP_CIDR"
    apply_nic_ip "$CAM2_NIC" "$CAM2_HOST_IP_CIDR"
    apply_nic_ip "$CAM3_NIC" "$CAM3_HOST_IP_CIDR"

    if [[ "$SET_RP_FILTER" -eq 1 ]]; then
        info "Disabling global rp_filter"
        sudo sysctl -w net.ipv4.conf.all.rp_filter=0 >/dev/null
        sudo sysctl -w net.ipv4.conf.default.rp_filter=0 >/dev/null
    fi
else
    info "Network apply skipped"
fi

TLIST_OUTPUT=""
FORMAT_S_OUTPUT=""
FORMAT_SI_OUTPUT=""
ALL_SERIALS=""
ORDERED_SERIALS=""
if command -v tcam-gigetool >/dev/null 2>&1; then
    TLIST_OUTPUT="$(tcam-gigetool list 2>/dev/null || true)"
    FORMAT_S_OUTPUT="$(tcam-gigetool list --format s 2>/dev/null || true)"
    FORMAT_SI_OUTPUT="$(tcam-gigetool list --format si 2>/dev/null || true)"

    if [[ -n "$FORMAT_S_OUTPUT" ]]; then
        ORDERED_SERIALS="$(printf '%s\n' "$FORMAT_S_OUTPUT" | extract_serials_in_order || true)"
    fi

    if [[ -z "$ORDERED_SERIALS" && -n "$TLIST_OUTPUT" ]]; then
        # Fallback for older tcam-gigetool behavior.
        ORDERED_SERIALS="$(printf '%s\n' "$TLIST_OUTPUT" | extract_serials_in_order || true)"
    fi

    if [[ -n "$ORDERED_SERIALS" ]]; then
        ALL_SERIALS="$(printf '%s\n' "$ORDERED_SERIALS" | sed '/^$/d' | sort -u)"
    elif [[ -n "$TLIST_OUTPUT" ]]; then
        ALL_SERIALS="$(printf '%s\n' "$TLIST_OUTPUT" | extract_serials_from_tcam_list || true)"
    fi
fi

if [[ "$AUTO_DETECT_SERIAL" -eq 1 && -n "$TLIST_OUTPUT" ]]; then
    # Fast path: use the first 3 rows from tcam-gigetool list.
    mapfile -t ordered_pool < <(printf '%s\n' "$ORDERED_SERIALS" | sed '/^$/d' | head -n3)
    if [[ -z "$CAM1_SERIAL" && ${#ordered_pool[@]} -ge 1 ]]; then
        CAM1_SERIAL="${ordered_pool[0]}"
    fi
    if [[ -z "$CAM2_SERIAL" && ${#ordered_pool[@]} -ge 2 ]]; then
        CAM2_SERIAL="${ordered_pool[1]}"
    fi
    if [[ -z "$CAM3_SERIAL" && ${#ordered_pool[@]} -ge 3 ]]; then
        CAM3_SERIAL="${ordered_pool[2]}"
    fi

    if [[ -z "$CAM1_SERIAL" ]]; then
        CAM1_SERIAL="$(detect_serial_for_ip "${FORMAT_SI_OUTPUT:-$TLIST_OUTPUT}" "$CAM1_CAMERA_IP" || true)"
    fi
    if [[ -z "$CAM2_SERIAL" ]]; then
        CAM2_SERIAL="$(detect_serial_for_ip "${FORMAT_SI_OUTPUT:-$TLIST_OUTPUT}" "$CAM2_CAMERA_IP" || true)"
    fi
    if [[ -z "$CAM3_SERIAL" ]]; then
        CAM3_SERIAL="$(detect_serial_for_ip "${FORMAT_SI_OUTPUT:-$TLIST_OUTPUT}" "$CAM3_CAMERA_IP" || true)"
    fi

    # Fallback: if still missing and total discovered is exactly one per missing slot, fill in order.
    unresolved=0
    [[ -z "$CAM1_SERIAL" ]] && unresolved=$((unresolved + 1))
    [[ -z "$CAM2_SERIAL" ]] && unresolved=$((unresolved + 1))
    [[ -z "$CAM3_SERIAL" ]] && unresolved=$((unresolved + 1))

    if [[ "$unresolved" == "1" && -n "$ALL_SERIALS" ]]; then
        mapfile -t serial_pool < <(printf '%s\n' "$ALL_SERIALS" | sed '/^$/d')

        assign_next_serial() {
            local current="$1"
            if [[ -n "$current" ]]; then
                printf '%s' "$current"
                return
            fi

            while [[ ${#serial_pool[@]} -gt 0 ]]; do
                candidate="${serial_pool[0]}"
                serial_pool=("${serial_pool[@]:1}")
                if [[ "$candidate" != "$CAM1_SERIAL" && "$candidate" != "$CAM2_SERIAL" && "$candidate" != "$CAM3_SERIAL" ]]; then
                    printf '%s' "$candidate"
                    return
                fi
            done
            printf ''
        }

        CAM1_SERIAL="$(assign_next_serial "$CAM1_SERIAL")"
        CAM2_SERIAL="$(assign_next_serial "$CAM2_SERIAL")"
        CAM3_SERIAL="$(assign_next_serial "$CAM3_SERIAL")"
    elif [[ "$unresolved" -gt 1 ]]; then
        warn "Multiple serials unresolved; skipping order-based fallback assignment"
    fi
fi

if [[ "$AUTO_DETECT_SERIAL" -eq 1 && "$AUTO_DISCOVER_SERIAL_BY_NIC" -eq 1 ]]; then
    if [[ -z "$CAM1_SERIAL" ]]; then
        CAM1_SERIAL="$(discover_serial_via_nic_isolation "CAM1" "$CAM1_NIC" "$CAM1_HOST_IP_CIDR" "$CAM1_CAMERA_IP" "$CAM2_NIC" "$CAM3_NIC" || true)"
    fi
    if [[ -z "$CAM2_SERIAL" ]]; then
        CAM2_SERIAL="$(discover_serial_via_nic_isolation "CAM2" "$CAM2_NIC" "$CAM2_HOST_IP_CIDR" "$CAM2_CAMERA_IP" "$CAM1_NIC" "$CAM3_NIC" || true)"
    fi
    if [[ -z "$CAM3_SERIAL" ]]; then
        CAM3_SERIAL="$(discover_serial_via_nic_isolation "CAM3" "$CAM3_NIC" "$CAM3_HOST_IP_CIDR" "$CAM3_CAMERA_IP" "$CAM1_NIC" "$CAM2_NIC" || true)"
    fi
fi

if [[ "$SET_CAMERA_STATIC" -eq 1 ]]; then
    if ! command -v tcam-gigetool >/dev/null 2>&1; then
        err "camera static IP setup requires tcam-gigetool"
        exit 1
    fi

    FORMAT_SI_OUTPUT="$(tcam-gigetool list --format siI 2>/dev/null || true)"

    set_camera_static_with_compat_nic "CAM1" "$CAM1_NIC" "$CAM1_HOST_IP_CIDR" "$CAM1_CAMERA_IP" "$CAM1_SERIAL" "$FORMAT_SI_OUTPUT"
    set_camera_static_with_compat_nic "CAM2" "$CAM2_NIC" "$CAM2_HOST_IP_CIDR" "$CAM2_CAMERA_IP" "$CAM2_SERIAL" "$FORMAT_SI_OUTPUT"
    set_camera_static_with_compat_nic "CAM3" "$CAM3_NIC" "$CAM3_HOST_IP_CIDR" "$CAM3_CAMERA_IP" "$CAM3_SERIAL" "$FORMAT_SI_OUTPUT"

    # Refresh list after potential readdressing.
    TLIST_OUTPUT="$(tcam-gigetool list 2>/dev/null || true)"
    if [[ -n "$TLIST_OUTPUT" ]]; then
        if [[ -z "$CAM1_SERIAL" ]]; then
            CAM1_SERIAL="$(detect_serial_for_ip "$TLIST_OUTPUT" "$CAM1_CAMERA_IP" || true)"
        fi
        if [[ -z "$CAM2_SERIAL" ]]; then
            CAM2_SERIAL="$(detect_serial_for_ip "$TLIST_OUTPUT" "$CAM2_CAMERA_IP" || true)"
        fi
        if [[ -z "$CAM3_SERIAL" ]]; then
            CAM3_SERIAL="$(detect_serial_for_ip "$TLIST_OUTPUT" "$CAM3_CAMERA_IP" || true)"
        fi
    fi
fi

# Connectivity checks
check_ping() {
    local name="$1"
    local host_cidr="$2"
    local camera_ip="$3"
    local host_ip

    [[ -z "$camera_ip" ]] && return 0
    host_ip="${host_cidr%%/*}"

    if ping -I "$host_ip" -c 2 -W 1 "$camera_ip" >/dev/null 2>&1; then
        info "$name ping OK: $camera_ip (source $host_ip)"
    else
        warn "$name ping NG: $camera_ip (source $host_ip)"
    fi
}

check_ping "CAM1" "$CAM1_HOST_IP_CIDR" "$CAM1_CAMERA_IP"
check_ping "CAM2" "$CAM2_HOST_IP_CIDR" "$CAM2_CAMERA_IP"
check_ping "CAM3" "$CAM3_HOST_IP_CIDR" "$CAM3_CAMERA_IP"

mkdir -p "$(dirname "$ENV_FILE")"
cat > "$ENV_FILE" <<ENV
# Generated by $(basename "$0")
# 3-camera GigE precondition map

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

echo
echo "=== setup.sh ip/serial ==="
echo "CAM1: NIC=${CAM1_NIC} HOST=${CAM1_HOST_IP_CIDR} CAM_IP=${CAM1_CAMERA_IP} SERIAL=${CAM1_SERIAL:-<unknown>}"
echo "CAM2: NIC=${CAM2_NIC} HOST=${CAM2_HOST_IP_CIDR} CAM_IP=${CAM2_CAMERA_IP} SERIAL=${CAM2_SERIAL:-<unknown>}"
echo "CAM3: NIC=${CAM3_NIC} HOST=${CAM3_HOST_IP_CIDR} CAM_IP=${CAM3_CAMERA_IP} SERIAL=${CAM3_SERIAL:-<unknown>}"

echo
echo "=== 次ステップ ==="
echo "source \"$ENV_FILE\""
echo "./setup.sh"
echo "source .venv/bin/activate"
echo "python viewer.py --serial \"\$CAM1_SERIAL\" --mode \"\$DEFAULT_MODE\""
echo "python continue_calib-gige.py --serial \"\$CAM1_SERIAL\" --mode \"\$DEFAULT_MODE\" --marker \"\$DEFAULT_MARKER\""
