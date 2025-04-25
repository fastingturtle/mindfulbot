# main.py
import discord
from discord.ext import commands, tasks
import os
import asyncpg # Database driver
from datetime import datetime, time, timezone, date
import random
import asyncio
from dotenv import load_dotenv

# --- Configuration ---
load_dotenv() # Load environment variables from .env file
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL") # Provided by Render PostgreSQL service
COMMAND_PREFIX = "!"
MINDFUL_ROLE_NAME = "MindfulTrader" # IMPORTANT: Create this role in your Discord Server

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

# Global variable for the database connection pool
db_pool = None

# --- Database Setup and Helpers ---

async def init_db():
    """Initializes the database connection pool and creates tables if they don't exist."""
    global db_pool
    if not DATABASE_URL:
        print("Error: DATABASE_URL environment variable not set.")
        return False

    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
        print("Database connection pool created.")

        # Create tables if they don't exist
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
            # Add index for faster date lookups during reset
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
    if not db_pool: return []
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT channel_id FROM mindful_channels WHERE guild_id = $1", guild_id)
        return [row['channel_id'] for row in rows]

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
    if not db_pool: return 'none', None
    today = datetime.now(timezone.utc).date()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT verified_date, pending_affirmation FROM user_verifications WHERE user_id = $1", user_id)
        if row:
            if row['verified_date'] == today:
                return 'verified', None # Verified today
            elif row['pending_affirmation'] is not None:
                 # Check if the pending state is from today or stale
                 # If verified_date is not today AND pending_affirmation exists, it's a stale pending state
                 # We treat stale pending as 'none' and let the normal flow overwrite it.
                 # A more robust system might track pending timestamp, but this is simpler for MVP.
                 if row['verified_date'] != today:
                      print(f"User {user_id} has stale pending state from {row['verified_date']}. Treating as 'none'.")
                      return 'none', None
                 else:
                      # This case implies date IS today but affirmation is still pending - should only happen briefly
                      return 'pending', row['pending_affirmation']
            else:
                # Entry exists but not verified today and no pending affirmation
                return 'none', None
        else:
            return 'none', None # No record found

async def set_pending_verification_db(user_id, affirmation):
    """Sets user status to pending in DB."""
    if not db_pool: return False
    # Use today's date even for pending to simplify logic/reset.
    # The presence of pending_affirmation indicates it's not truly verified.
    today = datetime.now(timezone.utc).date()
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_verifications (user_id, verified_date, pending_affirmation)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id) DO UPDATE SET
                    verified_date = EXCLUDED.verified_date,
                    pending_affirmation = EXCLUDED.pending_affirmation;
            """, user_id, today, affirmation)
        return True
    except Exception as e:
        print(f"Error setting pending verification in DB for {user_id}: {e}")
        return False

async def complete_verification_db(user_id):
    """Marks user as verified for today in DB."""
    if not db_pool: return False
    today = datetime.now(timezone.utc).date()
    try:
        async with db_pool.acquire() as conn:
            # Update date and remove pending affirmation text
            await conn.execute("""
                UPDATE user_verifications
                SET verified_date = $1, pending_affirmation = NULL
                WHERE user_id = $2;
            """, today, user_id)
        return True
    except Exception as e:
        print(f"Error completing verification in DB for {user_id}: {e}")
        return False

async def clear_stale_verifications_db():
    """Clears users not verified today during daily reset."""
    if not db_pool: return 0
    today = datetime.now(timezone.utc).date()
    try:
        async with db_pool.acquire() as conn:
            # Delete rows where the verification date is not today
            result = await conn.execute("DELETE FROM user_verifications WHERE verified_date != $1", today)
            # result string is like 'DELETE N', parse N
            deleted_count = int(result.split()[-1]) if result else 0
            return deleted_count
    except Exception as e:
        print(f"Error clearing stale verifications from DB: {e}")
        return 0

# --- Helper Functions (Discord Specific) ---

def get_mindful_role(guild):
    """Gets the mindful role object from the guild."""
    if not guild: return None
    return discord.utils.get(guild.roles, name=MINDFUL_ROLE_NAME)

# (apply_read_lock and remove_read_lock remain largely the same as v1,
#  just ensure they handle potential errors gracefully)
async def apply_read_lock(user, channel):
    """Applies the read message history lock for a user on a channel."""
    if not isinstance(user, discord.Member):
         print(f"Warning: apply_read_lock called with non-Member object: {user}")
         return
    if not isinstance(channel, discord.TextChannel):
         print(f"Warning: apply_read_lock called with non-TextChannel object: {channel}")
         return

    try:
        await channel.set_permissions(user, read_message_history=False, overwrite_reason="Mindful check pending")
        print(f"Applied read lock for {user.name} in {channel.name}")
    except discord.Forbidden:
        print(f"Error: Bot lacks permission to set permissions in {channel.name} (Guild: {channel.guild.name})")
    except discord.NotFound:
        print(f"Error: User {user.name} or Channel {channel.name} not found during apply_read_lock.")
    except discord.HTTPException as e:
        print(f"Error applying permissions for {user.name} in {channel.name}: {e}")

async def remove_read_lock(user, channel):
    """Removes the read message history lock for a user on a channel."""
    if not isinstance(user, discord.Member):
         print(f"Warning: remove_read_lock called with non-Member object: {user}")
         return
    if not isinstance(channel, discord.TextChannel):
         print(f"Warning: remove_read_lock called with non-TextChannel object: {channel}")
         return

    try:
        overwrite = channel.overwrites_for(user)
        if overwrite.read_message_history is False:
            await channel.set_permissions(user, read_message_history=None, overwrite_reason="Mindful check complete")
            print(f"Removed read lock for {user.name} in {channel.name}")
    except discord.Forbidden:
        print(f"Error: Bot lacks permission to reset permissions in {channel.name} for {user.name} (or user left)")
    except discord.NotFound:
         print(f"Error: User {user.name} or Channel {channel.name} not found during remove_read_lock.")
    except discord.HTTPException as e:
        print(f"Error removing permissions for {user.name} in {channel.name}: {e}")


# --- Event Handlers ---

@bot.event
async def on_ready():
    """Called when the bot successfully connects."""
    print(f'Logged in as {bot.user.name} ({bot.user.id})')
    print(f'Mindful Role Name: {MINDFUL_ROLE_NAME}')
    # Initialize Database
    if await init_db():
        print('Database initialized successfully.')
        # Start the daily reset task only if DB connection is successful
        if not daily_reset_task.is_running():
            daily_reset_task.start()
    else:
        print("CRITICAL: Database initialization failed. Bot may not function correctly.")
    print('------')


@bot.event
async def on_typing(channel, user, when):
    """
    Triggered when a user starts typing in a channel.
    Checks if verification is needed for mindful channels.
    """
    if not isinstance(channel, discord.TextChannel) or not channel.guild or user.bot or not isinstance(user, discord.Member):
        return # Ignore DMs, bots, non-members

    # Check if DB is available
    if not db_pool:
         # print("DB Pool not available, skipping on_typing check.") # Avoid spamming logs
         return

    guild_id = channel.guild.id
    user_id = user.id

    # Fetch mindful channels for this guild *from DB*
    channels_list = await get_mindful_channels_db(guild_id)
    if not channels_list: # No mindful channels configured for this guild
         return

    if channel.id in channels_list:
        mindful_role = get_mindful_role(channel.guild)
        if mindful_role and mindful_role in user.roles:
            # Check user status *from DB*
            verification_status, pending_affirmation = await get_user_verification_status_db(user_id)

            if verification_status == 'none':
                print(f"User {user.name} started typing in mindful channel {channel.name}, needs verification.")

                # 1. Apply Lock
                await apply_read_lock(user, channel)

                # 2. Send Ephemeral Message
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
                        print(f"Sent verification DM to {user.name} with affirmation: '{affirmation_text}'")
                    else:
                         print(f"Error: Failed to set pending state in DB for {user.name}. Aborting DM.")
                         # Optional: Maybe try removing the lock if DB failed? Or leave locked.

                except discord.Forbidden:
                    print(f"Error: Could not send DM to {user.name}. They might have DMs disabled.")
                    # Consider how to handle this - maybe log and leave locked.
                    # If DM fails, should we clear the pending state from DB? Yes, probably.
                    await conn.execute("DELETE FROM user_verifications WHERE user_id = $1", user_id)

                except Exception as e:
                    print(f"An unexpected error occurred sending DM or setting pending state for {user.name}: {e}")
                    # Clean up DB state if something went wrong
                    await conn.execute("DELETE FROM user_verifications WHERE user_id = $1", user_id)


            elif verification_status == 'pending':
                 # Already pending, maybe re-apply lock just in case it was manually removed
                 print(f"User {user.name} typing in {channel.name}, verification already pending. Re-applying lock.")
                 await apply_read_lock(user, channel)
                 # Maybe resend ephemeral message? Optional.
                 # Do NOT resend DM.

            # else: status is 'verified', do nothing.


@bot.event
async def on_message(message):
    """Handles incoming messages for commands and DM responses."""
    if message.author == bot.user:
        return

    # Check if DB is available before processing anything
    if not db_pool and not isinstance(message.channel, discord.DMChannel): # Allow commands even if DB fails? Maybe not.
         # Let's prevent command processing if DB is down too.
         if message.content.startswith(COMMAND_PREFIX):
              print("DB Pool not available, ignoring command.")
              try:
                   await message.channel.send("Sorry, I'm having trouble connecting to my database right now. Please try again later.", delete_after=10)
              except: pass # Ignore if can't send message
         return
    elif not db_pool and isinstance(message.channel, discord.DMChannel):
         print("DB Pool not available, ignoring DM.")
         # Maybe send a DM back?
         # await message.channel.send("Sorry, I'm having trouble connecting to my database right now.")
         return


    # Process commands first if message is in a guild
    if message.guild:
        await bot.process_commands(message)

    # Check if the message is a DM and not a command invocation
    if isinstance(message.channel, discord.DMChannel) and not message.content.startswith(COMMAND_PREFIX):
        user_id = message.author.id
        verification_status, pending_affirmation = await get_user_verification_status_db(user_id)

        if verification_status == 'pending' and pending_affirmation:
            # Compare submitted affirmation with stored pending one
            if message.content.strip().lower() == pending_affirmation.lower():
                print(f"User {message.author.name} successfully verified via DM.")

                # 1. Mark as verified in DB
                if await complete_verification_db(user_id):
                    # 2. Remove locks from all mindful channels in relevant guilds
                    for guild in message.author.mutual_guilds:
                        guild_id_loop = guild.id
                        member = guild.get_member(user_id) # Get member object
                        if member: # Check if user is still in the guild
                            channels_list = await get_mindful_channels_db(guild_id_loop)
                            for channel_id in channels_list:
                                channel = guild.get_channel(channel_id)
                                if channel and isinstance(channel, discord.TextChannel):
                                    bot_member = guild.me
                                    perms = channel.permissions_for(bot_member)
                                    if perms.manage_roles:
                                         await remove_read_lock(member, channel)
                                    else:
                                         print(f"Warning: Bot lacks Manage Roles in {channel.name} ({guild.name}), cannot remove lock.")
                                elif not channel:
                                     print(f"Warning: Mindful channel ID {channel_id} not found in guild {guild.name} during lock removal.")

                    # 3. Send confirmation DM
                    try:
                        await message.channel.send("✅ Affirmation complete! Access granted to mindful channels for today. Remember to trade responsibly!")
                    except discord.Forbidden: pass
                else:
                     print(f"Error: Failed to complete verification in DB for {user_id}.")
                     try:
                          await message.channel.send("⚠️ An error occurred while saving your verification. Please try typing in the channel again later.")
                     except discord.Forbidden: pass

            # Handle incorrect affirmation
            else:
                 try:
                    await asyncio.sleep(1) # Prevent spamming
                    await message.channel.send(
                        f"❌ That wasn't quite right. Please reply with the exact affirmation:\n\n"
                        f"**{pending_affirmation}**"
                    )
                 except discord.Forbidden: pass
        # else: User sent DM but wasn't pending verification, ignore.

# --- Admin Commands (Using DB) ---

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

        # Cleanup permissions
        mindful_role = get_mindful_role(ctx.guild)
        if mindful_role:
            bot_member = ctx.guild.me
            perms = channel.permissions_for(bot_member)
            if perms.manage_roles:
                 print(f"Cleaning up permissions for {channel.mention}...")
                 # Fetch members with the role asynchronously
                 members_with_role = [m async for m in ctx.guild.fetch_members(limit=None) if mindful_role in m.roles]
                 for member in members_with_role:
                      await remove_read_lock(member, channel)
            else:
                 print(f"Warning: Bot lacks Manage Roles permission in {channel.name}, cannot clean up locks on removal.")
    else:
        await ctx.send(f"⚠️ Failed to remove {channel.mention} from the database.")


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
            channel = ctx.guild.get_channel(channel_id)
            if channel:
                channel_mentions.append(channel.mention)
                valid_channel_ids.append(channel_id)
            else:
                channel_mentions.append(f"*(ID: {channel_id} - Not Found / Deleted?)*")
                needs_cleanup = True # Mark that a deleted channel was found

        message = "Channels currently requiring the daily mindful check:\n- " + "\n- ".join(channel_mentions)
        await ctx.send(message)

        # Perform cleanup if needed
        if needs_cleanup:
             print(f"Found deleted channels in mindful list for guild {ctx.guild.name}. Cleaning up DB...")
             # This is less efficient than it could be, but simple for MVP:
             # Remove all, then re-add the valid ones.
             async with db_pool.acquire() as conn:
                  await conn.execute("DELETE FROM mindful_channels WHERE guild_id = $1", ctx.guild.id)
                  if valid_channel_ids:
                       # Prepare data for executemany
                       data_to_insert = [(ctx.guild.id, ch_id) for ch_id in valid_channel_ids]
                       await conn.executemany("INSERT INTO mindful_channels (guild_id, channel_id) VALUES ($1, $2)", data_to_insert)
             await ctx.send("*(Note: Some channels were not found and have been removed from the list.)*")

    else:
        await ctx.send("ℹ️ No channels are currently configured for the mindful check in this server.")

# (Error handler mindful_command_error remains largely the same, maybe add DB error check)
@add_mindful_channel.error
@remove_mindful_channel.error
@list_mindful_channels.error
async def mindful_command_error(ctx, error):
    if isinstance(error, commands.CommandInvokeError) and isinstance(error.original, asyncpg.PostgresError):
         await ctx.send("❌ A database error occurred. Please try again later or contact the administrator.")
         print(f"Database Error in command {ctx.command.name}: {error.original}")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You do not have permission to use this command (Administrator required).")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument. Please check the command usage: `{COMMAND_PREFIX}help {ctx.command.name}`")
    elif isinstance(error, commands.ChannelNotFound):
         await ctx.send(f"❌ Channel not found: {error.argument}")
    elif isinstance(error, commands.NoPrivateMessage):
         await ctx.send("❌ This command cannot be used in Direct Messages.")
    elif isinstance(error, commands.CommandInvokeError) and isinstance(error.original, discord.Forbidden):
         await ctx.send("❌ Bot lacks the necessary permissions (likely `Manage Roles`/`Manage Permissions`) to perform this action.")
         print(f"Permission Error in command {ctx.command.name}: {error.original}")
    else:
        await ctx.send("An unexpected error occurred while executing the command.")
        print(f"Error in command {ctx.command.name}: {error}")


# --- Daily Reset Task (Using DB) ---
RESET_TIME = time(hour=0, minute=0, second=0, tzinfo=timezone.utc) # Midnight UTC

@tasks.loop(time=RESET_TIME)
async def daily_reset_task():
    """Resets the verification status for users not verified today."""
    await bot.wait_until_ready() # Ensure bot is ready
    if not db_pool:
        print("Daily Reset: Database pool not available. Skipping.")
        return

    print(f"--- Running Daily Reset Task at {datetime.now(timezone.utc)} ---")
    cleared_count = await clear_stale_verifications_db()
    if cleared_count > 0:
        print(f"Cleared verification status for {cleared_count} users from DB.")
    else:
        print("No user verification statuses needed clearing in DB.")

# --- Main Execution ---
async def main():
    """Main function to initialize DB and start the bot."""
    if BOT_TOKEN is None:
        print("Error: DISCORD_BOT_TOKEN not found in environment variables.")
        return
    if DATABASE_URL is None:
         print("Error: DATABASE_URL not found in environment variables.")
         print("Ensure it's set in your .env file locally or Render environment settings.")
         return

    # Initialize DB Pool before starting bot logic
    if not await init_db():
         print("Exiting due to database initialization failure.")
         return # Don't start the bot if DB connection fails

    async with bot:
        print("Attempting to start bot...")
        try:
            await bot.start(BOT_TOKEN)
        except discord.LoginFailure:
            print("Error: Invalid Discord Token. Please check your environment variables.")
        except Exception as e:
             print(f"An error occurred while starting or running the bot: {e}")
             # Perform cleanup if necessary
             if db_pool:
                  await db_pool.close()
                  print("Database pool closed.")
             await bot.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot shutdown requested.")
    # Add cleanup for DB pool on KeyboardInterrupt if main() doesn't complete fully
    finally:
         # This might run before main()'s cleanup if interrupt happens early.
         # Consider a more robust signal handling mechanism for production.
         # if db_pool:
         #      asyncio.run(db_pool.close()) # Need to run in an event loop
         #      print("Database pool closed on exit.")
         pass # Keep it simple for now