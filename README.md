# LinkHarbor for Factorio

A single-file Python agent that polls a Factorio server over RCON and pushes
the live state to your [LinkHarbor](https://linkharbor.com) bio page: a
game-server card with Online/Offline, the number of players, player names and
the map time, plus join/leave events for push notifications to your followers.

It uses the RCON port the Factorio headless server can open on start, so
nothing is installed on the server. Standard library only, Python 3.10 or newer.
Factorio does not report a player limit over RCON, so the card shows the
player count without a maximum.

Part of the [LinkHarbor integrations](https://github.com/linkharbor/integrations)
family, one repo per game or tool.

## Setup

```bash
git clone https://github.com/linkharbor/integration-factorio
cd integration-factorio

export LH_INGEST_URL="https://dock.at-harbor.com/v1/ingest/123"   # dashboard: Integrations, Custom (API)
export LH_INGEST_SECRET="…"
export FACTORIO_RCON_PASSWORD="…"                                  # see "Enabling RCON"

python3 agent.py --host 203.0.113.10 --port 27015 --name "Our Factory" \
  --address "203.0.113.10:34197" --url "https://discord.gg/xyz" --interval 60
```

Start with `--once -v`. Run it as a service with `integration-factorio.service`
(systemd; secrets go into `/etc/integration-factorio.env`, `chmod 600`). If the
server runs on the same machine, `--host 127.0.0.1` works even when the RCON
port is firewalled from the outside.

## Events sent

| Event | When | Template variables |
|---|---|---|
| `server.online` | the first successful poll, or the server is back after being unreachable | `{name}`, `{players_online}`, `{description}` |
| `server.offline` | the server stopped answering, or rejected the password | `{name}` |
| `player.joined` | a new name appears in `/players online` | `{player}` and the above |
| `player.left` | a name disappears | `{player}` and the above |

The state is sent with every event and every `--heartbeat` minutes (default
10). `{description}` is the output of `/time` ("Map time: …").
`--no-player-names` sends counts only.

Suggested push rule (dashboard, Integrations, Push rules, Custom (API), event `player.joined`): batch window 10 min, title "🎮 {count} players joined", body
"{player_list} are now on {name}", minimum pause 5 min.

## Options

```
--host HOST            server IP or hostname (required)
--port PORT            RCON port (default 27015)
--password PASSWORD    RCON password (or env FACTORIO_RCON_PASSWORD)
--name NAME            card title (default "Factorio server")
--address ADDRESS      address shown on the card, e.g. 203.0.113.10:34197
--url URL              link behind the card's button
--cta TEXT             button label (default "Join server")
--image-url URL        banner image (large layout)
--interval SECONDS     polling interval (default 60, minimum 5)
--heartbeat MINUTES    state-only update interval (default 10, 0 = only on change)
--no-player-names      send counts only
--once                 poll once, send, exit
-v                     verbose logging
```

## Enabling RCON

Factorio has no RCON by default. Add two flags to the server start line:

```bash
./bin/x64/factorio --start-server saves/mymap.zip \
  --rcon-port 27015 --rcon-password "a-long-random-string"
```

(`--rcon-bind 127.0.0.1:27015` instead of `--rcon-port` keeps the port local
when the agent runs on the same machine.) The password goes into
`FACTORIO_RCON_PASSWORD`. The agent opens a new connection every poll and runs
`/players online` and `/time`; nothing is changed on the server. RCON is plain
TCP without encryption, so keep the port on a private network or behind a
firewall.

## License

[MIT](LICENSE). © 2026 EMI Solutions OÜ.
