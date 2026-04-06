#!/usr/bin/env bash
set -euo pipefail

# =========================================================
# 4-port GigE camera setup script
# Target mapping:
#   enp134s0 <-> serial 08520932 <-> 192.168.30.59
#   enp135s0 <-> serial 05620902 <-> 192.168.31.59
#   enp136s0 <-> serial 05620909 <-> 192.168.32.59
#   enp137s0 <-> serial 05620901 <-> 192.168.33.59
#
# What this script does:
#   1) Set PC NIC IPs
#   2) Add temporary link-local helper IPs
#   3) Rescue cameras onto target static IPs
#   4) Persist static IPs with tcam-gigetool set
#   5) Remove temporary helper IPs
#   6) Verify final state
#
# Notes:
#   - Assumes all 4 cameras are connected.
#   - Uses the serial mappings already identified.
#   - 05620902 may need rescue before set, so rescue is run for all 3 DFK 33GR0521 cameras.
# =========================================================

run() {
  echo "+ $*"
  "$@"
}

echo "===== Step 1: configure NIC static IPs ====="
run sudo ip addr flush dev enp134s0
run sudo ip addr add 192.168.30.1/24 dev enp134s0

run sudo ip addr flush dev enp135s0
run sudo ip addr add 192.168.31.1/24 dev enp135s0

run sudo ip addr flush dev enp136s0
run sudo ip addr add 192.168.32.1/24 dev enp136s0

run sudo ip addr flush dev enp137s0
run sudo ip addr add 192.168.33.1/24 dev enp137s0

echo "===== Step 2: add temporary link-local helper IPs ====="
run sudo ip addr add 169.254.200.1/16 dev enp135s0 || true
run sudo ip addr add 169.254.201.1/16 dev enp136s0 || true
run sudo ip addr add 169.254.202.1/16 dev enp137s0 || true

echo "===== Step 3: inspect current discovery status ====="
run tcam-gigetool list

echo "===== Step 4: rescue cameras to target static IPs ====="
run tcam-gigetool rescue 05620902 --ip 192.168.31.59 --netmask 255.255.255.0 --gateway 0.0.0.0 --yes
run tcam-gigetool rescue 05620909 --ip 192.168.32.59 --netmask 255.255.255.0 --gateway 0.0.0.0 --yes
run tcam-gigetool rescue 05620901 --ip 192.168.33.59 --netmask 255.255.255.0 --gateway 0.0.0.0 --yes

echo "===== Step 5: confirm rescued IPs are reachable ====="
run ping -I 192.168.31.1 -c 3 192.168.31.59
run ping -I 192.168.32.1 -c 3 192.168.32.59
run ping -I 192.168.33.1 -c 3 192.168.33.59

echo "===== Step 6: persist static IP settings ====="
run tcam-gigetool set 05620902 --ip 192.168.31.59 --netmask 255.255.255.0 --gateway 0.0.0.0 --mode static
run tcam-gigetool set 05620909 --ip 192.168.32.59 --netmask 255.255.255.0 --gateway 0.0.0.0 --mode static
run tcam-gigetool set 05620901 --ip 192.168.33.59 --netmask 255.255.255.0 --gateway 0.0.0.0 --mode static

# Optional: re-assert 08520932 persistent static config
run tcam-gigetool set 08520932 --ip 192.168.30.59 --netmask 255.255.255.0 --gateway 0.0.0.0 --mode static || true

echo "===== Step 7: remove temporary helper IPs ====="
run sudo ip addr del 169.254.200.1/16 dev enp135s0 || true
run sudo ip addr del 169.254.201.1/16 dev enp136s0 || true
run sudo ip addr del 169.254.202.1/16 dev enp137s0 || true

echo "===== Step 8: verify NIC configuration ====="
run ip addr show dev enp134s0
run ip addr show dev enp135s0
run ip addr show dev enp136s0
run ip addr show dev enp137s0

echo "===== Step 9: verify camera configuration ====="
run tcam-gigetool info 08520932
run tcam-gigetool info 05620902
run tcam-gigetool info 05620909
run tcam-gigetool info 05620901

echo "===== Step 10: final reachability checks ====="
run ping -I enp134s0 -c 3 192.168.30.59
run ping -I enp135s0 -c 3 192.168.31.59
run ping -I enp136s0 -c 3 192.168.32.59
run ping -I enp137s0 -c 3 192.168.33.59

echo "===== Step 11: final discovery table ====="
run tcam-gigetool list

echo "===== DONE ====="