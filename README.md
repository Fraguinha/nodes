# Nodes

Automated k3s HA cluster provisioning over Tailscale — Raspberry Pi 5 and Hetzner Cloud VMs.

## Prerequisites

- [Tailscale](https://tailscale.com/) access
- [`pass`](https://www.passwordstore.org/)
- `pass tailscale/authkey`
- `pass github/token` for the `init` role
- `pass wifi/ssid` for physical nodes
- `pass wifi/password` for physical nodes
- macOS with [Raspberry Pi Imager](https://www.raspberrypi.com/software/) for arm64
- `qemu-img` and `sgdisk` for amd64
- Authenticated [`hcloud`](https://github.com/hetznercloud/cli) CLI for cloud nodes

## Roles

| Role | k3s mode | Runs etcd | Purpose |
|---|---|---|---|
| `init` | server | yes | Bootstrap k3s and Flux |
| `join` | server | yes | Join the control plane |
| `agent` | agent | no | Run workloads |

- Use one `init` node.
- Keep the total number of `init` and `join` nodes odd.

## Usage

### Interactive

```bash
./nodes
```

### Physical nodes

```bash
./nodes provision physical
```

```bash
./nodes provision physical \
  --disk /dev/disk4 \
  --arch arm64 \
  --hostname a50passos \
  --role join \
  --server fraguinha
```

### Hetzner Cloud nodes

```bash
./nodes provision cloud
```

```bash
./nodes provision cloud \
  --hostname cloud-1 \
  --role join \
  --server fraguinha \
  --type cx23 \
  --location nbg1
```

```bash
./nodes remove \
  --hostname cloud-1 \
  --peer fraguinha
```

### Existing nodes

```bash
./nodes sync fraguinha a50passos livinvicta carolion
```

## Defaults

| Setting | Value |
|---|---|
| Ubuntu image | Latest LTS for physical nodes |
| Hetzner image | `ubuntu-24.04` |
| Hetzner type | `cx23` |
| Hetzner location | `nbg1` |
| Flux owner | `Fraguinha` |
| Flux repository | `flux` |
| Flux path | `clusters/k8s-cluster` |
| Wi-Fi route metric | `100` |
| Ethernet route metric | `200` |

## Example: 6-node cluster

```bash
./nodes provision physical --arch arm64 --hostname fraguinha --role init
./nodes provision physical --arch arm64 --hostname a50passos --role join --server fraguinha
./nodes provision physical --arch arm64 --hostname livinvicta --role join --server fraguinha
./nodes provision physical --arch amd64 --hostname carolion --role agent --server fraguinha
./nodes provision cloud --hostname cloud-1 --role join --server fraguinha
./nodes provision cloud --hostname cloud-2 --role join --server fraguinha
```
