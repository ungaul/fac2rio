from dotenv import load_dotenv
import discord
from discord import app_commands
import boto3
import paramiko
import asyncio
import os
import json
from io import StringIO

load_dotenv()

# Environment variables
TOKEN = os.getenv('TOKEN')
INSTANCE_ID = os.getenv('INSTANCE_ID')
SERVER_IP = os.getenv('SERVER_IP')
PEM_KEY = os.getenv('PEM_KEY').replace('\\n', '\n')
KEY_FILE = StringIO(PEM_KEY)
PRIVATE_KEY = paramiko.RSAKey.from_private_key(KEY_FILE)
EC2_USER = os.getenv('EC2_USER')
STATUS_CHANNEL_ID = int("1338063790402699346")

# Factorio Variables
FACTORIO_USERNAME = os.getenv('FACTORIO_USERNAME')
FACTORIO_TOKEN = os.getenv('FACTORIO_TOKEN')

# Initialize Discord bot
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# AWS EC2 client
ec2_client = boto3.client('ec2', region_name='ap-east-1')


async def update_server_status():
    await bot.wait_until_ready()
    channel = bot.get_channel(STATUS_CHANNEL_ID)

    while not bot.is_closed():
        instance_state = get_instance_state()
        factorio_running = await is_factorio_running() if instance_state == "running" else False
        if factorio_running:
            is_paused = await check_if_paused()

            if is_paused:
                print("Game is paused, checking if we need to shut down EC2.")
                await asyncio.sleep(300)
                is_paused_again = await check_if_paused()
                if is_paused_again:
                    print("Game is still paused after 5 minutes, stopping EC2 server.")
                    ec2_client.stop_instances(InstanceIds=[INSTANCE_ID])
                    if channel:
                        await channel.edit(name="Server Off")
            else:
                new_name = "Server On"
                if channel:
                    await channel.edit(name=new_name)
                print(f"Channel name updated: {new_name}")
        else:
            new_name = "Server Off"
            if channel:
                await channel.edit(name=new_name)
            print(f"Channel name updated: {new_name}")

        await asyncio.sleep(60)

async def check_if_paused():
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(SERVER_IP, username=EC2_USER, pkey=PRIVATE_KEY)
        stdin, stdout, stderr = ssh.exec_command("cat /home/ec2-user/factorio/factorio-current.log | grep 'Game Paused'")
        output = stdout.read().decode()
        if "Game Paused" in output:
            ssh.close()
            return True

        ssh.close()
        return False
    except Exception as e:
        print(f"Error checking paused state: {e}")
        return False


@bot.event
async def on_ready():
    print(f"Bot connected as {bot.user}")
    await tree.sync()
    print("Slash commands synced with Discord.")
    bot.loop.create_task(update_server_status())


def get_instance_state():
    response = ec2_client.describe_instances(InstanceIds=[INSTANCE_ID])
    state = response['Reservations'][0]['Instances'][0]['State']['Name']
    return state


def upload_factorio_configs():
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(SERVER_IP, username=EC2_USER, pkey=PRIVATE_KEY)

        sftp = ssh.open_sftp()

        # Upload server-settings.json
        server_settings = {
            "name": "FAC2RIO",
            "description": "Yo.",
            "tags": ["modded", "coop", "space"],
            "max_players": 100,
            "visibility": {"public": True},
            "username": os.getenv('FACTORIO_USERNAME'),
            "password": "",
            "token": os.getenv('FACTORIO_TOKEN'),
            "game_password": "",
            "require_user_verification": True,
            "max_upload_in_kilobytes_per_second": 0,
            "minimum_latency_in_ticks": 0,
            "ignore_player_limit_for_returning_players": False,
            "allow_commands": "admins-only",
            "autosave_interval": 3,
            "autosave_slots": 5,
            "afk_autokick_interval": 10,
            "auto_pause": True,
            "only_admins_can_pause_the_game": True
        }

        with sftp.open('/home/ec2-user/factorio/server-settings.json', 'w') as settings_file:
            settings_file.write(json.dumps(server_settings, indent=4))
        print("server-settings.json successfully uploaded.")

        # Upload mod-list.json
        local_mod_list_path = 'mod-list.json'
        remote_mod_list_path = '/home/ec2-user/factorio/mods/mod-list.json'

        if os.path.exists(local_mod_list_path):
            sftp.put(local_mod_list_path, remote_mod_list_path)
            print("mod-list.json successfully uploaded.")
        else:
            print(f"Error: {local_mod_list_path} does not exist.")

        sftp.close()
        ssh.close()

    except Exception as e:
        print(f"Error uploading configuration files: {e}")


async def is_factorio_running():
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(SERVER_IP, username=EC2_USER, pkey=PRIVATE_KEY)

        stdin, stdout, stderr = ssh.exec_command("pgrep -f factorio")
        output = stdout.read().decode().strip()
        ssh.close()

        return bool(output)
    except Exception as e:
        print(f"SSH error: {e}")
        return False


@tree.command(name="start_factorio", description="Start the Factorio server.")
async def start_factorio(interaction: discord.Interaction):
    print("Command /start_factorio received")
    await interaction.response.defer()

    # Single message to update during execution
    status_message = await interaction.followup.send("Starting Factorio server...")

    instance_state = get_instance_state()
    if instance_state != "stopped":
        await status_message.edit(content=f"Factorio server is already running (state: {instance_state}).")
        return

    ec2_client.start_instances(InstanceIds=[INSTANCE_ID])
    await status_message.edit(content="Factorio server started, standby...")

    await asyncio.sleep(60)

    try:
        await status_message.edit(content="Uploading server configuration...")
        upload_factorio_configs()

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(SERVER_IP, username=EC2_USER, pkey=PRIVATE_KEY)

        await status_message.edit(content="Launching Factorio server...")
        ssh.exec_command(
            "cd factorio/bin/x64 && nohup ./factorio --start-server ~/factorio/saves/FAC2RIO.zip "
            "--server-settings ~/factorio/server-settings.json > /home/ec2-user/factorio/factorio.log 2>&1 &"
        )

        await asyncio.sleep(10)
        stdin, stdout, stderr = ssh.exec_command("cat /home/ec2-user/factorio/factorio.log")
        log_output = stdout.read().decode()
        print("Factorio log:")
        print(log_output)
        await status_message.edit(content="Factorio successfully launched!")
    except Exception as e:
        await status_message.edit(content=f"Error launching Factorio: {e}")
    finally:
        ssh.close()


@tree.command(name="stop_factorio", description="Stop the Factorio server.")
async def stop_factorio(interaction: discord.Interaction):
    print("Command /stop_factorio received")

    instance_state = get_instance_state()
    if instance_state == "stopped":
        await interaction.response.send_message("Factorio server is already stopped.", ephemeral=True)
        return

    await interaction.response.send_message("Connecting with SSH to stop Factorio...", ephemeral=True)

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(SERVER_IP, username=EC2_USER, pkey=PRIVATE_KEY)

        ssh.exec_command("pkill factorio")
        ssh.close()

        ec2_client.stop_instances(InstanceIds=[INSTANCE_ID])
        await interaction.followup.send("Factorio and the EC2 instance are now off.")
    except Exception as e:
        await interaction.followup.send(f"Error: {e}")


@tree.command(name="status_factorio", description="Check if the Factorio server is running.")
async def status_factorio(interaction: discord.Interaction):
    print("Command /status_factorio received")

    factorio_running = await is_factorio_running()
    status = "Running" if factorio_running else "Stopped"

    await interaction.response.send_message(f"Factorio server is currently: **{status}**")


@tree.command(name="help", description="Show available commands.")
async def help_command(interaction: discord.Interaction):
    print("Command /help received")
    commands_list = """
**Available Commands:**
- `/start_factorio`: Start the Factorio server.
- `/stop_factorio`: Stop the Factorio server.
- `/status_factorio`: Display the status of the Factorio server.
- `/help`: Display this help message.
"""
    await interaction.response.send_message(commands_list, ephemeral=True)


async def clear_messages(channel, user_command_message, limit=100):
    messages = await channel.history(limit=limit).flatten()
    messages_to_delete = messages + [user_command_message]
    if messages_to_delete:
        await channel.delete_messages(messages_to_delete)


@tree.command(name="clear", description="Clear messages from the channel.")
async def clear_command(interaction: discord.Interaction, limit: int = 100):
    print("Command /clear received")
    await interaction.response.send_message(f"Clearing the last {limit} messages...", ephemeral=True)
    await clear_messages(interaction.channel, interaction.message, limit)
    await interaction.followup.send("Messages cleared successfully!")


# Run the bot
bot.run(TOKEN)
