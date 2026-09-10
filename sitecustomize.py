"""Global Discord send hook for the Instagram bot.

Python imports sitecustomize automatically at startup when it is available on
sys.path. This keeps @everyone behavior centralized: any public channel.send()
performed by the bot automatically includes an @everyone notification.
"""

import discord

_original_send = discord.abc.Messageable.send


async def _send_with_everyone(self, content=None, **kwargs):
    # Avoid duplicating the mention if a future command already supplies it.
    if content:
        if "@everyone" not in str(content):
            content = f"@everyone\n{content}"
    else:
        content = "@everyone"

    # Explicitly allow the @everyone mention to parse. Discord will only notify
    # members if the bot also has the server/channel Mention Everyone permission.
    existing = kwargs.get("allowed_mentions")
    if existing is None:
        kwargs["allowed_mentions"] = discord.AllowedMentions(
            everyone=True,
            users=True,
            roles=True,
            replied_user=False,
        )
    else:
        # Preserve the caller's user/role mention policy while enabling everyone.
        kwargs["allowed_mentions"] = discord.AllowedMentions(
            everyone=True,
            users=existing.users,
            roles=existing.roles,
            replied_user=existing.replied_user,
        )

    return await _original_send(self, content=content, **kwargs)


discord.abc.Messageable.send = _send_with_everyone
