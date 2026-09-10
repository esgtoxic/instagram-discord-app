import asyncio
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from urllib.parse import urlencode

import discord
import requests
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from flask import Flask, render_template_string

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "").strip()
INSTAGRAM_ACCESS_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()
INSTAGRAM_USER_ID = os.getenv("INSTAGRAM_USER_ID", "").strip()
INSTAGRAM_GRAPH_HOST = os.getenv(
    "INSTAGRAM_GRAPH_HOST", "https://graph.instagram.com"
).rstrip("/")
META_API_VERSION = os.getenv("META_API_VERSION", "v26.0").strip()
POLL_INTERVAL_SECONDS = max(15, int(os.getenv("POLL_INTERVAL_SECONDS", "30")))
PORT = int(os.getenv("PORT", "10000"))
STATE_DB = os.getenv("STATE_DB", "/var/data/state.sqlite3")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("instagram-discord-app")


# ---------------------------------------------------------------------------
# Persistent state
# ---------------------------------------------------------------------------

def db():
    parent = os.path.dirname(STATE_DB)
    if parent:
        os.makedirs(parent, exist_ok=True)

    conn = sqlite3.connect(STATE_DB)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            include_stories INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS posted_media (
            guild_id INTEGER NOT NULL,
            media_id TEXT NOT NULL,
            media_kind TEXT NOT NULL,
            posted_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, media_id)
        )
        """
    )
    conn.commit()
    return conn


def save_config(guild_id, channel_id):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO guild_config(guild_id, channel_id, include_stories, created_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                include_stories = 1
            """,
            (guild_id, channel_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def get_config(guild_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)
        ).fetchone()


def all_configs():
    with db() as conn:
        return conn.execute("SELECT * FROM guild_config").fetchall()


def enable_stories_for_existing_configs():
    with db() as conn:
        conn.execute("UPDATE guild_config SET include_stories = 1")
        conn.commit()


def remove_config(guild_id):
    with db() as conn:
        conn.execute("DELETE FROM guild_config WHERE guild_id = ?", (guild_id,))
        conn.execute("DELETE FROM posted_media WHERE guild_id = ?", (guild_id,))
        conn.commit()


def posted(guild_id, media_id):
    with db() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM posted_media WHERE guild_id = ? AND media_id = ?",
                (guild_id, media_id),
            ).fetchone()
            is not None
        )


def mark_posted(guild_id, media_id, kind):
    with db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO posted_media(guild_id, media_id, media_kind, posted_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                guild_id,
                media_id,
                kind,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Instagram API
# ---------------------------------------------------------------------------

MEDIA_FIELDS = (
    "id,caption,media_type,media_product_type,media_url,thumbnail_url,"
    "permalink,timestamp,username,children{media_type,media_url,thumbnail_url}"
)

# Keep Story fields deliberately small for maximum Graph API compatibility.
# A Story link can fall back to instagram.com/stories/<username>/ if the API
# does not expose a direct permalink for that Story object.
STORY_FIELDS = "id,media_type,media_url,thumbnail_url,timestamp,username"


def graph_get(path, fields, limit=100):
    url = f"{INSTAGRAM_GRAPH_HOST}/{META_API_VERSION}/{path.lstrip('/')}"
    response = requests.get(
        url,
        params={
            "access_token": INSTAGRAM_ACCESS_TOKEN,
            "fields": fields,
            "limit": limit,
        },
        timeout=25,
    )
    if not response.ok:
        raise RuntimeError(
            f"Instagram API {response.status_code}: {response.text[:700]}"
        )
    return response.json().get("data", [])


def fetch_feed():
    return graph_get(f"{INSTAGRAM_USER_ID}/media", MEDIA_FIELDS, limit=100)


def fetch_stories():
    return graph_get(f"{INSTAGRAM_USER_ID}/stories", STORY_FIELDS, limit=100)


def fetch_username():
    url = f"{INSTAGRAM_GRAPH_HOST}/{META_API_VERSION}/{INSTAGRAM_USER_ID}"
    response = requests.get(
        url,
        params={
            "access_token": INSTAGRAM_ACCESS_TOKEN,
            "fields": "username",
        },
        timeout=25,
    )
    if not response.ok:
        raise RuntimeError(
            f"Instagram API {response.status_code}: {response.text[:700]}"
        )
    return response.json().get("username")


def parse_ts(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def oldest_first(items):
    fallback = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(
        items,
        key=lambda item: parse_ts(item.get("timestamp")) or fallback,
    )


def newest_item(items):
    ordered = oldest_first(items)
    return ordered[-1] if ordered else None


def is_reel(item):
    return (
        (item.get("media_product_type") or "").upper() == "REELS"
        or "/reel/" in (item.get("permalink") or "").lower()
    )


def preview_url(item):
    media_type = (item.get("media_type") or "").upper()

    if media_type == "IMAGE":
        return item.get("media_url")
    if media_type == "VIDEO":
        return item.get("thumbnail_url") or item.get("media_url")
    if media_type == "CAROUSEL_ALBUM":
        for child in (item.get("children") or {}).get("data") or []:
            return child.get("thumbnail_url") or child.get("media_url")

    return item.get("thumbnail_url") or item.get("media_url")


def title_for(item, kind):
    if kind == "story":
        return "📸 New Instagram Story"
    if is_reel(item):
        return "🎬 New Instagram Reel"

    media_type = (item.get("media_type") or "").upper()
    if media_type == "CAROUSEL_ALBUM":
        return "🖼️ New Instagram Carousel"
    if media_type == "VIDEO":
        return "🎥 New Instagram Video"
    return "📸 New Instagram Post"


def instagram_link(item, kind, username=None):
    permalink = (item.get("permalink") or "").strip()
    if permalink:
        return permalink

    account = (item.get("username") or username or "").strip()
    if account and kind == "story":
        return f"https://www.instagram.com/stories/{account}/"
    if account:
        return f"https://www.instagram.com/{account}/"
    return "https://www.instagram.com/"


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

intents = discord.Intents.none()
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
ig = app_commands.Group(
    name="instagram",
    description="Instagram auto-posting settings",
)

MENTIONS = discord.AllowedMentions(
    everyone=True,
    users=False,
    roles=False,
    replied_user=False,
)


async def make_embed(item, kind):
    username = item.get("username")
    if not username:
        try:
            username = await asyncio.to_thread(fetch_username)
        except Exception:
            username = None

    link = instagram_link(item, kind, username=username)
    caption = (item.get("caption") or "").strip()
    if len(caption) > 3400:
        caption = caption[:3397] + "..."

    if kind == "story":
        body = "A new Story was posted on Instagram."
    else:
        body = caption or "New content was posted on Instagram."

    body += f"\n\n🔗 **[View on Instagram]({link})**"

    embed = discord.Embed(
        title=title_for(item, kind),
        url=link,
        description=body,
    )

    image = preview_url(item)
    if image:
        embed.set_image(url=image)

    if username:
        embed.set_footer(text=f"Instagram • @{username}")
    else:
        embed.set_footer(text="Instagram")

    timestamp = parse_ts(item.get("timestamp"))
    if timestamp:
        embed.timestamp = timestamp

    return embed


async def send_public(channel, embed=None, text=None):
    content = "@everyone" + (f"\n{text}" if text else "")
    return await channel.send(
        content=content,
        embed=embed,
        allowed_mentions=MENTIONS,
    )


async def resolve_channel(guild_id, channel_id):
    channel = bot.get_channel(channel_id)
    if channel is not None and hasattr(channel, "send"):
        return channel

    guild = bot.get_guild(guild_id)
    if guild is not None:
        channel = guild.get_channel(channel_id)
        if channel is not None and hasattr(channel, "send"):
            return channel
        try:
            channel = await guild.fetch_channel(channel_id)
            if channel is not None and hasattr(channel, "send"):
                return channel
        except discord.DiscordException:
            pass

    try:
        channel = await bot.fetch_channel(channel_id)
        if channel is not None and hasattr(channel, "send"):
            return channel
    except discord.DiscordException:
        pass

    return None


async def seed_current(guild_id):
    """Avoid flooding old content when /instagram setup is first run."""
    try:
        for item in await asyncio.to_thread(fetch_feed):
            if item.get("id"):
                mark_posted(guild_id, str(item["id"]), "feed")
    except Exception:
        log.exception("Initial Instagram feed seed failed")

    try:
        for item in await asyncio.to_thread(fetch_stories):
            if item.get("id"):
                mark_posted(guild_id, str(item["id"]), "story")
    except Exception:
        log.exception("Initial Instagram Story seed failed")


async def forward(config, item, kind):
    guild_id = int(config["guild_id"])
    media_id = str(item.get("id") or "")

    if not media_id or posted(guild_id, media_id):
        return False

    channel = await resolve_channel(guild_id, int(config["channel_id"]))
    if not channel:
        log.warning(
            "Skipping Instagram %s %s because the Discord channel is unavailable",
            kind,
            media_id,
        )
        return False

    try:
        await send_public(channel, embed=await make_embed(item, kind))
        mark_posted(guild_id, media_id, kind)
        log.info(
            "Forwarded Instagram %s %s to guild %s",
            kind,
            media_id,
            guild_id,
        )
        return True
    except discord.DiscordException:
        log.exception("Discord send failed for Instagram %s %s", kind, media_id)
        return False


async def sync_unseen(config):
    sent = 0

    feed = oldest_first(await asyncio.to_thread(fetch_feed))
    for item in feed:
        if await forward(config, item, "feed"):
            sent += 1

    # Stories are permanently enabled. Fetch them independently so Story API
    # errors are visible without changing the feed-posting behavior.
    stories = oldest_first(await asyncio.to_thread(fetch_stories))
    for item in stories:
        if await forward(config, item, "story"):
            sent += 1

    return sent


@tasks.loop(seconds=POLL_INTERVAL_SECONDS)
async def poll_instagram():
    configs = all_configs()
    if not configs:
        return

    try:
        feed = oldest_first(await asyncio.to_thread(fetch_feed))
    except Exception:
        log.exception("Instagram feed fetch failed")
        feed = []

    try:
        stories = oldest_first(await asyncio.to_thread(fetch_stories))
        log.info("Instagram Stories poll returned %s active Story item(s)", len(stories))
    except Exception:
        log.exception("Instagram Stories fetch failed")
        stories = []

    for config in configs:
        for item in feed:
            await forward(config, item, "feed")
        for item in stories:
            await forward(config, item, "story")


@poll_instagram.before_loop
async def before_poll():
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

@ig.command(
    name="setup",
    description="Use this channel for all Instagram posts, Reels and Stories.",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def setup(interaction: discord.Interaction):
    if not interaction.guild_id or not interaction.guild:
        await interaction.response.send_message(
            "Use this command inside a Discord server.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    channel = interaction.channel

    if not isinstance(channel, discord.TextChannel):
        await interaction.followup.send(
            "Run `/instagram setup` inside a normal server text channel.",
            ephemeral=True,
        )
        return

    me = interaction.guild.me
    if me is not None:
        permissions = channel.permissions_for(me)
        missing = []
        if not permissions.view_channel:
            missing.append("View Channel")
        if not permissions.send_messages:
            missing.append("Send Messages")
        if not permissions.embed_links:
            missing.append("Embed Links")
        if not permissions.mention_everyone:
            missing.append("Mention @everyone, @here, and All Roles")

        if missing:
            await interaction.followup.send(
                "Give the bot these permissions first: " + ", ".join(missing),
                ephemeral=True,
            )
            return

    try:
        username = await asyncio.to_thread(fetch_username)
    except Exception as exc:
        await interaction.followup.send(
            f"Instagram connection failed: `{type(exc).__name__}: {str(exc)[:540]}`",
            ephemeral=True,
        )
        return

    save_config(interaction.guild_id, channel.id)
    await seed_current(interaction.guild_id)

    await interaction.followup.send(
        f"✅ Connected **@{username or 'Instagram'}** to {channel.mention}.\n"
        "Posts/Reels/Carousels: **On**\n"
        "Stories: **Always On**\n"
        "Clickable Instagram links: **On**\n"
        "@everyone notifications: **On**\n"
        f"Checking about every **{POLL_INTERVAL_SECONDS} seconds**.",
        ephemeral=True,
    )


@ig.command(name="status", description="Show the current Instagram setup.")
@app_commands.checks.has_permissions(manage_guild=True)
async def status(interaction: discord.Interaction):
    if not interaction.guild_id:
        return

    config = get_config(interaction.guild_id)
    if not config:
        await interaction.response.send_message(
            "Not configured. Run `/instagram setup`.", ephemeral=True
        )
        return

    await interaction.response.send_message(
        "✅ Instagram auto-posting is active.\n"
        f"Channel: <#{int(config['channel_id'])}>\n"
        "Posts/Reels/Carousels: **On**\n"
        "Stories: **Always On**\n"
        "@everyone notifications: **On**",
        ephemeral=True,
    )


@ig.command(name="test", description="Send a test Instagram-style message.")
@app_commands.checks.has_permissions(manage_guild=True)
async def test(interaction: discord.Interaction):
    if not interaction.guild_id:
        return

    config = get_config(interaction.guild_id)
    if not config:
        await interaction.response.send_message(
            "Run `/instagram setup` first.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    channel = await resolve_channel(
        interaction.guild_id, int(config["channel_id"])
    )
    if not channel:
        await interaction.followup.send(
            "Configured channel is unavailable.", ephemeral=True
        )
        return

    embed = discord.Embed(
        title="✅ Instagram Bot Test",
        url="https://www.instagram.com/",
        description=(
            "The bot can post here successfully.\n\n"
            "🔗 **[Open Instagram](https://www.instagram.com/)**"
        ),
    )
    await send_public(channel, embed=embed)
    await interaction.followup.send(
        "✅ Test sent with @everyone.", ephemeral=True
    )


async def send_manual(interaction, item, label, kind="feed"):
    config = get_config(interaction.guild_id)
    channel = await resolve_channel(
        interaction.guild_id, int(config["channel_id"])
    )
    if not channel:
        await interaction.followup.send(
            "Configured channel is unavailable.", ephemeral=True
        )
        return

    await send_public(channel, embed=await make_embed(item, kind))
    await interaction.followup.send(
        f"✅ {label} sent with @everyone.", ephemeral=True
    )


@ig.command(name="latest", description="Post the latest Instagram feed item now.")
@app_commands.checks.has_permissions(manage_guild=True)
async def latest(interaction: discord.Interaction):
    if not interaction.guild_id:
        return
    if not get_config(interaction.guild_id):
        await interaction.response.send_message(
            "Run `/instagram setup` first.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    try:
        item = newest_item(await asyncio.to_thread(fetch_feed))
    except Exception as exc:
        await interaction.followup.send(
            f"Instagram request failed: `{str(exc)[:600]}`", ephemeral=True
        )
        return

    if not item:
        await interaction.followup.send(
            "Instagram returned no feed items.", ephemeral=True
        )
        return

    await send_manual(interaction, item, "Latest Instagram item")


@ig.command(name="latest_reel", description="Post the newest Instagram Reel now.")
@app_commands.checks.has_permissions(manage_guild=True)
async def latest_reel(interaction: discord.Interaction):
    if not interaction.guild_id:
        return
    if not get_config(interaction.guild_id):
        await interaction.response.send_message(
            "Run `/instagram setup` first.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    try:
        item = newest_item(
            [item for item in await asyncio.to_thread(fetch_feed) if is_reel(item)]
        )
    except Exception as exc:
        await interaction.followup.send(
            f"Instagram request failed: `{str(exc)[:600]}`", ephemeral=True
        )
        return

    if not item:
        await interaction.followup.send(
            "Instagram did not return a Reel in the recent media list.",
            ephemeral=True,
        )
        return

    await send_manual(interaction, item, "Latest Reel")


@ig.command(name="latest_story", description="Post the newest active Instagram Story now.")
@app_commands.checks.has_permissions(manage_guild=True)
async def latest_story(interaction: discord.Interaction):
    if not interaction.guild_id:
        return
    if not get_config(interaction.guild_id):
        await interaction.response.send_message(
            "Run `/instagram setup` first.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    try:
        stories = await asyncio.to_thread(fetch_stories)
        item = newest_item(stories)
    except Exception as exc:
        await interaction.followup.send(
            f"Instagram Story request failed: `{type(exc).__name__}: {str(exc)[:540]}`",
            ephemeral=True,
        )
        return

    if not item:
        await interaction.followup.send(
            "Instagram returned no active Stories for this account.",
            ephemeral=True,
        )
        return

    await send_manual(interaction, item, "Latest Story", kind="story")


@ig.command(
    name="sync",
    description="Immediately post Instagram items the bot has not posted yet.",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def sync(interaction: discord.Interaction):
    if not interaction.guild_id:
        return

    config = get_config(interaction.guild_id)
    if not config:
        await interaction.response.send_message(
            "Run `/instagram setup` first.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    try:
        sent = await sync_unseen(config)
    except Exception as exc:
        await interaction.followup.send(
            f"Instagram sync failed: `{type(exc).__name__}: {str(exc)[:540]}`",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        f"✅ Sync complete. Posted **{sent}** unseen Instagram item"
        f"{'s' if sent != 1 else ''}, including Stories.",
        ephemeral=True,
    )


@ig.command(name="disconnect", description="Stop Instagram auto-posting.")
@app_commands.checks.has_permissions(manage_guild=True)
async def disconnect(interaction: discord.Interaction):
    if interaction.guild_id:
        remove_config(interaction.guild_id)
    await interaction.response.send_message(
        "✅ Instagram auto-posting disconnected.", ephemeral=True
    )


async def command_error(interaction, error):
    original = getattr(error, "original", error)
    if isinstance(error, app_commands.MissingPermissions):
        message = "You need **Manage Server** permission to use this command."
    else:
        message = (
            f"Something went wrong: `{type(original).__name__}: "
            f"{str(original)[:600]}`"
        )

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


for command in (
    setup,
    status,
    test,
    latest,
    latest_reel,
    latest_story,
    sync,
    disconnect,
):
    command.error(command_error)


async def sync_guild(guild):
    try:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    except discord.DiscordException:
        log.exception("Guild command sync failed: %s", guild.id)


@bot.event
async def on_ready():
    log.info("Discord bot online as %s", bot.user)
    for guild in bot.guilds:
        await sync_guild(guild)


@bot.event
async def on_guild_join(guild):
    await sync_guild(guild)


async def setup_hook():
    bot.tree.add_command(ig)
    await bot.tree.sync()
    if not poll_instagram.is_running():
        poll_instagram.start()
    log.info("Instagram slash commands synced")


bot.setup_hook = setup_hook


# ---------------------------------------------------------------------------
# Hosted dashboard
# ---------------------------------------------------------------------------

web = Flask(__name__)
PAGE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Instagram → Discord</title>
<style>
body{font-family:Arial,sans-serif;background:#0e1015;color:#f5f7fb;margin:0}
.wrap{max-width:760px;margin:70px auto;padding:24px}
.card{background:#171a21;border:1px solid #292e39;border-radius:20px;padding:28px;margin-bottom:18px}
h1{font-size:36px;margin:0 0 10px}
p{line-height:1.6;color:#b8c0ce}
.btn{display:inline-block;background:#5865f2;color:white;text-decoration:none;padding:14px 20px;border-radius:12px;font-weight:700}
.ok{color:#69db7c}
code{background:#0d0f14;padding:5px 8px;border-radius:7px}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1>Instagram → Discord</h1>
    <p class="ok">● Hosted service is running</p>
    <p>Automatically forwards new Instagram posts, Reels, carousels and Stories to Discord.</p>
    {% if install_url %}<a class="btn" href="{{ install_url }}">Add to Discord</a>{% endif %}
  </div>
  <div class="card">
    <h2>Commands</h2>
    <p><code>/instagram setup</code> · <code>/instagram status</code> · <code>/instagram test</code> · <code>/instagram latest</code> · <code>/instagram latest_reel</code> · <code>/instagram latest_story</code> · <code>/instagram sync</code></p>
  </div>
</div>
</body>
</html>
"""


def install_url():
    if not DISCORD_CLIENT_ID:
        return None

    # View Channel + Send Messages + Embed Links + Read Message History
    # + Mention @everyone/@here/all roles.
    permissions = 1024 + 2048 + 16384 + 65536 + 131072
    return "https://discord.com/oauth2/authorize?" + urlencode(
        {
            "client_id": DISCORD_CLIENT_ID,
            "scope": "bot applications.commands",
            "permissions": permissions,
        }
    )


@web.get("/")
def home():
    return render_template_string(PAGE, install_url=install_url())


@web.get("/health")
def health():
    return {
        "ok": True,
        "discord_ready": bot.is_ready(),
        "poll_interval_seconds": POLL_INTERVAL_SECONDS,
        "stories": "always_on",
    }


def run_web():
    web.run(host="0.0.0.0", port=PORT, use_reloader=False, threaded=True)


def validate():
    missing = [
        key
        for key, value in {
            "DISCORD_BOT_TOKEN": DISCORD_BOT_TOKEN,
            "DISCORD_CLIENT_ID": DISCORD_CLIENT_ID,
            "INSTAGRAM_ACCESS_TOKEN": INSTAGRAM_ACCESS_TOKEN,
            "INSTAGRAM_USER_ID": INSTAGRAM_USER_ID,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))


if __name__ == "__main__":
    validate()
    db()
    enable_stories_for_existing_configs()
    threading.Thread(target=run_web, daemon=True).start()
    bot.run(DISCORD_BOT_TOKEN, log_handler=None)
