# hikeping

Tiny `uv` app that checks the St. John's Hike Club upcoming-hike page and posts to Discord if it looks like there is a hike this upcoming weekend.

Page checked: https://www.stjohnshikeclub.com/upcoming-hike.html

## Setup

```bash
cd hikeping
uv sync
cp .env.example .env
# edit .env with your webhook URL
```

## Run once (test)

```bash
export $(grep -v '^#' .env | xargs)
uv run hikeping --once

# show next upcoming hike
uv run hikeping --next

# show + post next upcoming hike
uv run hikeping --next --post
```

## Run scheduler (every Friday, 6:00 PM)

```bash
export $(grep -v '^#' .env | xargs)
uv run hikeping
```

Defaults to timezone `America/St_Johns` (override with `HIKEPING_TIMEZONE`).

## Notes

- Discord message format: `🌳 Weekend hike looks posted: <link>`
- If no weekend hike is detected, it logs and does not post.

## Docker

Build locally:

```bash
docker build -t hikeping:local .
```

Run locally:

```bash
docker run --rm \
  -e DISCORD_WEBHOOK_URL="$DISCORD_WEBHOOK_URL" \
  -e HIKEPING_TIMEZONE="America/St_Johns" \
  hikeping:local
```

GitHub Actions in `.github/workflows/` will build and push to GHCR on pushes to `main`.
