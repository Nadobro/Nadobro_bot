# Deploying Nadobro Bot to Fly.io

## Prerequisites

- [Fly CLI](https://fly.io/docs/flyctl/install/) installed
- Fly.io account (free tier works)
- Supabase project with PostgreSQL database
- Your bot secrets ready (Telegram token, encryption key, API keys)

## Step 1: Log in to Fly.io

```bash
fly auth login
```

## Step 2: Launch the app

From the project root directory:

```bash
fly launch --no-deploy
```

When prompted:
- App name: `nadobro-bot` (or your preferred name)
- Region: select **Amsterdam (ams)** or another European region
- Skip creating a Postgres database (we're using Supabase)
- Skip creating a Redis database

## Step 3: Set your secrets

```bash
fly secrets set \
  DATABASE_URL="postgresql://postgres:YOUR_PASSWORD@db.YOUR_PROJECT.supabase.co:5432/postgres" \
  TELEGRAM_TOKEN="your-telegram-bot-token" \
  ENCRYPTION_KEY="your-encryption-key" \
  XAI_API_KEY="your-xai-api-key" \
  OPENAI_API_KEY="your-openai-api-key" \
  SESSION_SECRET="your-session-secret" \
  NADO_BUILDER_ID="your-builder-id" \
  NADO_BUILDER_FEE_RATE="10"
```

Builder routing safety:
- `NADO_BUILDER_ID` is required for order placement.
- `NADO_BUILDER_FEE_RATE` must stay `10` (1 bps in 0.1 bps units).
- If either value is invalid/missing, the bot rejects order submission (hard fail).

### Optional: Enable webhook transport (recommended for speed and scale)

```bash
fly secrets set \
  TELEGRAM_TRANSPORT="webhook" \
  TELEGRAM_WEBHOOK_URL="https://your-app-name.fly.dev/telegram/webhook" \
  TELEGRAM_WEBHOOK_PATH="/telegram/webhook" \
  TELEGRAM_WEBHOOK_SECRET="your-long-random-secret"
```

If you want to keep polling mode, set:

```bash
fly secrets set TELEGRAM_TRANSPORT="polling"
```

## Step 4: Deploy

```bash
fly deploy --build-arg GIT_SHA=$(git rev-parse HEAD)
```

This builds the Docker image and deploys it to the Amsterdam region.

## Step 5: Check logs

```bash
fly logs
```

You should see the bot starting in either polling or webhook mode, depending on `TELEGRAM_TRANSPORT`.

## Updating the bot

After making code changes in Replit:

1. Download your updated files (or push to GitHub)
2. Run `fly deploy --build-arg GIT_SHA=$(git rev-parse HEAD)` from the project directory (from a COMMITTED tree — the boot log prints `Build: NADOBRO_GIT_SHA=<sha>` so the running build can be matched to a commit)

If using GitHub, you can set up auto-deploy:
```bash
fly launch --from-repo https://github.com/your-username/nadobro-bot
```

## Useful commands

```bash
fly status          # Check app status
fly logs            # View live logs
fly logs -a nadobro-bot  # View logs for specific app
fly ssh console     # SSH into the running machine
fly secrets list    # List configured secrets
fly restart         # Restart the app
fly scale count 1   # Ensure 1 machine is running
```

## Troubleshooting

**Bot not responding:**
- Check logs: `fly logs`
- Verify secrets are set: `fly secrets list`
- Make sure the Telegram token is correct

**Database connection errors:**
- Verify your Supabase DATABASE_URL is correct
- Make sure the Supabase project is active
- Check that the connection string uses port 5432

**Health check failures:**
- In the current bot-only deploy profile, nginx serves `GET /health` on port 8080.
- Fly.io uses this endpoint to verify the app is running.

**Changing regions:**
- Edit `primary_region` in `fly.toml` and redeploy

## Scale Path (Webhook)

Current production runtime uses Telegram webhook mode.

### Phase A (now)
- Keep one ingress machine always on (`min_machines_running=1`).
- Scale internal workers using:
  - `NADO_STRATEGY_WORKERS` (default `2`)
  - `NADO_ALERT_WORKERS` (default `1`)

### Phase B (higher concurrency)
1. Keep ingress stateless and enqueue heavy work.
2. Use idempotency keys on jobs to avoid duplicate execution.
3. Scale ingress and worker capacity independently.

