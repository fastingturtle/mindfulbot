# main.py
import discord
from discord.ext import commands, tasks
import os
import asyncpg # Database driver
from datetime import datetime, time, timezone, date
import random
import asyncio
from dotenv import load_dotenv

# --- Timezone Imports (for DST handling) ---
try:
    from zoneinfo import ZoneInfo
except ImportError:
    # Fallback for Python < 3.9 (requires installing backports.zoneinfo)
    # Consider adding 'backports.zoneinfo; python_version < "3.9"' to requirements.txt
    # For simplicity, we'll assume Python 3.9+ for now. If using older Python,
    # you might need pytz or handle this differently.
    print("Warning: zoneinfo module not found. Timezone handling might be inaccurate without backports.zoneinfo on Python < 3.9.")
    # As a basic fallback, revert to fixed UTC time if zoneinfo isn't available
    ZoneInfo = None


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

# --- Daily Reset Time Configuration ---
TARGET_RESET_TIMEZONE = "America/New_York" # Timezone for reset check
TARGET_RESET_HOUR = 9 # 9 AM
TARGET_RESET_MINUTE = 0

# --- Global State for Reset Task ---
# Initialize to a date far in the past to ensure the first run happens
last_reset_run_date = date.min


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
    # Check if bot is initialized and ready
    bot_state = "initializing"
    if 'bot' in globals() and isinstance(bot, commands.Bot):
        bot_state = "running" if bot.is_ready() else "logged_in_not_ready"
    return {"status": "ok", "bot_status": bot_state}

# Global variable for the database connection pool
db_pool = None

# --- Database Setup and Helpers ---
# (Database functions: init_db, get_mindful_channels_db, add_mindful_channel_db,
# remove_mindful_channel_db, get_user_verification_status_db, set_pending_verification_db,
# complete_verification_db, clear_stale_verifications_db remain IDENTICAL to the previous version)
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
            # Ensure mindful_channels table exists
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS mindful_channels (
                    guild_id BIGINT NOT NULL,
                    channel_id BIGINT NOT NULL,
                    PRIMARY KEY (guild_id, channel_id)
                );
            """)
            # Ensure user_verifications table exists
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_verifications (
                    user_id BIGINT PRIMARY KEY,
                    verified_date DATE NOT NULL,
                    pending_affirmation TEXT
                );
            """)
            # Ensure index exists on verified_date for faster resets
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_verified_date ON user_verifications (verified_date);
            """)
        print("Database tables checked/created.")
        return True
    except Exception as e:
        print(f"Error connecting to or initializing database: {e}")
        db_pool = None # Ensure pool is None if connection failed
        return False

async def get_mindful_channels_db(guild_id):
    """Fetches the list of mindful channel IDs for a specific guild from the DB."""
    if not db_pool: return [] # Return empty list if DB pool not ready
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT channel_id FROM mindful_channels WHERE guild_id = $1", guild_id)
            return [row['channel_id'] for row in rows]
    except Exception as e:
        print(f"Error fetching mindful channels from DB: {e}")
        return [] # Return empty list on error

async def add_mindful_channel_db(guild_id, channel_id):
    """Adds a mindful channel to the database."""
    if not db_pool: return False
    try:
        async with db_pool.acquire() as conn:
            # Use INSERT ... ON CONFLICT DO NOTHING to avoid errors if already exists
            await conn.execute("""
                INSERT INTO mindful_channels (guild_id, channel_id)
                VALUES ($1, $2)
                ON CONFLICT (guild_id, channel_id) DO NOTHING;
            """, guild_id, channel_id)
        return True
    except Exception as e:
        print(f"Error adding mindful channel to DB: {e}")
        return False

async def remove_mindful_channel_db(guild_id, channel_id):
    """Removes a mindful channel from the database."""
    if not db_pool: return False
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM mindful_channels WHERE guild_id = $1 AND channel_id = $2", guild_id, channel_id)
        return True
    except Exception as e:
        print(f"Error removing mindful channel from DB: {e}")
        return False

async def get_user_verification_status_db(user_id):
    """
    Checks user verification status from DB.
    Returns: 'verified', 'pending', or 'none'.
    Also returns the pending affirmation if status is 'pending'.
    """
    if not db_pool: return 'none', None # Return default if DB pool not ready
    today_utc = datetime.now(timezone.utc).date() # Get current date in UTC
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT verified_date, pending_affirmation FROM user_verifications WHERE user_id = $1", user_id)
            if row:
                db_date = row['verified_date'] # This date is stored based on UTC day of verification
                pending_text = row['pending_affirmation']

                # Case 1: Verified today (date matches UTC today, no pending text)
                if db_date == today_utc and pending_text is None:
                    return 'verified', None
                # Case 2: Pending today (date matches UTC today, has pending text)
                elif db_date == today_utc and pending_text is not None:
                     return 'pending', pending_text
                # Case 3: Pending from previous day (stale) - treat as 'none'
                elif db_date != today_utc and pending_text is not None:
                    print(f"User {user_id} has stale pending state from {db_date}. Treating as 'none'.")
                    return 'none', None
                # Case 4: Verified on a previous day (not today, no pending text) - treat as 'none'
                else:
                    return 'none', None
            else: # No record found for the user
                return 'none', None
    except Exception as e:
        print(f"Error fetching user verification status from DB: {e}")
        return 'none', None # Return default on error

async def set_pending_verification_db(user_id, affirmation):
    """Sets user status to pending in DB."""
    if not db_pool: return False
    # Use today's UTC date even for pending.
    today_utc = datetime.now(timezone.utc).date()
    try:
        async with db_pool.acquire() as conn:
            # UPSERT: Insert or Update if user_id already exists
            await conn.execute("""
                INSERT INTO user_verifications (user_id, verified_date, pending_affirmation)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id) DO UPDATE SET
                    verified_date = EXCLUDED.verified_date,
                    pending_affirmation = EXCLUDED.pending_affirmation;
            """, user_id, today_utc, affirmation)
        return True
    except Exception as e:
        print(f"Error setting pending verification in DB for {user_id}: {e}")
        return False

async def complete_verification_db(user_id):
    """Marks user as verified for today in DB by removing pending text."""
    if not db_pool: return False
    today_utc = datetime.now(timezone.utc).date()
    try:
        async with db_pool.acquire() as conn:
            # Update date and remove pending affirmation text
            # Ensure we update the date to today's UTC date upon completion
            await conn.execute("""
                UPDATE user_verifications
                SET verified_date = $1, pending_affirmation = NULL
                WHERE user_id = $2;
            """, today_utc, user_id)
        return True
    except Exception as e:
        print(f"Error completing verification in DB for {user_id}: {e}")
        return False

async def clear_stale_verifications_db():
    """Clears users whose verification date is not today (UTC)."""
    if not db_pool: return 0
    today_utc = datetime.now(timezone.utc).date()
    try:
        async with db_pool.acquire() as conn:
            # Delete rows where the verification date is not today's UTC date
            result = await conn.execute("DELETE FROM user_verifications WHERE verified_date != $1", today_utc)
            # result string is like 'DELETE N', parse N
            deleted_count = int(result.split()[-1]) if result and result.startswith("DELETE") else 0
            return deleted_count
    except Exception as e:
        print(f"Error clearing stale verifications from DB: {e}")
        return 0

# --- Helper Functions (Discord Specific) ---
def get_mindful_role(guild):
    """Gets the mindful role object from the guild."""
    if not guild: return None
    return discord.utils.get(guild.roles, name=MINDFUL_ROLE_NAME)

async def apply_read_lock(user, channel):
    """Applies the read message history lock for a user on a channel."""
    # Input validation
    if not isinstance(user, discord.Member):
         print(f"Warning: apply_read_lock called with non-Member object: {user}")
         return
    if not isinstance(channel, discord.TextChannel):
         print(f"Warning: apply_read_lock called with non-TextChannel object: {channel}")
         return

    try:
        # Apply permission overwrite to deny reading message history
        await channel.set_permissions(user, read_message_history=False, overwrite_reason="Mindful check pending")
        print(f"Applied read lock for {user.name} in {channel.name}")
    except discord.Forbidden: # Bot lacks permissions
        print(f"Error: Bot lacks permission to set permissions in {channel.name} (Guild: {channel.guild.name})")
    except discord.NotFound: # User or Channel no longer exists
        print(f"Error: User {user.name} or Channel {channel.name} not found during apply_read_lock.")
    except discord.HTTPException as e: # Other Discord API errors
        print(f"Error applying permissions for {user.name} in {channel.name}: {e}")
    except Exception as e: # Catch any other unexpected errors
        print(f"Unexpected error in apply_read_lock for {user.name} in {channel.name}: {e}")


async def remove_read_lock(user, channel):
    """Removes the read message history lock for a user on a channel."""
    # Input validation
    if not isinstance(user, discord.Member):
         print(f"Warning: remove_read_lock called with non-Member object: {user}")
         return
    if not isinstance(channel, discord.TextChannel):
         print(f"Warning: remove_read_lock called with non-TextChannel object: {channel}")
         return

    try:
        # Get current overwrites for the user in the channel
        overwrite = channel.overwrites_for(user)
        # Only remove the lock if it's explicitly set to False
        if overwrite.read_message_history is False:
            # Reset permission to None (inherits from roles/default)
            await channel.set_permissions(user, read_message_history=None, overwrite_reason="Mindful check complete")
            print(f"Removed read lock for {user.name} in {channel.name}")
        # else: No specific deny overwrite exists, do nothing.
    except discord.Forbidden: # Bot lacks permissions
        print(f"Error: Bot lacks permission to reset permissions in {channel.name} for {user.name} (or user left)")
    except discord.NotFound: # User or Channel no longer exists
         print(f"Error: User {user.name} or Channel {channel.name} not found during remove_read_lock.")
    except discord.HTTPException as e: # Other Discord API errors
        print(f"Error removing permissions for {user.name} in {channel.name}: {e}")
    except Exception as e: # Catch any other unexpected errors
        print(f"Unexpected error in remove_read_lock for {user.name} in {channel.name}: {e}")


# --- Event Handlers ---
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
             # Log the check frequency, not a specific time
             print(f"Daily reset check task started (runs every 5 minutes).")
    else:
         # This case should ideally not happen if main() checks init_db()
         print("CRITICAL WARNING: Database pool not available at on_ready. DB operations will fail.")
    print('------')

@bot.event
async def on_typing(channel, user, when):
    """Triggered when a user starts typing in a mindful channel."""
    # Basic checks: ignore DMs, bots, non-members, ensure DB is ready
    if not isinstance(channel, discord.TextChannel) or not channel.guild or user.bot or not isinstance(user, discord.Member):
        return
    if not db_pool:
        return # Silently return if DB is not ready

    guild_id = channel.guild.id
    user_id = user.id

    try:
        # Fetch mindful channels for this guild *from DB*
        channels_list = await get_mindful_channels_db(guild_id)
        if not channels_list or channel.id not in channels_list:
            return # Not a mindful channel for this guild

        # Check if the user has the mindful role
        mindful_role = get_mindful_role(channel.guild)
        if not mindful_role or mindful_role not in user.roles:
            return # User doesn't have the role

        # Check user status *from DB*
        verification_status, pending_affirmation = await get_user_verification_status_db(user_id)

        # Action needed only if status is 'none' (needs verification)
        if verification_status == 'none':
            print(f"User {user.name} typing in {channel.name}, needs verification.")

            # 1. Apply Lock
            await apply_read_lock(user, channel)

            # 2. Send Ephemeral Message (best effort)
            try:
                await channel.send(
                    f"🔒 Reading and interacting in this channel requires a quick daily check-in, {user.mention}. Please check your DMs to complete your affirmation.",
                    delete_after=60.0, ephemeral=True
                )
            except Exception as e: # Catch broad exceptions for ephemeral send
                 print(f"Warning: Could not send ephemeral message in {channel.name}: {e}")

            # 3. Send DM & Set Pending State in DB
            try:
                affirmation_text = random.choice(AFFIRMATIONS)
                # Set pending state *before* sending DM
                if await set_pending_verification_db(user_id, affirmation_text):
                    dm_channel = await user.create_dm()
                    await dm_channel.send(
                        f"Hi {user.display_name}! To unlock reading mindful channels like `#{channel.name}` for today, "
                        f"please reply with the following affirmation:\n\n"
                        f"**{affirmation_text}**"
                    )
                    print(f"Sent verification DM to {user.name}.")
                else:
                     print(f"Error: Failed to set pending state in DB for {user.name}. Aborting DM.")
                     # Should we remove the lock if DB fails? Maybe.
                     # await remove_read_lock(user, channel)

            except discord.Forbidden: # Cannot DM user
                print(f"Error: Could not send DM to {user.name}. They might have DMs disabled.")
                # DM failed, clear the pending state we just set in DB
                async with db_pool.acquire() as conn:
                    await conn.execute("DELETE FROM user_verifications WHERE user_id = $1", user_id)
                # Lock remains applied. User needs to enable DMs or contact admin.

            except Exception as e: # Other errors during DM/DB pending state
                print(f"An unexpected error occurred sending DM or setting pending state for {user.name}: {e}")
                # Clean up DB state if something went wrong
                async with db_pool.acquire() as conn:
                    await conn.execute("DELETE FROM user_verifications WHERE user_id = $1", user_id)
                # Maybe remove lock too? Depends on desired failure behavior.


        elif verification_status == 'pending':
             # Already pending, maybe re-apply lock just in case it was manually removed
             print(f"User {user.name} typing in {channel.name}, verification already pending. Re-applying lock.")
             await apply_read_lock(user, channel)
             # Do NOT resend DM.

        # else: status is 'verified', do nothing.

    except Exception as e:
        print(f"Error during on_typing event processing for {user.name} in {channel.name}: {e}")


@bot.event
async def on_message(message):
    """Handles incoming messages for commands and DM responses."""
    # Ignore self
    if message.author == bot.user:
        return

    # Check DB connection before processing anything
    if not db_pool:
        # Maybe notify user if it's a command attempt?
        if message.content.startswith(COMMAND_PREFIX) and message.guild:
            print("DB Pool not available, ignoring command.")
            try:
                await message.channel.send("Sorry, I'm having trouble connecting to my database right now. Please try again later.", delete_after=10)
            except Exception: pass # Ignore if can't send message
        return # Don't process further if DB is down

    # Process commands if in a guild
    if message.guild:
        await bot.process_commands(message)
        # Return after processing commands to avoid treating commands as DM responses below
        # if message.content.startswith(COMMAND_PREFIX): # More explicit check
        #    return

    # Process potential DM affirmation responses
    if isinstance(message.channel, discord.DMChannel):
        # Ignore potential commands in DMs if any exist
        if message.content.startswith(COMMAND_PREFIX):
            return

        user_id = message.author.id
        try:
            verification_status, pending_affirmation = await get_user_verification_status_db(user_id)

            # Only process if user status is 'pending' and we have the affirmation text
            if verification_status == 'pending' and pending_affirmation:
                # Compare submitted affirmation with stored pending one (case-insensitive)
                if message.content.strip().lower() == pending_affirmation.lower():
                    print(f"User {message.author.name} successfully verified via DM.")

                    # 1. Mark as verified in DB
                    if await complete_verification_db(user_id):
                        # 2. Remove locks from all mindful channels in relevant guilds
                        # Iterate through guilds the bot and user share
                        processed_guilds = set() # Avoid processing same guild multiple times if user is in many
                        async for guild in bot.fetch_guilds(limit=None): # More reliable than mutual_guilds sometimes
                             if guild.id in processed_guilds: continue
                             member = guild.get_member(user_id) # Check if user is in this guild
                             if member:
                                  processed_guilds.add(guild.id)
                                  guild_id_loop = guild.id
                                  channels_list = await get_mindful_channels_db(guild_id_loop)
                                  for channel_id in channels_list:
                                       channel = guild.get_channel(channel_id)
                                       if channel and isinstance(channel, discord.TextChannel):
                                            # Check bot permissions before attempting removal
                                            bot_member = guild.me
                                            perms = channel.permissions_for(bot_member)
                                            if perms.manage_roles: # manage_roles implies manage_permissions
                                                 await remove_read_lock(member, channel)
                                            else:
                                                 print(f"Warning: Bot lacks Manage Roles permission in {channel.name} ({guild.name}), cannot remove lock.")
                                       # Removed check for non-existent channels here, get_mindful_channels_db handles it

                        # 3. Send confirmation DM (best effort)
                        try:
                            await message.channel.send("✅ Affirmation complete! Access granted to mindful channels for today. Remember to trade responsibly!")
                        except discord.Forbidden: pass # Ignore if can't send confirmation
                    else: # Failed to update DB status
                         print(f"Error: Failed to complete verification in DB for {user_id}.")
                         try:
                              await message.channel.send("⚠️ An error occurred while saving your verification. Please try typing in the channel again later.")
                         except discord.Forbidden: pass

                # Handle incorrect affirmation
                else:
                     try:
                        await asyncio.sleep(1) # Prevent spamming attempts
                        await message.channel.send(
                            f"❌ That wasn't quite right. Please reply with the exact affirmation:\n\n"
                            f"**{pending_affirmation}**"
                        )
                     except discord.Forbidden: pass # Ignore if can't send guidance
            # else: User sent DM but wasn't pending verification, ignore silently.

        except Exception as e:
            print(f"Error processing DM from {message.author.name}: {e}")


# --- Admin Commands (Using DB) ---

# FIX: Apply decorators on separate lines
@bot.command(name="addMindfulChannel", help="Adds a channel to the mindful check list. Usage: !addMindfulChannel #channel-name")
@commands.has_permissions(administrator=True)
@commands.guild_only()
async def add_mindful_channel(ctx, channel: discord.TextChannel):
    """Adds a channel to the database."""
    if not db_pool:
        await ctx.send("❌ Database connection is not available.")
        return
    if await add_mindful_channel_db(ctx.guild.id, channel.id):
        await ctx.send(f"✅ Channel {channel.mention} added to the mindful check list.")
        print(f"Admin {ctx.author.name} added channel {channel.name} ({channel.id}) to mindful list for guild {ctx.guild.name}")
    else:
         await ctx.send(f"⚠️ Failed to add {channel.mention} to the database.")


# FIX: Apply decorators on separate lines
@bot.command(name="removeMindfulChannel", help="Removes a channel from the mindful check list. Usage: !removeMindfulChannel #channel-name")
@commands.has_permissions(administrator=True)
@commands.guild_only()
async def remove_mindful_channel(ctx, channel: discord.TextChannel):
    """Removes a channel from the database."""
    if not db_pool:
        await ctx.send("❌ Database connection is not available.")
        return

    # Check if it exists before attempting removal for better feedback
    current_channels = await get_mindful_channels_db(ctx.guild.id)
    if channel.id not in current_channels:
         await ctx.send(f"ℹ️ Channel {channel.mention} was not found on the mindful check list.")
         return

    if await remove_mindful_channel_db(ctx.guild.id, channel.id):
        await ctx.send(f"✅ Channel {channel.mention} removed from the mindful check list.")
        print(f"Admin {ctx.author.name} removed channel {channel.name} ({channel.id}) from mindful list for guild {ctx.guild.name}")

        # Cleanup permissions for users with the role
        mindful_role = get_mindful_role(ctx.guild)
        if mindful_role:
            bot_member = ctx.guild.me
            perms = channel.permissions_for(bot_member)
            if perms.manage_roles:
                 print(f"Cleaning up permissions for {channel.mention}...")
                 # Iterate through members with the role (more efficient than fetch_members if guild cache is populated)
                 for member in mindful_role.members:
                      await remove_read_lock(member, channel)
                 # Fallback if cache might be incomplete (less efficient but safer)
                 # members_with_role = [m async for m in ctx.guild.fetch_members(limit=None) if mindful_role in m.roles]
                 # for member in members_with_role: await remove_read_lock(member, channel)
            else:
                 print(f"Warning: Bot lacks Manage Roles permission in {channel.name}, cannot clean up locks on removal.")
    else:
        await ctx.send(f"⚠️ Failed to remove {channel.mention} from the database.")


# FIX: Apply decorators on separate lines
@bot.command(name="listMindfulChannels", help="Lists all channels currently on the mindful check list.")
@commands.has_permissions(administrator=True)
@commands.guild_only()
async def list_mindful_channels(ctx):
    """Lists the channels requiring affirmation from the database."""
    if not db_pool:
        await ctx.send("❌ Database connection is not available.")
        return

    channel_ids = await get_mindful_channels_db(ctx.guild.id)
    if channel_ids:
        channel_mentions = []
        valid_channel_ids = []
        needs_cleanup = False
        for channel_id in channel_ids:
            channel = ctx.guild.get_channel(channel_id) # Use cache lookup first
            if not channel: # Fallback if not in cache (less likely with intents)
                 try:
                      channel = await bot.fetch_channel(channel_id)
                 except (discord.NotFound, discord.Forbidden):
                      channel = None # Channel deleted or bot lacks access

            if channel:
                channel_mentions.append(channel.mention)
                valid_channel_ids.append(channel_id)
            else:
                channel_mentions.append(f"*(ID: {channel_id} - Not Found / Deleted?)*")
                needs_cleanup = True # Mark that a deleted channel was found

        if not channel_mentions: # All channels were invalid
             await ctx.send("ℹ️ No valid mindful check channels configured (list may need cleanup).")
             # Trigger cleanup if needed
             if needs_cleanup:
                  print(f"Cleaning deleted channels for guild {ctx.guild.name}...")
                  async with db_pool.acquire() as conn:
                       await conn.execute("DELETE FROM mindful_channels WHERE guild_id = $1", ctx.guild.id)
             return

        message = "Channels currently requiring the daily mindful check:\n- " + "\n- ".join(channel_mentions)
        await ctx.send(message)

        # Perform cleanup if needed after sending the list
        if needs_cleanup:
             print(f"Cleaning deleted channels for guild {ctx.guild.name}...")
             # Remove all for the guild, then re-add only the valid ones
             async with db_pool.acquire() as conn:
                  await conn.execute("DELETE FROM mindful_channels WHERE guild_id = $1", ctx.guild.id)
                  if valid_channel_ids:
                       # Prepare data for executemany [(guild_id, channel_id), ...]
                       data_to_insert = [(ctx.guild.id, ch_id) for ch_id in valid_channel_ids]
                       await conn.executemany("INSERT INTO mindful_channels (guild_id, channel_id) VALUES ($1, $2)", data_to_insert)
             await ctx.send("*(Note: Some channels were not found and have been removed from the list.)*")

    else: # No channels configured at all
        await ctx.send("ℹ️ No channels are currently configured for the mindful check in this server.")


# --- Error Handler ---
@add_mindful_channel.error
@remove_mindful_channel.error
@list_mindful_channels.error
async def mindful_command_error(ctx, error):
    """Handles errors for the admin commands."""
    if isinstance(error, commands.CommandInvokeError) and isinstance(error.original, asyncpg.PostgresError):
         await ctx.send("❌ A database error occurred. Please try again later or contact the administrator.")
         print(f"Database Error in command {ctx.command.name}: {error.original}")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You do not have permission to use this command (Administrator required).")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument. Please check the command usage: `{ctx.prefix}help {ctx.command.name}`")
    elif isinstance(error, commands.ChannelNotFound):
         await ctx.send(f"❌ Channel not found: {error.argument}")
    elif isinstance(error, commands.NoPrivateMessage):
         await ctx.send("❌ This command cannot be used in Direct Messages.")
    elif isinstance(error, commands.CommandInvokeError) and isinstance(error.original, discord.Forbidden):
         # More specific feedback if possible
         await ctx.send("❌ Bot lacks the necessary Discord permissions (likely `Manage Roles`/`Manage Permissions`) to perform this action.")
         print(f"Permission Error in command {ctx.command.name}: {error.original}")
    elif isinstance(error, commands.CommandNotFound):
         # Ignore CommandNotFound errors silently in the main handler
         pass
    else:
        await ctx.send("An unexpected error occurred while executing the command.")
        print(f"Unhandled error in command {ctx.command.name}: {error}")


# --- Daily Reset Task (Using Timezone Check) ---

# Run loop frequently (e.g., every 5 minutes)
@tasks.loop(minutes=5)
async def daily_reset_task():
    """Checks time and runs reset logic once per day at target time in target timezone."""
    global last_reset_run_date
    await bot.wait_until_ready() # Ensure bot is connected and ready
    if not db_pool:
        # print("Daily Reset Check: Database pool not available. Skipping.") # Avoid log spam
        return
    if ZoneInfo is None:
        print("Daily Reset Check: ZoneInfo not available. Cannot perform timezone-aware reset.")
        # Optionally, fall back to a fixed UTC time check here if desired
        return

    try:
        # Get the target timezone
        target_tz = ZoneInfo(TARGET_RESET_TIMEZONE)
        # Get the current time in the target timezone
        now_local = datetime.now(target_tz)
        # Get the current date in the target timezone
        today_local = now_local.date()

        # Define the target reset time for today in the local timezone
        target_time_today = datetime.combine(today_local,
                                             time(hour=TARGET_RESET_HOUR, minute=TARGET_RESET_MINUTE),
                                             tzinfo=target_tz)

        # Check conditions:
        # 1. Is it past the target reset time today?
        # 2. Has the reset already run for today's date in the target timezone?
        if now_local >= target_time_today and last_reset_run_date < today_local:
            print(f"--- Running Daily Reset Task (Detected time >= {TARGET_RESET_HOUR}:{TARGET_RESET_MINUTE:02d} {TARGET_RESET_TIMEZONE}) ---")
            try:
                cleared_count = await clear_stale_verifications_db()
                print(f"Cleared verification status for {cleared_count} users from DB.")
                # IMPORTANT: Update the last run date *after* successful execution
                last_reset_run_date = today_local
                print(f"Reset task marked as run for {today_local}")
            except Exception as e:
                print(f"Error during daily reset database operation: {e}")
                # Do not update last_reset_run_date if clearing failed

    except Exception as e:
        print(f"Error during daily reset time check: {e}")


# --- Main Execution ---
async def run_bot():
    """Starts the discord bot and handles cleanup."""
    try:
        print("Attempting to log in discord bot...")
        await bot.start(BOT_TOKEN)
    except discord.LoginFailure:
        print("Error: Invalid Discord Token.")
    except Exception as e:
        print(f"Error running discord bot: {e}")
    finally:
        print("Discord bot task concluding...")
        if not bot.is_closed():
            print("Closing discord bot connection...")
            await bot.close()
        # Database pool closed in main() cleanup

async def run_web_server():
    """Starts the FastAPI web server using uvicorn."""
    # Configure uvicorn to run the FastAPI app
    config = uvicorn.Config(app, host="0.0.0.0", port=WEB_SERVER_PORT, log_level="info")
    server = uvicorn.Server(config)
    print(f"Starting web server on port {WEB_SERVER_PORT}...")
    try:
        # Run the server within the existing async loop managed by asyncio.run()
        await server.serve()
    except asyncio.CancelledError:
         print("Web server task cancelled.")
    except Exception as e:
        print(f"Error running web server: {e}")
    finally:
        # Uvicorn handles its own shutdown gracefully on await server.serve() completion/cancellation
        print("Web server stopped.")


async def main():
    """Initializes DB and runs Bot and Web Server concurrently."""
    # Validate essential environment variables
    if BOT_TOKEN is None: print("CRITICAL: DISCORD_BOT_TOKEN not found."); return
    if DATABASE_URL is None: print("CRITICAL: DATABASE_URL not found."); return

    # Initialize Database Pool
    if not await init_db():
        print("CRITICAL: Exiting due to database initialization failure.")
        return # Don't proceed if DB connection fails

    # Create tasks for bot and web server
    # These tasks run within the event loop created by asyncio.run(main())
    bot_task = asyncio.create_task(run_bot(), name="DiscordBotTask")
    web_server_task = asyncio.create_task(run_web_server(), name="WebServerTask")

    # Wait for either task to complete (e.g., bot logs out, web server crashes)
    done, pending = await asyncio.wait(
        {bot_task, web_server_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    print("One of the main tasks completed or failed.")

    # Initiate shutdown for the other task(s)
    for task in pending:
        print(f"Cancelling pending task: {task.get_name()}")
        task.cancel()

    # Wait for pending tasks to be cancelled
    if pending:
         await asyncio.wait(pending, return_when=asyncio.ALL_COMPLETED)

    # Final cleanup (ensure DB pool is closed)
    if db_pool:
        print("Closing database pool...")
        await db_pool.close()
        print("Database pool closed.")

    print("Main execution finished.")


if __name__ == "__main__":
    print("Starting application...")
    try:
        # asyncio.run() creates and manages the event loop
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown requested by user (KeyboardInterrupt).")
    except Exception as e:
        # Catch any unexpected errors during the setup or main loop management
        print(f"CRITICAL error during main execution scope: {e}")
    finally:
         print("Application exiting.")