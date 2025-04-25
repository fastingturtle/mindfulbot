# main.py
import discord
from discord.ext import commands, tasks
import os
import asyncpg # Database driver
from datetime import datetime, time, timezone, date
import random
import asyncio
from dotenv import load_dotenv

# --- Web Server Imports ---
from fastapi import FastAPI
import uvicorn

# --- Configuration ---
load_dotenv() # Load environment variables from .env file
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL") # Provided by Render PostgreSQL service
COMMAND_PREFIX = "!"
MINDFUL_ROLE_NAME = "MindfulTrader" # IMPORTANT: Create this role in your Discord Server
# Port for the web server (Render provides $PORT automatically)
WEB_SERVER_PORT = int(os.getenv("PORT", 8080)) # Default to 8080 for local testing if PORT not set

# List of affirmations - easily extendable
AFFIRMATIONS = [
    "I will manage my risk.",
    "I will stick to my trading plan.",
    "I will not let FOMO drive my decisions.",
    "Patience is key to profitable trading.",
    "I trade based on strategy, not emotion.",
    "I accept the risk before entering a trade.",
    "I will protect my capital.",
    "I will not revenge trade.",
    "I am disciplined in my approach.",
    "I learn from both wins and losses.",
]

# --- Bot Setup ---
# Define necessary intents
intents = discord.Intents.default()
intents.members = True          # Required to check roles
intents.message_content = True  # Required to read command content and DMs
intents.typing = True           # Required for the on_typing trigger
intents.guilds = True           # Basic guild operations

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

# --- FastAPI App Setup ---
# This app's only purpose is to respond to Render's health checks
app = FastAPI()

@app.get("/")
async def read_root():
    """Basic health check endpoint for Render."""
    return {"status": "ok", "bot_status": "running" if bot.is_ready() else "initializing"}

# Global variable for the database connection pool
db_pool = None

# --- Database Setup and Helpers ---
# (Database functions: init_db, get_mindful_channels_db, add_mindful_channel_db,
# remove_mindful_channel_db, get_user_verification_status_db, set_pending_verification_db,
# complete_verification_db, clear_stale_verifications_db remain IDENTICAL to the previous version v2)
async def init_db():
    """Initializes the database connection pool and creates tables if they don't exist."""
    global db_pool
    if not DATABASE_URL:
        print("Error: DATABASE_URL environment variable not set.")
        return False
    try:
        # Adjust pool size if needed, Render free tier might have connection limits
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        print("Database connection pool created.")
        async with db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS mindful_channels (
                    guild_id BIGINT NOT NULL,
                    channel_id BIGINT NOT NULL,
                    PRIMARY KEY (guild_id, channel_id)
                );
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_verifications (
                    user_id BIGINT PRIMARY KEY,
                    verified_date DATE NOT NULL,
                    pending_affirmation TEXT
                );
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_verified_date ON user_verifications (verified_date);
            """)
        print("Database tables checked/created.")
        return True
    except Exception as e:
        print(f"Error connecting to or initializing database: {e}")
        db_pool = None
        return False

async def get_mindful_channels_db(guild_id):
    if not db_pool: return []
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT channel_id FROM mindful_channels WHERE guild_id = $1", guild_id)
        return [row['channel_id'] for row in rows]

async def add_mindful_channel_db(guild_id, channel_id):
    if not db_pool: return False
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO mindful_channels (guild_id, channel_id) VALUES ($1, $2)
                ON CONFLICT (guild_id, channel_id) DO NOTHING;
            """, guild_id, channel_id)
        return True
    except Exception as e: print(f"Error adding mindful channel to DB: {e}"); return False

async def remove_mindful_channel_db(guild_id, channel_id):
    if not db_pool: return False
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM mindful_channels WHERE guild_id = $1 AND channel_id = $2", guild_id, channel_id)
        return True
    except Exception as e: print(f"Error removing mindful channel from DB: {e}"); return False

async def get_user_verification_status_db(user_id):
    if not db_pool: return 'none', None
    today = datetime.now(timezone.utc).date()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT verified_date, pending_affirmation FROM user_verifications WHERE user_id = $1", user_id)
        if row:
            if row['verified_date'] == today and row['pending_affirmation'] is None:
                 return 'verified', None # Verified today and NOT pending
            elif row['pending_affirmation'] is not None:
                 # Treat stale pending as 'none'
                 if row['verified_date'] != today:
                      print(f"User {user_id} has stale pending state from {row['verified_date']}. Treating as 'none'.")
                      # Clean up stale state here? Optional but good practice.
                      # await conn.execute("DELETE FROM user_verifications WHERE user_id = $1", user_id)
                      return 'none', None
                 else: # Pending state is from today
                      return 'pending', row['pending_affirmation']
            else: # Entry exists but not verified today and no pending text
                return 'none', None
        else: # No record found
            return 'none', None

async def set_pending_verification_db(user_id, affirmation):
    if not db_pool: return False
    today = datetime.now(timezone.utc).date()
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_verifications (user_id, verified_date, pending_affirmation) VALUES ($1, $2, $3)
                ON CONFLICT (user_id) DO UPDATE SET
                    verified_date = EXCLUDED.verified_date,
                    pending_affirmation = EXCLUDED.pending_affirmation;
            """, user_id, today, affirmation)
        return True
    except Exception as e: print(f"Error setting pending verification in DB for {user_id}: {e}"); return False

async def complete_verification_db(user_id):
    if not db_pool: return False
    today = datetime.now(timezone.utc).date()
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE user_verifications SET verified_date = $1, pending_affirmation = NULL WHERE user_id = $2;
            """, today, user_id)
        return True
    except Exception as e: print(f"Error completing verification in DB for {user_id}: {e}"); return False

async def clear_stale_verifications_db():
    if not db_pool: return 0
    today = datetime.now(timezone.utc).date()
    try:
        async with db_pool.acquire() as conn:
            result = await conn.execute("DELETE FROM user_verifications WHERE verified_date != $1", today)
            deleted_count = int(result.split()[-1]) if result else 0
            return deleted_count
    except Exception as e: print(f"Error clearing stale verifications from DB: {e}"); return 0

# --- Helper Functions (Discord Specific) ---
# (get_mindful_role, apply_read_lock, remove_read_lock remain IDENTICAL to previous version v2)
def get_mindful_role(guild):
    if not guild: return None
    return discord.utils.get(guild.roles, name=MINDFUL_ROLE_NAME)

async def apply_read_lock(user, channel):
    if not isinstance(user, discord.Member): return
    if not isinstance(channel, discord.TextChannel): return
    try:
        await channel.set_permissions(user, read_message_history=False, overwrite_reason="Mindful check pending")
        print(f"Applied read lock for {user.name} in {channel.name}")
    except discord.Forbidden: print(f"Error: Bot lacks permission to set permissions in {channel.name} (Guild: {channel.guild.name})")
    except discord.NotFound: print(f"Error: User {user.name} or Channel {channel.name} not found during apply_read_lock.")
    except discord.HTTPException as e: print(f"Error applying permissions for {user.name} in {channel.name}: {e}")

async def remove_read_lock(user, channel):
    if not isinstance(user, discord.Member): return
    if not isinstance(channel, discord.TextChannel): return
    try:
        overwrite = channel.overwrites_for(user)
        if overwrite.read_message_history is False:
            await channel.set_permissions(user, read_message_history=None, overwrite_reason="Mindful check complete")
            print(f"Removed read lock for {user.name} in {channel.name}")
    except discord.Forbidden: print(f"Error: Bot lacks permission to reset permissions in {channel.name} for {user.name} (or user left)")
    except discord.NotFound: print(f"Error: User {user.name} or Channel {channel.name} not found during remove_read_lock.")
    except discord.HTTPException as e: print(f"Error removing permissions for {user.name} in {channel.name}: {e}")

# --- Event Handlers ---
# (on_ready, on_typing, on_message remain IDENTICAL to previous version v2,
# just ensure they check if db_pool is None before proceeding)
@bot.event
async def on_ready():
    """Called when the bot successfully connects."""
    print(f'Logged in as {bot.user.name} ({bot.user.id})')
    print(f'Mindful Role Name: {MINDFUL_ROLE_NAME}')
    # Database initialization is now handled in main() before bot starts
    if db_pool:
         print('Database connection pool available.')
         # Start the daily reset task only if DB connection is successful
         if not daily_reset_task.is_running():
             daily_reset_task.start()
    else:
         print("WARNING: Database pool not available at on_ready. DB operations will fail.")
    print('------')

@bot.event
async def on_typing(channel, user, when):
    if not isinstance(channel, discord.TextChannel) or not channel.guild or user.bot or not isinstance(user, discord.Member): return
    if not db_pool: return # Check DB
    guild_id = channel.guild.id
    user_id = user.id
    channels_list = await get_mindful_channels_db(guild_id)
    if not channels_list: return
    if channel.id in channels_list:
        mindful_role = get_mindful_role(channel.guild)
        if mindful_role and mindful_role in user.roles:
            verification_status, pending_affirmation = await get_user_verification_status_db(user_id)
            if verification_status == 'none':
                print(f"User {user.name} typing in {channel.name}, needs verification.")
                await apply_read_lock(user, channel)
                try:
                    await channel.send(f"🔒 Reading/interacting requires check-in, {user.mention}. Check DMs.", delete_after=60.0, ephemeral=True)
                except Exception as e: print(f"Warning: Ephemeral send failed: {e}")
                try:
                    affirmation_text = random.choice(AFFIRMATIONS)
                    if await set_pending_verification_db(user_id, affirmation_text):
                        dm_channel = await user.create_dm()
                        await dm_channel.send(f"Hi {user.display_name}! To unlock `#{channel.name}`, reply with:\n\n**{affirmation_text}**")
                        print(f"Sent verification DM to {user.name}.")
                    else: print(f"Error: Failed to set pending DB state for {user.name}.")
                except discord.Forbidden: print(f"Error: Could not send DM to {user.name}.") # Consider clearing pending state from DB here
                except Exception as e: print(f"Error sending DM/setting pending state for {user.name}: {e}") # Consider clearing pending state
            elif verification_status == 'pending':
                 print(f"User {user.name} typing in {channel.name}, verification already pending. Re-applying lock.")
                 await apply_read_lock(user, channel)

@bot.event
async def on_message(message):
    if message.author == bot.user: return
    if not db_pool: # Check DB
        if message.content.startswith(COMMAND_PREFIX): print("DB Pool not available, ignoring command.")
        return
    if message.guild: await bot.process_commands(message)
    if isinstance(message.channel, discord.DMChannel) and not message.content.startswith(COMMAND_PREFIX):
        user_id = message.author.id
        verification_status, pending_affirmation = await get_user_verification_status_db(user_id)
        if verification_status == 'pending' and pending_affirmation:
            if message.content.strip().lower() == pending_affirmation.lower():
                print(f"User {message.author.name} successfully verified via DM.")
                if await complete_verification_db(user_id):
                    for guild in message.author.mutual_guilds:
                        guild_id_loop = guild.id
                        member = guild.get_member(user_id)
                        if member:
                            channels_list = await get_mindful_channels_db(guild_id_loop)
                            for channel_id in channels_list:
                                channel = guild.get_channel(channel_id)
                                if channel and isinstance(channel, discord.TextChannel):
                                    bot_member = guild.me
                                    perms = channel.permissions_for(bot_member)
                                    if perms.manage_roles: await remove_read_lock(member, channel)
                                    else: print(f"Warning: Bot lacks Manage Roles in {channel.name} ({guild.name}).")
                                elif not channel: print(f"Warning: Mindful channel ID {channel_id} not found in {guild.name}.")
                    try: await message.channel.send("✅ Affirmation complete! Access granted for today.")
                    except discord.Forbidden: pass
                else:
                    print(f"Error: Failed to complete verification in DB for {user_id}.")
                    try: await message.channel.send("⚠️ Error saving verification. Try typing in channel again later.")
                    except discord.Forbidden: pass
            else:
                 try:
                    await asyncio.sleep(1)
                    await message.channel.send(f"❌ Incorrect. Reply with:\n\n**{pending_affirmation}**")
                 except discord.Forbidden: pass

# --- Admin Commands (Using DB) ---
# (add_mindful_channel, remove_mindful_channel, list_mindful_channels commands remain IDENTICAL to previous version v2)
@bot.command(name="addMindfulChannel", help="Adds a channel to the mindful check list. Usage: !addMindfulChannel #channel-name")
@commands.has_permissions(administrator=True) @commands.guild_only()
async def add_mindful_channel(ctx, channel: discord.TextChannel):
    if not db_pool: await ctx.send("❌ DB connection unavailable."); return
    if await add_mindful_channel_db(ctx.guild.id, channel.id):
        await ctx.send(f"✅ Channel {channel.mention} added.")
        print(f"Admin {ctx.author.name} added {channel.name} ({channel.id}) for guild {ctx.guild.name}")
    else: await ctx.send(f"⚠️ Failed to add {channel.mention}.")

@bot.command(name="removeMindfulChannel", help="Removes a channel from the mindful check list. Usage: !removeMindfulChannel #channel-name")
@commands.has_permissions(administrator=True) @commands.guild_only()
async def remove_mindful_channel(ctx, channel: discord.TextChannel):
    if not db_pool: await ctx.send("❌ DB connection unavailable."); return
    current_channels = await get_mindful_channels_db(ctx.guild.id)
    if channel.id not in current_channels: await ctx.send(f"ℹ️ {channel.mention} not on list."); return
    if await remove_mindful_channel_db(ctx.guild.id, channel.id):
        await ctx.send(f"✅ Channel {channel.mention} removed.")
        print(f"Admin {ctx.author.name} removed {channel.name} ({channel.id}) for guild {ctx.guild.name}")
        mindful_role = get_mindful_role(ctx.guild)
        if mindful_role:
            bot_member = ctx.guild.me
            perms = channel.permissions_for(bot_member)
            if perms.manage_roles:
                 print(f"Cleaning permissions for {channel.mention}...")
                 members_with_role = [m async for m in ctx.guild.fetch_members(limit=None) if mindful_role in m.roles] # More efficient fetch
                 for member in members_with_role: await remove_read_lock(member, channel)
            else: print(f"Warning: Bot lacks Manage Roles in {channel.name}, cannot clean up locks.")
    else: await ctx.send(f"⚠️ Failed to remove {channel.mention}.")

@bot.command(name="listMindfulChannels", help="Lists all channels currently on the mindful check list.")
@commands.has_permissions(administrator=True) @commands.guild_only()
async def list_mindful_channels(ctx):
    if not db_pool: await ctx.send("❌ DB connection unavailable."); return
    channel_ids = await get_mindful_channels_db(ctx.guild.id)
    if channel_ids:
        channel_mentions = []
        valid_channel_ids = []
        needs_cleanup = False
        for channel_id in channel_ids:
            channel = ctx.guild.get_channel(channel_id)
            if channel: channel_mentions.append(channel.mention); valid_channel_ids.append(channel_id)
            else: channel_mentions.append(f"*(ID: {channel_id} - Not Found)*"); needs_cleanup = True
        message = "Mindful check channels:\n- " + "\n- ".join(channel_mentions)
        await ctx.send(message)
        if needs_cleanup:
             print(f"Cleaning deleted channels for guild {ctx.guild.name}...")
             async with db_pool.acquire() as conn:
                  await conn.execute("DELETE FROM mindful_channels WHERE guild_id = $1", ctx.guild.id)
                  if valid_channel_ids:
                       data_to_insert = [(ctx.guild.id, ch_id) for ch_id in valid_channel_ids]
                       await conn.executemany("INSERT INTO mindful_channels (guild_id, channel_id) VALUES ($1, $2)", data_to_insert)
             await ctx.send("*(Note: Cleaned up channels not found.)*")
    else: await ctx.send("ℹ️ No mindful check channels configured.")

# --- Error Handler ---
# (mindful_command_error remains IDENTICAL to previous version v2)
@add_mindful_channel.error
@remove_mindful_channel.error
@list_mindful_channels.error
async def mindful_command_error(ctx, error):
    if isinstance(error, commands.CommandInvokeError) and isinstance(error.original, asyncpg.PostgresError):
         await ctx.send("❌ DB error occurred."); print(f"DB Error: {error.original}")
    elif isinstance(error, commands.MissingPermissions): await ctx.send("❌ Admin permission required.")
    elif isinstance(error, commands.MissingRequiredArgument): await ctx.send(f"❌ Missing argument. Usage: `{COMMAND_PREFIX}help {ctx.command.name}`")
    elif isinstance(error, commands.ChannelNotFound): await ctx.send(f"❌ Channel not found: {error.argument}")
    elif isinstance(error, commands.NoPrivateMessage): await ctx.send("❌ Command unavailable in DMs.")
    elif isinstance(error, commands.CommandInvokeError) and isinstance(error.original, discord.Forbidden):
         await ctx.send("❌ Bot lacks permissions (Manage Roles?)."); print(f"Permission Error: {error.original}")
    else: await ctx.send("An unexpected error occurred."); print(f"Command Error: {error}")

# --- Daily Reset Task (Using DB) ---
# (daily_reset_task remains IDENTICAL to previous version v2)
RESET_TIME = time(hour=0, minute=0, second=0, tzinfo=timezone.utc)
@tasks.loop(time=RESET_TIME)
async def daily_reset_task():
    await bot.wait_until_ready()
    if not db_pool: print("Daily Reset: DB pool unavailable."); return
    print(f"--- Running Daily Reset Task at {datetime.now(timezone.utc)} ---")
    cleared_count = await clear_stale_verifications_db()
    print(f"Cleared verification status for {cleared_count} users from DB.")

# --- Main Execution ---
async def run_bot():
    """Starts the discord bot."""
    try:
        await bot.start(BOT_TOKEN)
    except discord.LoginFailure:
        print("Error: Invalid Discord Token.")
    except Exception as e:
        print(f"Error running discord bot: {e}")
    finally:
        if not bot.is_closed():
            await bot.close()
        if db_pool:
            await db_pool.close()
            print("Database pool closed.")

async def run_web_server():
    """Starts the FastAPI web server using uvicorn."""
    config = uvicorn.Config(app, host="0.0.0.0", port=WEB_SERVER_PORT, log_level="info")
    server = uvicorn.Server(config)
    print(f"Starting web server on port {WEB_SERVER_PORT}...")
    try:
        # Run the server within the existing async loop
        await server.serve()
    except Exception as e:
        print(f"Error running web server: {e}")
    finally:
        print("Web server stopped.")


async def main():
    """Initializes DB and runs Bot and Web Server concurrently."""
    if BOT_TOKEN is None: print("Error: DISCORD_BOT_TOKEN not found."); return
    if DATABASE_URL is None: print("Error: DATABASE_URL not found."); return

    if not await init_db():
        print("Exiting due to database initialization failure.")
        return

    # Create tasks for bot and web server
    bot_task = asyncio.create_task(run_bot())
    web_server_task = asyncio.create_task(run_web_server())

    # Keep main running until one task finishes (or is cancelled)
    done, pending = await asyncio.wait(
        [bot_task, web_server_task],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Cancel pending tasks if one finishes/errors
    for task in pending:
        task.cancel()

    print("Main loop finished.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutdown requested.")
    except Exception as e:
        print(f"Critical error in main execution: {e}")