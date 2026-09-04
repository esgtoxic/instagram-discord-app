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

DISCORD_BOT_TOKEN=os.getenv("DISCORD_BOT_TOKEN","").strip()
DISCORD_CLIENT_ID=os.getenv("DISCORD_CLIENT_ID","").strip()
INSTAGRAM_ACCESS_TOKEN=os.getenv("INSTAGRAM_ACCESS_TOKEN","").strip()
INSTAGRAM_USER_ID=os.getenv("INSTAGRAM_USER_ID","").strip()
INSTAGRAM_GRAPH_HOST=os.getenv("INSTAGRAM_GRAPH_HOST","https://graph.instagram.com").rstrip("/")
META_API_VERSION=os.getenv("META_API_VERSION","v26.0").strip()
POLL_INTERVAL_SECONDS=max(15,int(os.getenv("POLL_INTERVAL_SECONDS","30")))
PORT=int(os.getenv("PORT","10000"))
STATE_DB=os.getenv("STATE_DB","/var/data/state.sqlite3")

logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO").upper(),format="%(asctime)s | %(levelname)s | %(message)s")
log=logging.getLogger("instagram-discord-app")

def db():
    parent=os.path.dirname(STATE_DB)
    if parent: os.makedirs(parent,exist_ok=True)
    conn=sqlite3.connect(STATE_DB)
    conn.row_factory=sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS guild_config(
      guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL,
      include_stories INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS posted_media(
      guild_id INTEGER NOT NULL, media_id TEXT NOT NULL, media_kind TEXT NOT NULL,
      posted_at TEXT NOT NULL, PRIMARY KEY(guild_id,media_id))""")
    conn.commit()
    return conn

def save_config(guild_id,channel_id,include_stories):
    with db() as conn:
        conn.execute("""INSERT INTO guild_config(guild_id,channel_id,include_stories,created_at)
        VALUES(?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET
        channel_id=excluded.channel_id, include_stories=excluded.include_stories""",
        (guild_id,channel_id,1 if include_stories else 0,datetime.now(timezone.utc).isoformat()))
        conn.commit()

def get_config(guild_id):
    with db() as conn:
        return conn.execute("SELECT * FROM guild_config WHERE guild_id=?",(guild_id,)).fetchone()

def all_configs():
    with db() as conn:
        return conn.execute("SELECT * FROM guild_config").fetchall()

def remove_config(guild_id):
    with db() as conn:
        conn.execute("DELETE FROM guild_config WHERE guild_id=?",(guild_id,))
        conn.execute("DELETE FROM posted_media WHERE guild_id=?",(guild_id,))
        conn.commit()

def posted(guild_id,media_id):
    with db() as conn:
        return conn.execute("SELECT 1 FROM posted_media WHERE guild_id=? AND media_id=?",(guild_id,media_id)).fetchone() is not None

def mark_posted(guild_id,media_id,kind):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO posted_media VALUES(?,?,?,?)",
          (guild_id,media_id,kind,datetime.now(timezone.utc).isoformat()))
        conn.commit()

MEDIA_FIELDS="id,caption,media_type,media_product_type,media_url,thumbnail_url,permalink,timestamp,username,children{media_type,media_url,thumbnail_url}"
STORY_FIELDS="id,caption,media_type,media_product_type,media_url,thumbnail_url,permalink,timestamp,username"

def graph_get(path,fields,limit=25):
    url=f"{INSTAGRAM_GRAPH_HOST}/{META_API_VERSION}/{path.lstrip('/')}"
    r=requests.get(url,params={"access_token":INSTAGRAM_ACCESS_TOKEN,"fields":fields,"limit":limit},timeout=25)
    if not r.ok: raise RuntimeError(f"Instagram API {r.status_code}: {r.text[:700]}")
    return r.json().get("data",[])

def fetch_feed(): return graph_get(f"{INSTAGRAM_USER_ID}/media",MEDIA_FIELDS)
def fetch_stories(): return graph_get(f"{INSTAGRAM_USER_ID}/stories",STORY_FIELDS)

def fetch_username():
    url=f"{INSTAGRAM_GRAPH_HOST}/{META_API_VERSION}/{INSTAGRAM_USER_ID}"
    r=requests.get(url,params={"access_token":INSTAGRAM_ACCESS_TOKEN,"fields":"username"},timeout=25)
    if not r.ok: raise RuntimeError(f"Instagram API {r.status_code}: {r.text[:700]}")
    return r.json().get("username")

def parse_ts(raw):
    if not raw: return None
    try: return datetime.fromisoformat(raw.replace("Z","+00:00"))
    except Exception: return None

def oldest_first(items):
    fallback=datetime.min.replace(tzinfo=timezone.utc)
    return sorted(items,key=lambda x:parse_ts(x.get("timestamp")) or fallback)

def preview_url(item):
    t=(item.get("media_type") or "").upper()
    if t=="IMAGE": return item.get("media_url")
    if t=="VIDEO": return item.get("thumbnail_url") or item.get("media_url")
    if t=="CAROUSEL_ALBUM":
        for child in (item.get("children") or {}).get("data") or []:
            return child.get("media_url") or child.get("thumbnail_url")
    return item.get("thumbnail_url") or item.get("media_url")

def title_for(item,kind):
    if kind=="story": return "📸 New Instagram Story"
    if (item.get("media_product_type") or "").upper()=="REELS": return "🎬 New Instagram Reel"
    if (item.get("media_type") or "").upper()=="CAROUSEL_ALBUM": return "🖼️ New Instagram Carousel"
    if (item.get("media_type") or "").upper()=="VIDEO": return "🎥 New Instagram Video"
    return "📸 New Instagram Post"

intents=discord.Intents.none()
intents.guilds=True
bot=commands.Bot(command_prefix="!",intents=intents,help_command=None)
ig=app_commands.Group(name="instagram",description="Instagram auto-posting settings")

async def make_embed(item,kind):
    caption=(item.get("caption") or "").strip()
    if len(caption)>3800: caption=caption[:3797]+"..."
    e=discord.Embed(title=title_for(item,kind),url=item.get("permalink") or None,
                    description=caption or "New content was posted on Instagram.")
    image=preview_url(item)
    if image: e.set_image(url=image)
    username=item.get("username")
    e.set_footer(text=f"Instagram • @{username}" if username else "Instagram")
    ts=parse_ts(item.get("timestamp"))
    if ts: e.timestamp=ts
    return e

async def resolve_channel(guild_id,channel_id):
    # Prefer cache, then guild-level REST, then global REST. Do not restrict
    # to TextChannel only: Discord can return other message-capable channel
    # implementations depending on channel/server configuration.
    ch=bot.get_channel(channel_id)
    if ch is not None and hasattr(ch,"send"):
        return ch

    guild=bot.get_guild(guild_id)
    if guild is not None:
        ch=guild.get_channel(channel_id)
        if ch is not None and hasattr(ch,"send"):
            return ch
        try:
            ch=await guild.fetch_channel(channel_id)
            if ch is not None and hasattr(ch,"send"):
                return ch
        except discord.DiscordException as exc:
            log.warning("Guild channel fetch failed for %s: %r",channel_id,exc)

    try:
        ch=await bot.fetch_channel(channel_id)
        if ch is not None and hasattr(ch,"send"):
            return ch
    except discord.Forbidden:
        log.error("Bot cannot access configured channel %s in guild %s",channel_id,guild_id)
    except discord.NotFound:
        log.error("Configured channel %s no longer exists in guild %s",channel_id,guild_id)
    except discord.DiscordException as exc:
        log.error("Global channel fetch failed for %s: %r",channel_id,exc)

    return None

async def seed_current(guild_id,include_stories):
    try:
        for item in await asyncio.to_thread(fetch_feed):
            if item.get("id"): mark_posted(guild_id,str(item["id"]),"feed")
        if include_stories:
            for item in await asyncio.to_thread(fetch_stories):
                if item.get("id"): mark_posted(guild_id,str(item["id"]),"story")
    except Exception:
        log.exception("Initial Instagram seed failed")

async def forward(config,item,kind):
    guild_id=int(config["guild_id"])
    media_id=str(item.get("id") or "")
    if not media_id or posted(guild_id,media_id): return
    channel=await resolve_channel(guild_id,int(config["channel_id"]))
    if not channel: return
    try:
        await channel.send(embed=await make_embed(item,kind))
        mark_posted(guild_id,media_id,kind)
    except discord.DiscordException:
        log.exception("Discord send failed")

@tasks.loop(seconds=POLL_INTERVAL_SECONDS)
async def poll_instagram():
    configs=all_configs()
    if not configs: return
    try:
        feed=oldest_first(await asyncio.to_thread(fetch_feed))
    except Exception:
        log.exception("Instagram feed fetch failed")
        return
    stories=[]
    if any(bool(c["include_stories"]) for c in configs):
        try: stories=oldest_first(await asyncio.to_thread(fetch_stories))
        except Exception: log.exception("Instagram stories fetch failed")
    for config in configs:
        for item in feed: await forward(config,item,"feed")
        if bool(config["include_stories"]):
            for item in stories: await forward(config,item,"story")

@poll_instagram.before_loop
async def before_poll(): await bot.wait_until_ready()

@ig.command(name="setup",description="Use this channel for Instagram auto-posts.")
@app_commands.describe(include_stories="Also forward Instagram Stories")
@app_commands.checks.has_permissions(manage_guild=True)
async def setup(interaction:discord.Interaction,include_stories:bool=True):
    # Acknowledge immediately so Discord never times out the interaction.
    if not interaction.guild_id or not interaction.guild:
        await interaction.response.send_message(
            "Use this command inside a Discord server.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    # Use the channel where the command was executed. This avoids Discord.py
    # TextChannel transformer failures and makes setup simpler for admins.
    channel=interaction.channel

    if not isinstance(channel,discord.TextChannel):
        await interaction.followup.send(
            "Please run `/instagram setup` inside a normal server text channel.",
            ephemeral=True
        )
        return

    log.info(
        "Running /instagram setup for guild=%s channel=%s",
        interaction.guild_id,
        channel.id
    )

    me=interaction.guild.me
    if me is None and bot.user is not None:
        me=interaction.guild.get_member(bot.user.id)
    if me is None and bot.user is not None:
        try:
            me=await interaction.guild.fetch_member(bot.user.id)
        except discord.DiscordException:
            me=None

    if me is not None:
        perms=channel.permissions_for(me)
        missing=[]
        if not perms.view_channel: missing.append("View Channel")
        if not perms.send_messages: missing.append("Send Messages")
        if not perms.embed_links: missing.append("Embed Links")

        if missing:
            await interaction.followup.send(
                "Give the bot these permissions in this channel first: "
                + ", ".join(missing),
                ephemeral=True
            )
            return

    try:
        username=await asyncio.to_thread(fetch_username)
    except Exception as exc:
        log.error("Instagram connection failed during setup: %r",exc)
        await interaction.followup.send(
            f"Instagram connection failed: `{type(exc).__name__}: {str(exc)[:540]}`",
            ephemeral=True
        )
        return

    try:
        save_config(interaction.guild_id,channel.id,include_stories)
    except Exception as exc:
        log.error("Database/config save failed during setup: %r",exc)
        await interaction.followup.send(
            f"Could not save the Discord channel configuration: "
            f"`{type(exc).__name__}: {str(exc)[:500]}`",
            ephemeral=True
        )
        return

    await seed_current(interaction.guild_id,include_stories)

    await interaction.followup.send(
        f"✅ Connected **@{username or 'Instagram'}** to {channel.mention}.\n"
        f"Stories: **{'On' if include_stories else 'Off'}**\n"
        f"New content is checked about every {POLL_INTERVAL_SECONDS} seconds.\n\n"
        f"Run `/instagram test` here to verify posting.",
        ephemeral=True
    )

@ig.command(name="status",description="Show the current Instagram setup.")
@app_commands.checks.has_permissions(manage_guild=True)
async def status(interaction:discord.Interaction):
    if not interaction.guild_id: return
    config=get_config(interaction.guild_id)
    if not config:
        await interaction.response.send_message("Not configured. Run `/instagram setup`.",ephemeral=True); return
    channel=interaction.guild.get_channel(int(config["channel_id"]))
    text=channel.mention if channel else f"`{config['channel_id']}`"
    await interaction.response.send_message(
      f"✅ Instagram auto-posting is active.\nChannel: {text}\nStories: **{'On' if bool(config['include_stories']) else 'Off'}**",
      ephemeral=True)

@ig.command(name="test",description="Send a test message.")
@app_commands.checks.has_permissions(manage_guild=True)
async def test(interaction:discord.Interaction):
    if not interaction.guild_id:
        return

    config=get_config(interaction.guild_id)
    if not config:
        await interaction.response.send_message("Run `/instagram setup` first.",ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    configured_channel_id=int(config["channel_id"])

    # If the command is being run in the configured channel, use the exact
    # interaction channel object Discord already supplied. This avoids any
    # cache/transformer discrepancies.
    channel=None
    if interaction.channel is not None and getattr(interaction.channel,"id",None)==configured_channel_id and hasattr(interaction.channel,"send"):
        channel=interaction.channel
    else:
        channel=await resolve_channel(interaction.guild_id,configured_channel_id)

    if not channel:
        await interaction.followup.send(
            f"Configured channel <#{configured_channel_id}> could not be resolved. "
            "Run `/instagram setup` again in the channel you want to use.",
            ephemeral=True
        )
        return

    e=discord.Embed(
        title="✅ Instagram Bot Test",
        description="The hosted Discord app is online and can post in this channel."
    )
    e.set_footer(text="Instagram → Discord")

    try:
        await channel.send(embed=e)
    except discord.Forbidden:
        await interaction.followup.send(
            "I found the configured channel, but Discord denied permission to send there. "
            "Give the bot **View Channel**, **Send Messages**, and **Embed Links**.",
            ephemeral=True
        )
        return
    except discord.DiscordException as exc:
        await interaction.followup.send(
            f"Discord send failed: `{type(exc).__name__}: {str(exc)[:500]}`",
            ephemeral=True
        )
        return

    await interaction.followup.send(
        f"✅ Test message sent to <#{configured_channel_id}>.",
        ephemeral=True
    )

@ig.command(name="latest",description="Post the latest Instagram feed item.")
@app_commands.checks.has_permissions(manage_guild=True)
async def latest(interaction:discord.Interaction):
    if not interaction.guild_id: return
    config=get_config(interaction.guild_id)
    if not config:
        await interaction.response.send_message("Run `/instagram setup` first.",ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    try: feed=oldest_first(await asyncio.to_thread(fetch_feed))
    except Exception as exc:
        await interaction.followup.send(f"Instagram request failed: `{str(exc)[:600]}`",ephemeral=True); return
    if not feed:
        await interaction.followup.send("Instagram returned no feed items.",ephemeral=True); return
    channel=await resolve_channel(interaction.guild_id,int(config["channel_id"]))
    if not channel:
        await interaction.followup.send("Configured channel is unavailable.",ephemeral=True); return
    await channel.send(embed=await make_embed(feed[-1],"feed"))
    await interaction.followup.send(f"Latest post sent to {channel.mention}.",ephemeral=True)

@ig.command(name="disconnect",description="Stop Instagram auto-posting.")
@app_commands.checks.has_permissions(manage_guild=True)
async def disconnect(interaction:discord.Interaction):
    if interaction.guild_id: remove_config(interaction.guild_id)
    await interaction.response.send_message("✅ Instagram auto-posting disconnected.",ephemeral=True)

async def cmd_error(interaction,error):
    if isinstance(error,app_commands.MissingPermissions):
        msg="You need **Manage Server** permission to use this command."
    else:
        original=getattr(error,"original",error)
        detail=f"{type(original).__name__}: {str(original)}"
        log.error("Slash command failed: %s",detail,exc_info=original if isinstance(original,BaseException) else None)
        # Return the underlying error ephemerally to the administrator so setup
        # problems can be diagnosed without exposing it publicly in the channel.
        msg=f"Something went wrong: `{detail[:650]}`"
    if interaction.response.is_done(): await interaction.followup.send(msg,ephemeral=True)
    else: await interaction.response.send_message(msg,ephemeral=True)

setup.error(cmd_error); status.error(cmd_error); test.error(cmd_error); latest.error(cmd_error); disconnect.error(cmd_error)

@bot.event
async def on_ready(): log.info("Discord bot online as %s",bot.user)

async def setup_hook():
    bot.tree.add_command(ig)
    await bot.tree.sync()
    if not poll_instagram.is_running(): poll_instagram.start()
bot.setup_hook=setup_hook

web=Flask(__name__)
PAGE="""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Instagram → Discord</title>
<style>body{font-family:Arial,sans-serif;background:#0e1015;color:#f5f7fb;margin:0}.wrap{max-width:760px;margin:70px auto;padding:24px}.card{background:#171a21;border:1px solid #292e39;border-radius:20px;padding:28px;margin-bottom:18px}h1{font-size:36px;margin:0 0 10px}p{line-height:1.6;color:#b8c0ce}.btn{display:inline-block;background:#5865f2;color:white;text-decoration:none;padding:14px 20px;border-radius:12px;font-weight:700}.ok{color:#69db7c}code{background:#0d0f14;padding:5px 8px;border-radius:7px}</style></head>
<body><div class="wrap"><div class="card"><h1>Instagram → Discord</h1><p class="ok">● Hosted service is running</p><p>Automatically send new Instagram posts, Reels, carousels and optional Stories to Discord.</p>{% if install_url %}<a class="btn" href="{{ install_url }}">Add to Discord</a>{% endif %}</div>
<div class="card"><h2>After installing</h2><p>Run <code>/instagram setup</code> in your Discord server and select the channel.</p></div></div></body></html>"""

def install_url():
    if not DISCORD_CLIENT_ID: return None
    permissions=1024+2048+16384
    return "https://discord.com/oauth2/authorize?"+urlencode({"client_id":DISCORD_CLIENT_ID,"scope":"bot applications.commands","permissions":permissions})

@web.get("/")
def home(): return render_template_string(PAGE,install_url=install_url())

@web.get("/health")
def health(): return {"ok":True,"discord_ready":bot.is_ready(),"poll_interval_seconds":POLL_INTERVAL_SECONDS}

def run_web(): web.run(host="0.0.0.0",port=PORT,use_reloader=False,threaded=True)

def validate():
    missing=[k for k,v in {
      "DISCORD_BOT_TOKEN":DISCORD_BOT_TOKEN,"DISCORD_CLIENT_ID":DISCORD_CLIENT_ID,
      "INSTAGRAM_ACCESS_TOKEN":INSTAGRAM_ACCESS_TOKEN,"INSTAGRAM_USER_ID":INSTAGRAM_USER_ID}.items() if not v]
    if missing: raise RuntimeError("Missing required environment variables: "+", ".join(missing))

if __name__=="__main__":
    validate()
    db()
    threading.Thread(target=run_web,daemon=True).start()
    bot.run(DISCORD_BOT_TOKEN,log_handler=None)
