# Monitoring additions (ntfy + Gatus running in the same compose project)

## Gatus: `config.yaml` (hot-reloads)
Under `external-endpoints:`:

```yaml
  - name: ifit-garmin-sync
    group: apps
    token: "<new random token = IFIT_GATUS_TOKEN>"
    heartbeat:
      interval: 1h          # sync cycles every 15 min; 4 missed cycles = alert
    alerts:
      - type: ntfy
        failure-threshold: 2
        send-on-resolved: true
```

## ntfy: `server.yml` (restart ntfy)
Add a write-only user for topic `fitness` (success notices), following the
existing server-config users:

```yaml
auth-users:
  - "ifitsync:<bcrypt hash>:user"
auth-access:
  - "ifitsync:fitness:wo"
auth-tokens:
  - "ifitsync:<tk_... token = IFIT_NTFY_TOKEN>"
```
Subscribe to `fitness` in the ntfy phone app.

## Stack environment (.env)
IFIT_STRAVA_CLIENT_ID, IFIT_STRAVA_CLIENT_SECRET, IFIT_GARMIN_EMAIL,
IFIT_GARMIN_PASSWORD, IFIT_NTFY_TOKEN, IFIT_GATUS_TOKEN

## Data seed
Copy `data/garmin_tokens/`, `data/strava_tokens.json`, `data/state.db` to
the container data volume (owner 1000:1000, mode 700),
so the container starts already logged in and does not re-upload past workouts.
