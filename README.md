# Nodes

Automated k3s HA cluster provisioning over Tailscale — Raspberry Pi 5 and Hetzner Cloud VMs.

## Prerequisites

- [Tailscale](https://tailscale.com/) access
- [`pass`](https://www.passwordstore.org/)
- `pass tailscale/authkey`
- `pass github/token` for the `init` role
- `pass wifi/ssid` for physical nodes
- `pass wifi/password` for physical nodes
- macOS with [`qemu-img`](https://www.qemu.org/docs/master/tools/qemu-img.html) for all physical nodes
- `xz` for arm64 images
- `sgdisk` for amd64 images
- Authenticated [`hcloud`](https://github.com/hetznercloud/cli) CLI for cloud nodes

## Roles

| Role    | k3s mode | Runs etcd | Purpose                |
| ------- | -------- | --------- | ---------------------- |
| `init`  | server   | yes       | Bootstrap k3s and Flux |
| `join`  | server   | yes       | Join the control plane |
| `agent` | agent    | no        | Run workloads          |

- Use one `init` node.
- Keep the total number of `init` and `join` nodes odd.

## Usage

### Interactive

```bash
./nodes
```

### Physical nodes

Connect the SSD to your Mac and run:

```bash
./nodes provision physical
```

Select the whole SSD (`/dev/diskN`, not a partition) and its architecture. Flashing overwrites existing data but does not securely erase old SSD contents.

- **Raspberry Pi 5:** NVMe SSD on an M.2 HAT+. For Ubuntu 26.04, boot EEPROM must be dated `2025-02-11` or newer, and `BOOT_ORDER` must include `6` (NVMe).
- **AMD64:** Connect wired Ethernet for first boot.

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

## Example: 6-node cluster

```bash
./nodes provision physical --arch arm64 --hostname fraguinha --role init
./nodes provision physical --arch arm64 --hostname a50passos --role join --server fraguinha
./nodes provision physical --arch arm64 --hostname livinvicta --role join --server fraguinha
./nodes provision physical --arch amd64 --hostname carolion --role agent --server fraguinha
./nodes provision cloud --hostname cloud-1 --role join --server fraguinha
./nodes provision cloud --hostname cloud-2 --role join --server fraguinha
```
