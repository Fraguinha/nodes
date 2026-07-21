#!/usr/bin/env bash
set -euo pipefail

CONFIG=/etc/nodes/config
[[ -r "$CONFIG" ]] || { echo "Error: $CONFIG not found" >&2; exit 1; }
source "$CONFIG"

wait_for() {
  local description="$1"
  shift

  until "$@" >/dev/null 2>&1; do
    logger -t node-setup "waiting for $description"
    sleep 5
  done
}

retry_output() {
  local value=""

  while true; do
    if value=$("$@" 2>/dev/null) && [[ -n "$value" ]]; then
      printf '%s' "$value"
      return
    fi
    sleep 10
  done
}

resolve_peer_ip() {
  tailscale status --json | jq -er --arg hostname "$SERVER_HOSTNAME" \
    '[.Peer // {} | .[] | select(.HostName == $hostname) | .TailscaleIPs[] | select(contains(":") | not)][0] // empty'
}

wipe_file() {
  local path="$1"

  if [[ -f "$path" ]]; then
    : > "$path" 2>/dev/null || true
    rm -f "$path" || true
  fi
}

cleanup_seed_data() {
  local device=""
  local target=""
  local mounted=0

  wipe_file /boot/firmware/user-data
  wipe_file /boot/firmware/network-config
  wipe_file /boot/user-data
  wipe_file /boot/network-config

  device=$(blkid -L CIDATA 2>/dev/null || true)
  if [[ -b "$device" ]]; then
    target=$(findmnt -nr -S "$device" -o TARGET 2>/dev/null || true)
    if [[ -z "$target" ]]; then
      target=/run/nodes-cidata
      mkdir -p "$target"
      if mount "$device" "$target"; then
        mounted=1
      else
        target=""
      fi
    fi

    if [[ -n "$target" ]]; then
      wipe_file "$target/user-data"
      wipe_file "$target/network-config"
      sync
      if [[ "$mounted" == "1" ]]; then
        umount "$target"
        rmdir "$target"
      fi
    fi
  fi
}

cleanup_cloud_init_data() {
  local path=""

  for path in /var/lib/cloud/instance/user-data.txt /var/lib/cloud/instance/user-data.txt.i; do
    wipe_file "$path"
  done
  for path in /var/lib/cloud/seed/nocloud*/user-data /var/lib/cloud/seed/nocloud-net*/user-data; do
    wipe_file "$path"
  done
}

[[ "$NODE_ROLE" == "init" || "$NODE_ROLE" == "join" || "$NODE_ROLE" == "agent" ]] \
  || { echo "Error: invalid node role" >&2; exit 1; }
[[ "$NODE_PLATFORM" == "physical" || "$NODE_PLATFORM" == "cloud" ]] \
  || { echo "Error: invalid node platform" >&2; exit 1; }

wait_for "internet access" ping -c1 -W2 1.1.1.1

if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
systemctl enable --now tailscaled

if tailscale status --json 2>/dev/null | jq -e '.BackendState == "Running"' >/dev/null 2>&1; then
  tailscale set --ssh=true --hostname="$NODE_HOSTNAME"
else
  tailscale up --authkey="$TAILSCALE_AUTHKEY" --ssh --hostname="$NODE_HOSTNAME"
fi

wait_for "Tailscale" tailscale status
TAILSCALE_IP=$(tailscale ip -4 | head -n 1)
SERVER_IP=""
K3S_TOKEN=""

if [[ "$NODE_ROLE" != "init" ]]; then
  SERVER_IP=$(retry_output resolve_peer_ip)
  K3S_TOKEN=$(retry_output ssh \
    -o BatchMode=yes \
    -o ConnectTimeout=10 \
    -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile=/etc/nodes/known_hosts \
    "pi@$SERVER_IP" \
    "sudo cat /var/lib/rancher/k3s/server/node-token")
fi

K3S_COMMON_ARGS=(
  --node-ip "$TAILSCALE_IP"
  --node-external-ip "$TAILSCALE_IP"
  --flannel-iface tailscale0
  --resolv-conf /etc/k3s-resolv.conf
  --kubelet-arg "allowed-unsafe-sysctls=net.ipv4.ip_forward"
)

if [[ "$NODE_ROLE" == "agent" ]]; then
  K3S_ARGS=(
    agent
    --server "https://$SERVER_IP:6443"
    --token "$K3S_TOKEN"
    "${K3S_COMMON_ARGS[@]}"
  )
else
  K3S_ARGS=(
    server
    "${K3S_COMMON_ARGS[@]}"
    --secrets-encryption
    --write-kubeconfig-mode 0644
    --disable servicelb
    --etcd-arg heartbeat-interval=500
    --etcd-arg election-timeout=5000
    --tls-san "$TAILSCALE_IP"
  )

  if [[ "$NODE_ROLE" == "init" ]]; then
    K3S_ARGS+=(--cluster-init)
  else
    K3S_ARGS+=(--server "https://$SERVER_IP:6443" --token "$K3S_TOKEN")
  fi

  if [[ "$NODE_PLATFORM" == "physical" ]]; then
    LAN_IP=$(ip -4 route get 1.1.1.1 | sed -n 's/.*[[:space:]]src[[:space:]]\([0-9.]\+\).*/\1/p')
    [[ -n "$LAN_IP" ]] && K3S_ARGS+=(--tls-san "$LAN_IP")
    K3S_ARGS+=(--tls-san "$NODE_HOSTNAME.local")
  else
    PUBLIC_IP=$(curl -fsS --max-time 5 http://169.254.169.254/hetzner/v1/metadata/public-ipv4 \
      || curl -fsS --max-time 10 https://ifconfig.me/ip \
      || true)
    [[ -n "$PUBLIC_IP" ]] && K3S_ARGS+=(--tls-san "$PUBLIC_IP")
  fi
fi

sysctl --system >/dev/null
curl -fsSL https://get.k3s.io | INSTALL_K3S_CHANNEL=stable sh -s - "${K3S_ARGS[@]}"

if [[ "$NODE_ROLE" == "init" ]]; then
  export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
  wait_for "Kubernetes API" kubectl get nodes
  curl -fsSL https://fluxcd.io/install.sh | bash
  GITHUB_TOKEN="$GITHUB_TOKEN" flux bootstrap github \
    --owner=Fraguinha \
    --repository=flux \
    --branch=main \
    --path=clusters/k8s-cluster \
    --personal
fi

systemctl stop node-health.service >/dev/null 2>&1 || true
systemctl disable node-health.service >/dev/null 2>&1 || true
systemctl daemon-reload
systemctl enable --now watchdog-load.service
systemctl daemon-reexec
systemctl enable --now node-health.timer
systemctl restart systemd-journald
systemctl restart rsyslog
journalctl --vacuum-size=256M >/dev/null

if [[ "$NODE_PLATFORM" == "physical" ]] && ip link show wlan0 >/dev/null 2>&1; then
  iw dev wlan0 set power_save off || true
fi

chmod 600 /etc/netplan/50-cloud-init.yaml 2>/dev/null || true
cleanup_seed_data
cleanup_cloud_init_data
wipe_file "$CONFIG"
unset TAILSCALE_AUTHKEY GITHUB_TOKEN K3S_TOKEN

if [[ "$NODE_PLATFORM" == "physical" ]]; then
  touch /run/nodes-reboot
fi

logger -t node-setup "provisioning complete"
