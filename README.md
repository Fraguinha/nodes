# Nodes

Automated k3s HA cluster provisioning over Tailscale — Raspberry Pi 5 and Hetzner Cloud VMs.

## Prerequisites

- [Tailscale](https://tailscale.com/) running on the local machine
- `pass tailscale/authkey` — [reusable Tailscale auth key](https://login.tailscale.com/admin/settings/keys)
- `pass github/token` — GitHub PAT with `repo` scope (init only)
- macOS with [Raspberry Pi Imager](https://www.raspberrypi.com/software/) installed (arm64/RPi only)
- `qemu-img` and `sgdisk` — (amd64/x86 only)
- [`hcloud`](https://github.com/hetznercloud/cli) CLI authenticated (`hcloud context create`) (cloud only)

## Usage

### Raspberry Pi and x86 nodes

```bash
./flash.sh
```

### Hetzner Cloud nodes

```bash
./up.sh    # create a node
./down.sh  # destroy a node
```

## Example: 5-node HA cluster

```bash
# First RPi — bootstraps etcd + Flux
./flash.sh  # arch: arm64, hostname: fraguinha, role: init

# Second RPi — joins as control plane peer
./flash.sh  # arch: arm64, hostname: a50passos, role: join, server: fraguinha

# Third RPi — joins as control plane peer
./flash.sh  # arch: arm64, hostname: livinvicta, role: join, server: fraguinha

# x86 PC — joins as control plane peer
./flash.sh  # arch: amd64, hostname: carolion, role: join, server: fraguinha

# Cloud node 1 — joins as control plane peer
./up.sh  # hostname: cloud-1, server: fraguinha

# Cloud node 2 — joins as control plane peer
./up.sh  # hostname: cloud-2, server: fraguinha
```
