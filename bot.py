from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

import config as config_module
from db import Database
from embed_refresher import EmbedRefresher
from scheduler import Scheduler

cfg = config_module.load()

logging.basicConfig(
    level=getattr(logging, cfg.log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class BetBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(command_prefix="!", intents=intents)

        self.db = Database(cfg.db_path)
        self.scheduler = Scheduler(self)
        self.refresher = EmbedRefresher(self)

    async def setup_hook(self) -> None:
        # 1. Connect to DB
        await self.db.connect()

        # 2. Register persistent DynamicItems
        from views.bet_main import JoinBetButton, RefreshBetButton, CloseBetButton
        self.add_dynamic_items(JoinBetButton, RefreshBetButton, CloseBetButton)

        # 3. Load cogs
        await self.load_extension("cogs.bets")
        await self.load_extension("cogs.wallet")
        await self.load_extension("cogs.help")

        # 4. Restore pending schedules from DB
        await self.scheduler.restore()

        # 5. Sync slash commands to dev guild immediately
        dev_guild = discord.Object(id=cfg.dev_guild_id)
        self.tree.copy_global_to(guild=dev_guild)
        synced = await self.tree.sync(guild=dev_guild)
        logger.info("Synced %d command(s) to guild %d", len(synced), cfg.dev_guild_id)

    async def close(self) -> None:
        logger.info("Shutting down...")
        self.scheduler.cancel_all()
        self.refresher.cancel_all()
        await self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (id=%d)", self.user, self.user.id)


def main() -> None:
    bot = BetBot()
    bot.run(cfg.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
