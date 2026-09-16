# Nodes

Automated k3s HA cluster provisioning over Tailscale — Raspberry Pi 5 and Hetzner Cloud VMs.

## Prerequisites

- [Tailscale](https://tailscale.com/) running on the local machine
- `pass tailscale/authkey` — [reusable Tailscale auth key](https://login.tailscale.com/admin/settings/keys)
- `pass github/token` — GitHub PAT with `repo` scope (init only)
- macOS with [Raspberry Pi Imager](https://www.raspberrypi.com/software/) installed (arm64/RPi only)
- `qemu-img` and `sgdisk` — (amd64/x86 only)
- [`hcloud`](https://github.com/hetznercloud/cli) CLI authenticated (`hcloud context create`) (cloud only)

## Roles

| Role | k3s mode | Runs etcd | Purpose |
|---|---|---|---|
| `init` | server | yes | Bootstraps the cluster and Flux |
| `join` | server | yes | Adds a control plane peer |
| `agent` | agent | no | Adds a worker that only runs workloads |

Keep the number of `init` + `join` nodes odd so etcd can hold quorum. Use `agent` for
any node beyond that, or for hardware you do not want in the etcd voting set.

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

## Example: 6-node cluster (5 control plane + 1 worker)

```bash
# First RPi — bootstraps etcd + Flux
./flash.sh  # arch: arm64, hostname: fraguinha, role: init

# Second RPi — joins as control plane peer
./flash.sh  # arch: arm64, hostname: a50passos, role: join, server: fraguinha

# Third RPi — joins as control plane peer
./flash.sh  # arch: arm64, hostname: livinvicta, role: join, server: fraguinha

# x86 PC — joins as a worker, no etcd or control plane
./flash.sh  # arch: amd64, hostname: carolion, role: agent, server: fraguinha

# Cloud node 1 — joins as control plane peer
./up.sh  # hostname: cloud-1, server: fraguinha

# Cloud node 2 — joins as control plane peer
./up.sh  # hostname: cloud-2, server: fraguinha
```
