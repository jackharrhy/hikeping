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

# run tests
uv run pytest
```

## Run scheduler (every Friday, 6:00 PM)

```bash
export $(grep -v '^#' .env | xargs)
uv run hikeping
```

Defaults to timezone `America/St_Johns` (override with `HIKEPING_TIMEZONE`).
Set `HIKEPING_INFO_WEBHOOK_URL` to send scraper-health alerts to a separate Discord channel, such as `#bot-spam`.

## Notes

- Discord hike posts include the title, date, time, location, difficulty, distance, registration link, and hike page link when those fields exist in `events-data.js`.
- If no weekend hike is detected, it logs and does not post.
- If the structured event feed cannot be parsed, it logs and posts an alert to `HIKEPING_INFO_WEBHOOK_URL` when set.

## Docker

Build locally:

```bash
docker build -t hikeping:local .
```

Run locally:

```bash
docker run --rm \
  -e DISCORD_WEBHOOK_URL="$DISCORD_WEBHOOK_URL" \
  -e HIKEPING_INFO_WEBHOOK_URL="$HIKEPING_INFO_WEBHOOK_URL" \
  -e HIKEPING_TIMEZONE="America/St_Johns" \
  hikeping:local
```

GitHub Actions in `.github/workflows/` will build and push to GHCR on pushes to `main`.
