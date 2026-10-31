# Lexi push gateway on Thor

Thor's Forge is the push gateway for Damien's Guild: it sends LexiChat's alerts, Live
Activity updates, widget reloads and background pushes for every node (Thor and mini-m6).
Damien approved it on 2026-10-31. How the gateway itself works is in
[forge-notifications.md](forge-notifications.md), section "Apple push".

## What is configured where

| What | Where |
|---|---|
| Gateway config | Thor, `~/.config/niuu-forge-thor/config.yaml`, section `push:` (file mode 600) |
| APNs auth key (`.p8`) | Thor, `~/.openclaw/secrets/apns/`: the only `.p8` there, mode 600 |
| Who else uses the key | lexi-agent-service (VoIP pushes): the same team-scoped "Forge Push" key |
| Device registry | Thor's Volundr database, table `device_tokens` (`push.device_registry: postgres`) |
| Push on mini-m6 | **off** (no `push:` section), so nothing is pushed twice |

The `push:` section:

```yaml
push:
  enabled: true
  device_registry: postgres
  apns:
    team_id: 828357L924
    key_id: <the key's id: the 10 characters in its file name, AuthKey_<id>.p8>
    bundle_id: com.xteo.lexi.chat
    private_key_path: <the .p8 above>
  gateway:
    enabled: true
    guild_url: http://127.0.0.1:8080          # Thor's own Guild facade (same process)
    public_api_url: http://thor.tail737f2a.ts.net:8080
    origin_label: Thor
    catch_up_items: 0                         # start at each node's newest notification
```

- The key's id and contents never go into a ticket, log, commit or message. Only the
  config file names them.
- TestFlight and App Store builds register `production` tokens, which go to
  `api.push.apple.com`. Debug builds register `sandbox` tokens, which go to the sandbox
  host. The token's own `environment` picks the host.
- `stream_enabled` stays off: the Guild runs in the gateway's own process. The gateway
  wakes on Thor's own notifications at once and reads mini-m6 every 5 s.

**Devices.** The app registers its tokens through the Guild with
`POST /api/v1/forge/devices`: alert, push-to-start, widget, and one Live Activity token per
running activity. Alert tokens reach every node; the other kinds go to the gateway alone.
Check them on Thor:

```bash
docker exec volundr-pg psql -U volundr -d volundr -c \
  "SELECT kind, environment, count(*), max(updated_at) FROM device_tokens GROUP BY 1, 2"
```

## Checking it

```bash
curl -s http://127.0.0.1:8080/api/v1/forge/push/status | python3 -m json.tool
```

- `running: true` and an empty `unreachable_nodes` are the healthy state.
- `nodes` holds the gateway's position per node. The counters cover alerts sent,
  suppressed and failed, and Live Activity, widget and background pushes.
- `POST /api/v1/forge/push/test {"kind": "alert", "title": "…", "body": "…"}` pushes once
  to the caller's own tokens.

## Rotating the key

1. In the Apple Developer portal (team 828357L924), create a new APNs auth key and
   download its `.p8`. The download can be made only once.
2. Put it in `~/.openclaw/secrets/apns/` with mode 600, and move the old `.p8` out of the
   folder: the folder holds exactly one key.
3. Update `push.apns.key_id` and `push.apns.private_key_path` in Thor's config. Tell the
   agent-service lane, which uses the same key for VoIP.
4. Restart Thor's Forge between turns, under the Thor gate (an API restart only; lanes
   keep running). Check `GET /push/status`, then send a test push.
5. Revoke the old key in the portal only after both Forge and the agent-service push with
   the new one.

A key that cannot be read, or is not an EC P-256 key, stops Forge's start with the setting
to fix. There is no silent fallback to running without push.

## Turning it off

- **Everything:** set `push.enabled: false`, or remove the `push:` section, and restart
  Thor's Forge. Registered devices stay in `device_tokens`, unused.
- **One surface:** keep the gateway and set `push.gateway.alerts.enabled`,
  `push.gateway.live_activity.enabled`, `push.gateway.widgets.enabled` or
  `push.gateway.background.enabled` to `false`.
- **Moving the gateway to another node:** turn it off on Thor first, then on the other
  node, so two gateways never push the same notification.

The backup taken before the first enable is
`~/.config/niuu-forge-thor/config.yaml.bak-20261031-push`.
