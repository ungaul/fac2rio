from dotenv import load_dotenv
import discord
from discord import app_commands
import boto3
import paramiko
import asyncio
import os
import json
import re
from io import StringIO

load_dotenv()

TOKEN = os.getenv('TOKEN')
INSTANCE_ID = os.getenv('INSTANCE_ID')
SERVER_IP = os.getenv('SERVER_IP')
PEM_KEY = os.getenv('PEM_KEY').replace('\\n', '\n')
KEY_FILE = StringIO(PEM_KEY)
PRIVATE_KEY = paramiko.RSAKey.from_private_key(KEY_FILE)
EC2_USER = os.getenv('EC2_USER')
STATUS_CHANNEL_ID = int("1338063790402699346")

FACTORIO_USERNAME = os.getenv('FACTORIO_USERNAME')
FACTORIO_TOKEN = os.getenv('FACTORIO_TOKEN')

intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

ec2_client = boto3.client('ec2', region_name='ap-northeast-1')

player_count = 0
tail_task = None
zero_player_start_time = None
tail_stop_event = asyncio.Event()
current_map = None

def upload_factorio_configs():
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(SERVER_IP, username=EC2_USER, pkey=PRIVATE_KEY)

        sftp = ssh.open_sftp()

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
            "only_admins_can_pause_the_game": True,
            "rcon_port": 27015,
            "rcon_password": "HEIL",
            "rcon_interface": "0.0.0.0"
        }

        with sftp.open('/home/ec2-user/factorio/server-settings.json', 'w') as settings_file:
            settings_file.write(json.dumps(server_settings, indent=4))
        print("server-settings.json successfully uploaded.")

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

async def tail_logs():
    global player_count, zero_player_start_time
    try:
        print("Starting log tailing...")
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(SERVER_IP, username=EC2_USER, pkey=PRIVATE_KEY)

        transport = ssh.get_transport()
        channel = transport.open_session()
        channel.exec_command("tail -F /home/ec2-user/factorio/factorio.log")

        while not tail_stop_event.is_set():
            if channel.recv_ready():
                data = channel.recv(1024).decode("utf-8")
                for line in data.splitlines():
                    if "[JOIN]" in line:
                        player_count += 1
                        zero_player_start_time = None
                        print(f"JOIN event: new player detected. Player count: {player_count}")
                    elif "[LEAVE]" in line:
                        player_count = max(0, player_count - 1)
                        print(f"LEAVE event: player left. Player count: {player_count}")
                        if player_count == 0:
                            zero_player_start_time = asyncio.get_event_loop().time()
            else:
                await asyncio.sleep(1)
        channel.close()
        ssh.close()
        print("Stopped log tailing.")
    except Exception as e:
        print(f"Error in tail_logs: {e}")

async def auto_shutdown():
    global zero_player_start_time
    while not bot.is_closed():
        if player_count == 0 and zero_player_start_time is not None:
            elapsed = asyncio.get_event_loop().time() - zero_player_start_time
            if elapsed >= 300:
                print("No players for 5 minutes. Shutting down the server...")
                try:
                    ec2_client.stop_instances(InstanceIds=[INSTANCE_ID])
                    tail_stop_event.set()
                except Exception as e:
                    print(f"Error stopping instance: {e}")
                zero_player_start_time = None
        else:
            zero_player_start_time = None
        await asyncio.sleep(10)

@tree.command(name="start_factorio", description="Start the Factorio server for a specified map (provide map name without .zip).")
async def start_factorio(interaction: discord.Interaction, mapname: str):
    global tail_task, tail_stop_event, player_count, current_map
    print("Command /start_factorio received")
    await interaction.response.defer(ephemeral=True)
    message = await interaction.original_response()

    instance_state = get_instance_state()
    if instance_state != "stopped":
        await message.edit(content=f"Factorio server is already running (state: {instance_state}).")
        return
    if current_map is not None:
        await message.edit(content=f"A server instance is already running with map **{current_map}**. Stop it before starting a new one.")
        return

    await message.edit(content="Starting EC2 instance...")
    ec2_client.start_instances(InstanceIds=[INSTANCE_ID])
    await asyncio.sleep(60)

    tail_stop_event.clear()
    player_count = 0
    current_map = mapname

    if tail_task is None or tail_task.done():
        tail_task = bot.loop.create_task(tail_logs())

    await message.edit(content="Uploading server configuration...")
    upload_factorio_configs()

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(SERVER_IP, username=EC2_USER, pkey=PRIVATE_KEY)

        await message.edit(content="Synchronizing mods for the map...")
        ssh.exec_command(f"cd factorio/bin/x64 && ./factorio --sync-mods ~/factorio/saves/{mapname}.zip")
        await asyncio.sleep(5)

        await message.edit(content="Launching Factorio server...")
        ssh.exec_command(
            f"cd factorio/bin/x64 && nohup ./factorio --start-server ~/factorio/saves/{mapname}.zip "
            "--server-settings ~/factorio/server-settings.json > /home/ec2-user/factorio/factorio.log 2>&1 &"
        )
        await asyncio.sleep(10)
        stdin, stdout, stderr = ssh.exec_command("tail -n 10 /home/ec2-user/factorio/factorio.log")
        log_output = stdout.read().decode()
        print("Factorio log excerpt:")
        print(log_output)
        await message.edit(content="Factorio successfully launched!")
    except Exception as e:
        await message.edit(content=f"Error launching Factorio: {e}")
    finally:
        ssh.close()

@tree.command(name="stop_factorio", description="Stop the Factorio server for the specified map (only allowed if no players are connected).")
async def stop_factorio(interaction: discord.Interaction, mapname: str):
    global tail_stop_event, current_map
    print("Command /stop_factorio received")
    await interaction.response.defer(ephemeral=True)
    message = await interaction.original_response()

    instance_state = get_instance_state()
    if instance_state == "stopped":
        await message.edit(content="Factorio server is already stopped.")
        return
    if player_count != 0:
        await message.edit(content="Cannot stop server: players are currently connected.")
        return
    if current_map != mapname:
        await message.edit(content="The specified map does not match the currently running instance.")
        return

    await message.edit(content="Stopping Factorio server...")
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(SERVER_IP, username=EC2_USER, pkey=PRIVATE_KEY)
        ssh.exec_command("pkill factorio")
        ssh.close()
        ec2_client.stop_instances(InstanceIds=[INSTANCE_ID])
        tail_stop_event.set()
        await message.edit(content="Factorio server is now off.")
        await asyncio.sleep(2)
        await interaction.channel.purge(limit=100, check=lambda m: m.author == bot.user)
        current_map = None
    except Exception as e:
        await message.edit(content=f"Error: {e}")

@tree.command(name="status_factorio", description="Check the Factorio server status and show current player count.")
async def status_factorio(interaction: discord.Interaction):
    print("Command /status_factorio received")
    await interaction.response.defer(ephemeral=True)
    instance_state = get_instance_state()
    status = "Running" if instance_state == "running" else "Stopped"
    await interaction.followup.send(f"Factorio server is **{status}** with **{player_count}** players online. (Map: **{current_map or 'None'}**)", ephemeral=True)

@tree.command(name="help", description="Show available commands.")
async def help_command(interaction: discord.Interaction):
    print("Command /help received")
    commands_list = """
    **Available Commands:**
    - /start_factorio [mapname]: Start the Factorio server for the specified map.
    - /stop_factorio [mapname]: Stop the Factorio server for the specified map (only if no players are online).
    - /status_factorio: Display the server status, current player count and active map.
    - /help: Display this help message.
    """
    await interaction.response.send_message(commands_list, ephemeral=True)

@tree.command(name="clear", description="Clear a specified number of messages from the channel (any author).")
async def clear(interaction: discord.Interaction, number: int):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=number)
    await interaction.followup.send(f"Deleted {len(deleted)} messages.", ephemeral=True)

def get_instance_state():
    response = ec2_client.describe_instances(InstanceIds=[INSTANCE_ID])
    state = response['Reservations'][0]['Instances'][0]['State']['Name']
    return state

@bot.event
async def on_ready():
    print(f"Bot connected as {bot.user}")
    await tree.sync()
    print("Slash commands synced with Discord.")
    bot.loop.create_task(auto_shutdown())

bot.run(TOKEN)
