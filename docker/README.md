# Docker

Ringbearer's image contains the application. Configuration, the Telegram
session, and captured transcripts stay in the bind-mounted `data/` directory.

## Setup

From the repository root:

```bash
cp docker/compose.example.yml docker/compose.yml
mkdir -p docker/data
```

The image runs as UID 1000. On Linux, if your user has a different UID, give
only this new state directory to the container user before onboarding:

```bash
sudo chown -R 1000:1000 docker/data
```

Docker Desktop handles bind-mounted ownership on macOS, so this step is not
normally needed there.

Edit `docker/compose.yml` or set `RINGBEARER_HOST` to the Docker host interface
that Pebble should reach. A Tailscale `100.x.x.x` address is recommended. The
checked-in default, `127.0.0.1`, is intentionally reachable only on the host.

Run onboarding inside the container:

```bash
docker compose -f docker/compose.yml run --rm --service-ports ringbearer
```

Enter `0.0.0.0` for `BIND_HOST`: that is the listener inside the container.
Configure Pebble with the host address selected above, not `0.0.0.0`. Setup
writes `.env`, the Telegram login writes `ringbearer.session`, and delivery
writes `captures.jsonl`; all remain under `docker/data/`.

Test the foreground bridge, press Ctrl-C, then start it in the background:

```bash
docker compose -f docker/compose.yml up -d
```

## Operations

```bash
docker compose -f docker/compose.yml logs -f
docker compose -f docker/compose.yml ps
docker compose -f docker/compose.yml down
docker compose -f docker/compose.yml build --pull
docker compose -f docker/compose.yml up -d
```

## Network exposure

Bind the published port to the host's Tailscale address when possible:

```yaml
ports:
  - "100.64.0.1:8787:8787" # replace with this host's Tailscale IP
```

A LAN address works only on that LAN and exposes the port to that network.
Do not omit the host IP, bind the host side to `0.0.0.0`, or forward this port
from the public internet.

## Existing state and deployment systems

To migrate an existing native installation, stop it first, then copy `.env`
and all `<SESSION_NAME>.session*` files into `docker/data/`. Never run native
and containerized Ringbearer against the same Telegram session simultaneously.

Deployment systems may render their own `.env` and mount any persistent
directory at `/data`; Compose is only an example.

The session file grants access to your Telegram account; protect the entire
state directory like a password. The bearer token gates MCP calls but does not
make public-internet exposure safe.

## Troubleshooting

- Permission denied under `/data`: apply the narrowly scoped ownership command
  from Setup, then retry onboarding.
- Healthy but unreachable: replace the loopback publication with the host's
  Tailscale or LAN address and use that same address in Pebble.
- Port binding failure: confirm the selected address exists on the Docker host.
- Missing session: rerun the interactive container and complete Telegram login.
