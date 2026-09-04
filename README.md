# One-click hosted Instagram → Discord app

This project is designed to be deployed as a single hosted service.

After deployment:

1. Open the service URL.
2. Click **Add to Discord**.
3. Choose your Discord server.
4. Run `/instagram setup`.
5. Pick the channel.
6. New Instagram content is posted automatically.

## Discord commands

- `/instagram setup`
- `/instagram status`
- `/instagram test`
- `/instagram latest`
- `/instagram disconnect`

## Content supported

- Image posts
- Reels
- Video posts
- Carousels
- Stories (optional)

## Required secrets

The hosting provider will need these four values:

```env
DISCORD_BOT_TOKEN=
DISCORD_CLIENT_ID=
INSTAGRAM_ACCESS_TOKEN=
INSTAGRAM_USER_ID=
```

These are deliberately not stored in the repository.

## Render deployment

The included `render.yaml` creates:

- one always-on Python web service
- a health endpoint
- a persistent disk for Discord server configuration and duplicate protection
- environment-variable prompts for credentials

A paid always-on Render web service is used because free web services can spin down after inactivity, which is unsuitable for a Discord bot that needs to maintain its gateway connection and poll Instagram continuously.

### One-click deploy

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/esgtoxic/instagram-discord-app)

Clicking **Deploy to Render** opens the deployment screen. Enter the four required secrets and approve the Blueprint.

## Discord Developer Portal setup

Create a Discord Application and Bot.

Copy:

- Application ID → `DISCORD_CLIENT_ID`
- Bot Token → `DISCORD_BOT_TOKEN`

The app uses:

- View Channels
- Send Messages
- Embed Links
- Application Commands

No Message Content intent is required.

## Instagram

The Instagram account must be compatible with Meta's professional Instagram API access and the token must be able to read its media.

Enter:

- `INSTAGRAM_ACCESS_TOKEN`
- `INSTAGRAM_USER_ID`

## Dashboard

The hosted `/` page displays an **Add to Discord** button automatically based on `DISCORD_CLIENT_ID`.

`/health` returns deployment health.

## Persistent state

Server/channel configuration and already-posted media IDs are saved to:

`/var/data/state.sqlite3`

The Render Blueprint mounts persistent storage at `/var/data`.

## Local testing

```bash
python -m venv .venv
```

Install:

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env`, fill in your credentials, then:

```bash
python app.py
```

Open:

`http://localhost:10000`

## Security

Never commit `.env`.

Treat both your Discord bot token and Instagram access token as secrets. Rotate either credential if it is exposed.
