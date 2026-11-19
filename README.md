# Do I need a coat? 🧥

Open the page, it finds where you are, and tells you **yes** or **no**.

The answer comes from people nearby saying whether *they* needed a coat. When nobody has voted, it uses the current weather and historical data instead. The background shows the weather right now (sun, clouds, rain, snow, fog, storms, day and night), and a breakdown underneath shows how the answer was worked out.

## How the answer is worked out

Each signal gives a probability that you need a coat, plus a weight. The answer is the weighted average. It's **yes** at 50% or more.

| Signal | What it is | Weight |
|---|---|---|
| **People nearby** | Votes within 10 km from the last 2 hours. Each vote's weight drops linearly from full to zero over 2 hours, then the vote stops counting. | 1.5 per brand-new vote |
| **Weather right now** | Open-Meteo forecast. Based on feels-like temperature (50/50 at 14 °C), raised for rain now, rain likely in the next 3 h, snow, or wind. | 3 |
| **Past votes in similar weather** | Votes older than 2 h from the past year, within 100 km, cast when it felt within ±2.5 °C of now and was wet/dry like now. Each vote saves a weather snapshot, so the database builds up its own history. | 0.25 per vote, max 4 |
| **Usual for this date** | The last 10 years of Open-Meteo archive data, ±7 days around today's date. | 1 |

So with no votes, the weather and climate decide. Two fresh local votes count as much as the forecast. A handful of people saying "nah, it's fine" will overrule it.

All the tuning lives at the top of `brolly/verdict.py`.

## Running it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn --factory brolly.app:create_app --reload
# open http://localhost:8000
```

Browsers only allow geolocation on `https://` or `localhost`. If location is blocked or denied, the page offers a place search.

Tests (they don't need network access, because the weather is faked):

```bash
pytest
# also run the API tests against Postgres:
docker run -d --rm -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16-alpine
TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres pytest
```

Moving existing votes from SQLite to Postgres (it refuses to run if the target already has votes):

```bash
python -m brolly.migrate data/brolly.db "$DATABASE_URL"
```

### Config

| Env var | Default | |
|---|---|---|
| `DATABASE_URL` | – | Postgres connection string (`postgresql://…?sslmode=require`). **Use this in production.** |
| `DATABASE_PATH` | `data/brolly.db` | SQLite file, used when `DATABASE_URL` isn't set (local dev) |
| `CANONICAL_HOST` | – | e.g. `needacoat.com`. Requests to `www.` or `*.fly.dev` get a 301 redirect there |
| `IP_SALT` | `brolly` | Salt for hashing IPs (used for rate limiting). **Set this in production.** |

### Docker

```bash
docker build -t brolly .
docker run -p 8000:8000 -v brolly-data:/data -e IP_SALT=change-me brolly
```

Put the SQLite file on a persistent disk/volume (Fly.io volume, Render disk, Railway volume, etc.).

### Deploying to Fly.io

```bash
# one-off setup (install flyctl: https://fly.io/docs/flyctl/install/)
fly auth login
fly launch --copy-config --no-deploy        # pick a unique app name when asked
fly secrets set IP_SALT=$(openssl rand -hex 16)
fly secrets set DATABASE_URL='postgresql://…'   # Neon (or any Postgres)

# every deploy
fly deploy
```

Votes live in Postgres, so the machine itself is stateless and safe to scale or replace.

## CI/CD

`.github/workflows/ci-cd.yml` runs on every push and PR:

1. **test** runs `pytest` against SQLite and a Postgres service container, then builds the Docker image and checks `/healthz`.
2. **deploy** (pushes to `main` only, after tests pass) runs `flyctl deploy --remote-only`, then smoke-tests https://needacoat.com.

It needs one repo secret, `FLY_API_TOKEN`: an app-scoped deploy token, made in the Fly dashboard (ancient-pine-6068 → Tokens) or with `fly tokens create deploy -a ancient-pine-6068`.
Dependabot (`.github/dependabot.yml`) opens weekly PRs for pip, Docker and Actions updates.

## API

- `GET /api/verdict?lat=..&lon=..` returns the answer, probability, per-signal breakdown, recent nearby votes, current weather and climate
- `POST /api/vote` `{lat, lon, needed, client_id}` records a vote. Voting again within 2 h changes your vote rather than adding another. Each IP is limited to 10 new votes an hour.
- `GET /api/geocode?q=..` searches for a place (used when location isn't available)

## Security

- The client IP comes from `Fly-Client-IP` (set by Fly's edge). `X-Forwarded-For` is never trusted.
- Per-IP limits: 60/min on `/api/verdict`, 20/min on `/api/geocode`, 30/min on `/api/vote`, plus 10 new votes/hour.
- Strict CSP, HSTS, `X-Frame-Options: DENY`, `nosniff`, a tight `Permissions-Policy`. No public `/docs`.
- The container runs as a non-root user.

## Privacy

Locations are rounded to 2 decimal places (~1 km) before they reach the database. IPs are only stored as salted hashes. The client ID is a random UUID kept in the browser's localStorage. Votes older than a year are deleted automatically.
