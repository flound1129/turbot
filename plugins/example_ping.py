"""Example plugin demonstrating the correct Turbot plugin pattern."""

from discord.ext import commands

from plugin_api import TurbotPlugin


class PingPlugin(TurbotPlugin):
    """Simple ping/pong command to verify plugin loading."""

    @commands.command(name="ping")
    async def ping(self, ctx: commands.Context) -> None:
        """Reply with Pong! to verify the bot is alive."""
        await ctx.send("Pong! Turbotastic!")


async def setup(bot: commands.Bot) -> None:
    """Entry point for discord.py extension loading."""
    await bot.add_cog(PingPlugin(bot))
