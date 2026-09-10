import asyncio, logging, os, sqlite3, threading
from datetime import datetime, timezone
from urllib.parse import urlencode

import discord, requests
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from flask import Flask, render_template_string

load_dotenv()
DISCORD_BOT_TOKEN=os.getenv('DISCORD_BOT_TOKEN','').strip()
DISCORD_CLIENT_ID=os.getenv('DISCORD_CLIENT_ID','').strip()
INSTAGRAM_ACCESS_TOKEN=os.getenv('INSTAGRAM_ACCESS_TOKEN','').strip()
INSTAGRAM_USER_ID=os.getenv('INSTAGRAM_USER_ID','').strip()
INSTAGRAM_GRAPH_HOST=os.getenv('INSTAGRAM_GRAPH_HOST','https://graph.instagram.com').rstrip('/')
META_API_VERSION=os.getenv('META_API_VERSION','v26.0').strip()
POLL_INTERVAL_SECONDS=max(15,int(os.getenv('POLL_INTERVAL_SECONDS','30')))
PORT=int(os.getenv('PORT','10000'))
STATE_DB=os.getenv('STATE_DB','/var/data/state.sqlite3')
logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO').upper(),format='%(asctime)s | %(levelname)s | %(message)s')
log=logging.getLogger('instagram-discord-app')

# ---------- state ----------
def db():
    parent=os.path.dirname(STATE_DB)
    if parent: os.makedirs(parent,exist_ok=True)
    c=sqlite3.connect(STATE_DB); c.row_factory=sqlite3.Row
    c.execute('CREATE TABLE IF NOT EXISTS guild_config (guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, include_stories INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS posted_media (guild_id INTEGER NOT NULL, media_id TEXT NOT NULL, media_kind TEXT NOT NULL, posted_at TEXT NOT NULL, PRIMARY KEY(guild_id,media_id))')
    c.commit(); return c

def save_config(guild_id,channel_id,include_stories):
    with db() as c:
        c.execute('INSERT INTO guild_config(guild_id,channel_id,include_stories,created_at) VALUES(?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, include_stories=excluded.include_stories',(guild_id,channel_id,1 if include_stories else 0,datetime.now(timezone.utc).isoformat()))
        c.commit()

def get_config(guild_id):
    with db() as c: return c.execute('SELECT * FROM guild_config WHERE guild_id=?',(guild_id,)).fetchone()

def all_configs():
    with db() as c: return c.execute('SELECT * FROM guild_config').fetchall()

def remove_config(guild_id):
    with db() as c:
        c.execute('DELETE FROM guild_config WHERE guild_id=?',(guild_id,)); c.execute('DELETE FROM posted_media WHERE guild_id=?',(guild_id,)); c.commit()

def posted(guild_id,media_id):
    with db() as c: return c.execute('SELECT 1 FROM posted_media WHERE guild_id=? AND media_id=?',(guild_id,media_id)).fetchone() is not None

def mark_posted(guild_id,media_id,kind):
    with db() as c:
        c.execute('INSERT OR IGNORE INTO posted_media(guild_id,media_id,media_kind,posted_at) VALUES(?,?,?,?)',(guild_id,media_id,kind,datetime.now(timezone.utc).isoformat())); c.commit()

# ---------- Instagram ----------
MEDIA_FIELDS='id,caption,media_type,media_product_type,media_url,thumbnail_url,permalink,timestamp,username,children{media_type,media_url,thumbnail_url}'
STORY_FIELDS='id,caption,media_type,media_product_type,media_url,thumbnail_url,permalink,timestamp,username'

def graph_get(path,fields,limit=100):
    r=requests.get(f'{INSTAGRAM_GRAPH_HOST}/{META_API_VERSION}/{path.lstrip("/")}',params={'access_token':INSTAGRAM_ACCESS_TOKEN,'fields':fields,'limit':limit},timeout=25)
    if not r.ok: raise RuntimeError(f'Instagram API {r.status_code}: {r.text[:700]}')
    return r.json().get('data',[])

def fetch_feed(): return graph_get(f'{INSTAGRAM_USER_ID}/media',MEDIA_FIELDS)
def fetch_stories(): return graph_get(f'{INSTAGRAM_USER_ID}/stories',STORY_FIELDS)
def fetch_username():
    r=requests.get(f'{INSTAGRAM_GRAPH_HOST}/{META_API_VERSION}/{INSTAGRAM_USER_ID}',params={'access_token':INSTAGRAM_ACCESS_TOKEN,'fields':'username'},timeout=25)
    if not r.ok: raise RuntimeError(f'Instagram API {r.status_code}: {r.text[:700]}')
    return r.json().get('username')

def parse_ts(raw):
    try: return datetime.fromisoformat(raw.replace('Z','+00:00')) if raw else None
    except Exception: return None

def oldest_first(items):
    fallback=datetime.min.replace(tzinfo=timezone.utc)
    return sorted(items,key=lambda x:parse_ts(x.get('timestamp')) or fallback)

def newest_item(items):
    x=oldest_first(items); return x[-1] if x else None

def is_reel(item):
    return (item.get('media_product_type') or '').upper()=='REELS' or '/reel/' in (item.get('permalink') or '').lower()

def preview_url(item):
    t=(item.get('media_type') or '').upper()
    if t=='IMAGE': return item.get('media_url')
    if t=='VIDEO': return item.get('thumbnail_url') or item.get('media_url')
    if t=='CAROUSEL_ALBUM':
        for child in (item.get('children') or {}).get('data') or []:
            return child.get('thumbnail_url') or child.get('media_url')
    return item.get('thumbnail_url') or item.get('media_url')

def title_for(item,kind):
    if kind=='story': return '📸 New Instagram Story'
    if is_reel(item): return '🎬 New Instagram Reel'
    t=(item.get('media_type') or '').upper()
    if t=='CAROUSEL_ALBUM': return '🖼️ New Instagram Carousel'
    if t=='VIDEO': return '🎥 New Instagram Video'
    return '📸 New Instagram Post'

def instagram_link(item,kind):
    if item.get('permalink'): return item['permalink']
    u=(item.get('username') or '').strip()
    if u and kind=='story': return f'https://www.instagram.com/stories/{u}/'
    if u: return f'https://www.instagram.com/{u}/'
    return 'https://www.instagram.com/'

# ---------- Discord ----------
intents=discord.Intents.none(); intents.guilds=True
bot=commands.Bot(command_prefix='!',intents=intents,help_command=None)
ig=app_commands.Group(name='instagram',description='Instagram auto-posting settings')
MENTIONS=discord.AllowedMentions(everyone=True,users=False,roles=False,replied_user=False)

async def make_embed(item,kind):
    link=instagram_link(item,kind); caption=(item.get('caption') or '').strip()
    if len(caption)>3400: caption=caption[:3397]+'...'
    body=(caption or 'New content was posted on Instagram.')+f'\n\n🔗 **[View on Instagram]({link})**'
    e=discord.Embed(title=title_for(item,kind),url=link,description=body)
    image=preview_url(item)
    if image: e.set_image(url=image)
    u=item.get('username'); e.set_footer(text=f'Instagram • @{u}' if u else 'Instagram')
    ts=parse_ts(item.get('timestamp'))
    if ts: e.timestamp=ts
    return e

async def send_public(channel,embed=None,text=None):
    content='@everyone'+(f'\n{text}' if text else '')
    return await channel.send(content=content,embed=embed,allowed_mentions=MENTIONS)

async def resolve_channel(guild_id,channel_id):
    ch=bot.get_channel(channel_id)
    if ch is not None and hasattr(ch,'send'): return ch
    guild=bot.get_guild(guild_id)
    if guild:
        ch=guild.get_channel(channel_id)
        if ch is not None and hasattr(ch,'send'): return ch
        try:
            ch=await guild.fetch_channel(channel_id)
            if ch is not None and hasattr(ch,'send'): return ch
        except discord.DiscordException: pass
    try:
        ch=await bot.fetch_channel(channel_id)
        return ch if ch is not None and hasattr(ch,'send') else None
    except discord.DiscordException: return None

async def seed_current(guild_id,include_stories):
    try:
        for item in await asyncio.to_thread(fetch_feed):
            if item.get('id'): mark_posted(guild_id,str(item['id']),'feed')
        if include_stories:
            for item in await asyncio.to_thread(fetch_stories):
                if item.get('id'): mark_posted(guild_id,str(item['id']),'story')
    except Exception: log.exception('Initial Instagram seed failed')

async def forward(config,item,kind):
    guild_id=int(config['guild_id']); media_id=str(item.get('id') or '')
    if not media_id or posted(guild_id,media_id): return False
    channel=await resolve_channel(guild_id,int(config['channel_id']))
    if not channel: return False
    try:
        await send_public(channel,embed=await make_embed(item,kind)); mark_posted(guild_id,media_id,kind); return True
    except discord.DiscordException:
        log.exception('Discord send failed for %s %s',kind,media_id); return False

async def sync_unseen(config):
    sent=0
    for item in oldest_first(await asyncio.to_thread(fetch_feed)):
        sent += 1 if await forward(config,item,'feed') else 0
    if bool(config['include_stories']):
        for item in oldest_first(await asyncio.to_thread(fetch_stories)):
            sent += 1 if await forward(config,item,'story') else 0
    return sent

@tasks.loop(seconds=POLL_INTERVAL_SECONDS)
async def poll_instagram():
    configs=all_configs()
    if not configs: return
    try: feed=oldest_first(await asyncio.to_thread(fetch_feed))
    except Exception: log.exception('Instagram feed fetch failed'); return
    stories=[]
    if any(bool(c['include_stories']) for c in configs):
        try: stories=oldest_first(await asyncio.to_thread(fetch_stories))
        except Exception: log.exception('Instagram stories fetch failed')
    for c in configs:
        for item in feed: await forward(c,item,'feed')
        if bool(c['include_stories']):
            for item in stories: await forward(c,item,'story')

@poll_instagram.before_loop
async def before_poll(): await bot.wait_until_ready()

# ---------- commands ----------
@ig.command(name='setup',description='Use this channel for all new Instagram posts, Reels and Stories.')
@app_commands.describe(include_stories='Also forward all new Instagram Stories')
@app_commands.checks.has_permissions(manage_guild=True)
async def setup(interaction:discord.Interaction,include_stories:bool=True):
    if not interaction.guild_id or not interaction.guild:
        await interaction.response.send_message('Use this command inside a Discord server.',ephemeral=True); return
    await interaction.response.defer(ephemeral=True); channel=interaction.channel
    if not isinstance(channel,discord.TextChannel):
        await interaction.followup.send('Run `/instagram setup` inside a normal server text channel.',ephemeral=True); return
    me=interaction.guild.me
    if me:
        p=channel.permissions_for(me); missing=[]
        if not p.view_channel: missing.append('View Channel')
        if not p.send_messages: missing.append('Send Messages')
        if not p.read_message_history: missing.append('Read Message History')
        if not p.embed_links: missing.append('Embed Links')
        if not p.mention_everyone: missing.append('Mention @everyone, @here, and All Roles')
        if missing:
            await interaction.followup.send('Give the bot these permissions first: '+', '.join(missing),ephemeral=True); return
    try: username=await asyncio.to_thread(fetch_username)
    except Exception as exc:
        await interaction.followup.send(f'Instagram connection failed: `{type(exc).__name__}: {str(exc)[:540]}`',ephemeral=True); return
    save_config(interaction.guild_id,channel.id,include_stories); await seed_current(interaction.guild_id,include_stories)
    await interaction.followup.send(f'✅ Connected **@{username or "Instagram"}** to {channel.mention}.\nPosts/Reels/Carousels: **On**\nStories: **{"On" if include_stories else "Off"}**\nClickable links: **On**\n@everyone notifications: **On**',ephemeral=True)

@ig.command(name='status',description='Show the current Instagram setup.')
@app_commands.checks.has_permissions(manage_guild=True)
async def status(interaction:discord.Interaction):
    if not interaction.guild_id: return
    c=get_config(interaction.guild_id)
    if not c:
        await interaction.response.send_message('Not configured. Run `/instagram setup`.',ephemeral=True); return
    await interaction.response.send_message(f'✅ Instagram auto-posting is active.\nChannel: <#{int(c["channel_id"])}>\nStories: **{"On" if bool(c["include_stories"]) else "Off"}**\n@everyone notifications: **On**',ephemeral=True)

@ig.command(name='test',description='Send a test Instagram-style message.')
@app_commands.checks.has_permissions(manage_guild=True)
async def test(interaction:discord.Interaction):
    if not interaction.guild_id: return
    c=get_config(interaction.guild_id)
    if not c:
        await interaction.response.send_message('Run `/instagram setup` first.',ephemeral=True); return
    await interaction.response.defer(ephemeral=True); ch=await resolve_channel(interaction.guild_id,int(c['channel_id']))
    if not ch:
        await interaction.followup.send('Configured channel is unavailable.',ephemeral=True); return
    e=discord.Embed(title='✅ Instagram Bot Test',url='https://www.instagram.com/',description='The bot can post here successfully.\n\n🔗 **[Open Instagram](https://www.instagram.com/)**')
    await send_public(ch,embed=e); await interaction.followup.send('✅ Test sent with @everyone.',ephemeral=True)

async def send_manual(interaction,item,label):
    c=get_config(interaction.guild_id); ch=await resolve_channel(interaction.guild_id,int(c['channel_id']))
    if not ch:
        await interaction.followup.send('Configured channel is unavailable.',ephemeral=True); return
    await send_public(ch,embed=await make_embed(item,'feed')); await interaction.followup.send(f'✅ {label} sent with @everyone.',ephemeral=True)

@ig.command(name='latest',description='Post the latest Instagram feed item now.')
@app_commands.checks.has_permissions(manage_guild=True)
async def latest(interaction:discord.Interaction):
    if not interaction.guild_id: return
    if not get_config(interaction.guild_id):
        await interaction.response.send_message('Run `/instagram setup` first.',ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    try: item=newest_item(await asyncio.to_thread(fetch_feed))
    except Exception as exc:
        await interaction.followup.send(f'Instagram request failed: `{str(exc)[:600]}`',ephemeral=True); return
    if not item:
        await interaction.followup.send('Instagram returned no feed items.',ephemeral=True); return
    await send_manual(interaction,item,'Latest Instagram item')

@ig.command(name='latest_reel',description='Post the newest Instagram Reel now.')
@app_commands.checks.has_permissions(manage_guild=True)
async def latest_reel(interaction:discord.Interaction):
    if not interaction.guild_id: return
    if not get_config(interaction.guild_id):
        await interaction.response.send_message('Run `/instagram setup` first.',ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    try: item=newest_item([x for x in await asyncio.to_thread(fetch_feed) if is_reel(x)])
    except Exception as exc:
        await interaction.followup.send(f'Instagram request failed: `{str(exc)[:600]}`',ephemeral=True); return
    if not item:
        await interaction.followup.send('Instagram did not return a Reel in the recent media list.',ephemeral=True); return
    await send_manual(interaction,item,'Latest Reel')

@ig.command(name='sync',description='Immediately post Instagram items the bot has not posted yet.')
@app_commands.checks.has_permissions(manage_guild=True)
async def sync(interaction:discord.Interaction):
    if not interaction.guild_id: return
    c=get_config(interaction.guild_id)
    if not c:
        await interaction.response.send_message('Run `/instagram setup` first.',ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    try: sent=await sync_unseen(c)
    except Exception as exc:
        await interaction.followup.send(f'Instagram sync failed: `{type(exc).__name__}: {str(exc)[:540]}`',ephemeral=True); return
    await interaction.followup.send(f'✅ Sync complete. Posted **{sent}** unseen Instagram item{"s" if sent!=1 else ""}.',ephemeral=True)

@ig.command(name='disconnect',description='Stop Instagram auto-posting.')
@app_commands.checks.has_permissions(manage_guild=True)
async def disconnect(interaction:discord.Interaction):
    if interaction.guild_id: remove_config(interaction.guild_id)
    await interaction.response.send_message('✅ Instagram auto-posting disconnected.',ephemeral=True)

async def command_error(interaction,error):
    original=getattr(error,'original',error)
    msg='You need **Manage Server** permission to use this command.' if isinstance(error,app_commands.MissingPermissions) else f'Something went wrong: `{type(original).__name__}: {str(original)[:600]}`'
    if interaction.response.is_done(): await interaction.followup.send(msg,ephemeral=True)
    else: await interaction.response.send_message(msg,ephemeral=True)
for cmd in (setup,status,test,latest,latest_reel,sync,disconnect): cmd.error(command_error)

async def sync_guild(guild):
    try:
        bot.tree.copy_global_to(guild=guild); await bot.tree.sync(guild=guild)
    except discord.DiscordException: log.exception('Guild command sync failed: %s',guild.id)

@bot.event
async def on_ready():
    log.info('Discord bot online as %s',bot.user)
    for guild in bot.guilds: await sync_guild(guild)

@bot.event
async def on_guild_join(guild): await sync_guild(guild)

async def setup_hook():
    bot.tree.add_command(ig); await bot.tree.sync()
    if not poll_instagram.is_running(): poll_instagram.start()
bot.setup_hook=setup_hook

# ---------- web ----------
web=Flask(__name__)
PAGE='''<!doctype html><html><body style="background:#0e1015;color:white;font-family:Arial;padding:50px"><h1>Instagram → Discord</h1><p>Hosted bot is running.</p>{% if install_url %}<a style="color:white;background:#5865f2;padding:12px 18px;text-decoration:none;border-radius:10px" href="{{install_url}}">Add to Discord</a>{% endif %}</body></html>'''
def install_url():
    if not DISCORD_CLIENT_ID: return None
    permissions=1024+2048+16384+65536+131072
    return 'https://discord.com/oauth2/authorize?'+urlencode({'client_id':DISCORD_CLIENT_ID,'scope':'bot applications.commands','permissions':permissions})
@web.get('/')
def home(): return render_template_string(PAGE,install_url=install_url())
@web.get('/health')
def health(): return {'ok':True,'discord_ready':bot.is_ready(),'poll_interval_seconds':POLL_INTERVAL_SECONDS}
def run_web(): web.run(host='0.0.0.0',port=PORT,use_reloader=False,threaded=True)

def validate():
    missing=[k for k,v in {'DISCORD_BOT_TOKEN':DISCORD_BOT_TOKEN,'DISCORD_CLIENT_ID':DISCORD_CLIENT_ID,'INSTAGRAM_ACCESS_TOKEN':INSTAGRAM_ACCESS_TOKEN,'INSTAGRAM_USER_ID':INSTAGRAM_USER_ID}.items() if not v]
    if missing: raise RuntimeError('Missing required environment variables: '+', '.join(missing))

if __name__=='__main__':
    validate(); db(); threading.Thread(target=run_web,daemon=True).start(); bot.run(DISCORD_BOT_TOKEN,log_handler=None)
