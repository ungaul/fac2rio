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
                    if current_map is not None:
                        ssh = paramiko.SSHClient()
                        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                        ssh.connect(SERVER_IP, username=EC2_USER, pkey=PRIVATE_KEY)
                        ssh.exec_command("pkill factorio")
                        ssh.exec_command("rm -f /home/ec2-user/factorio/.lock")
                        # Procéder à la sauvegarde finale
                        stdin, stdout, stderr = ssh.exec_command("ls -t /home/ec2-user/factorio/saves | grep autosave")
                        autosave_files = stdout.read().decode().strip().splitlines()
                        if autosave_files:
                            most_recent = autosave_files[0]
                            ssh.exec_command(f"cp /home/ec2-user/factorio/saves/{current_map}.zip /home/ec2-user/factorio/saves/{current_map}_save.zip")
                            ssh.exec_command(f"cp /home/ec2-user/factorio/saves/{most_recent} /home/ec2-user/factorio/saves/{current_map}.zip")
                        ssh.close()
                    ec2_client.stop_instances(InstanceIds=[INSTANCE_ID])
                    tail_stop_event.set()
                except Exception as e:
                    print(f"Error stopping instance: {e}")
                zero_player_start_time = None
        else:
            zero_player_start_time = None
        await asyncio.sleep(10)

@tree.command(name="start", description="Start the Factorio server for a specified map (provide map name without .zip).")
async def start(interaction: discord.Interaction, mapname: str):
    global tail_task, tail_stop_event, player_count, current_map
    print("Command /start received")
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
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(SERVER_IP, username=EC2_USER, pkey=PRIVATE_KEY)
        await message.edit(content="Synchronizing mods for the map...")
        ssh.exec_command(f"cd factorio/bin/x64 && ./factorio --sync-mods ~/factorio/saves/{mapname}.zip")
        await asyncio.sleep(5)
        await message.edit(content="Launching Factorio server...")
        ssh.exec_command("rm -f /home/ec2-user/factorio/.lock")
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

@tree.command(name="stop", description="Stop the Factorio server (only allowed if no players are online).")
async def stop(interaction: discord.Interaction):
    global tail_stop_event, current_map
    print("Command /stop received")
    await interaction.response.defer(ephemeral=True)
    message = await interaction.original_response()
    instance_state = get_instance_state()
    if instance_state == "stopped":
        await message.edit(content="Factorio server is already stopped.")
        return
    if player_count != 0:
        await message.edit(content="Cannot stop server: players are currently connected.")
        return
    await message.edit(content="Stopping Factorio server...")
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(SERVER_IP, username=EC2_USER, pkey=PRIVATE_KEY)
        ssh.exec_command("pkill factorio")
        ssh.exec_command("rm -f /home/ec2-user/factorio/.lock")
        stdin, stdout, stderr = ssh.exec_command("ls -t /home/ec2-user/factorio/saves | grep autosave")
        autosave_files = stdout.read().decode().strip().splitlines()
        if autosave_files:
            most_recent = autosave_files[0]
            ssh.exec_command(f"cp /home/ec2-user/factorio/saves/{current_map}.zip /home/ec2-user/factorio/saves/{current_map}_save.zip")
            ssh.exec_command(f"cp /home/ec2-user/factorio/saves/{most_recent} /home/ec2-user/factorio/saves/{current_map}.zip")
        ssh.close()
        ec2_client.stop_instances(InstanceIds=[INSTANCE_ID])
        tail_stop_event.set()
        await message.edit(content="Factorio server is now off.")
        await asyncio.sleep(2)
        await interaction.channel.purge(limit=100, check=lambda m: m.author == bot.user)
        current_map = None
    except Exception as e:
        await message.edit(content=f"Error: {e}")

@tree.command(name="status", description="Check the Factorio server status and show current player count.")
async def status(interaction: discord.Interaction):
    print("Command /status received")
    await interaction.response.defer(ephemeral=True)
    instance_state = get_instance_state()
    status_str = "Running" if instance_state == "running" else "Stopped"
    await interaction.followup.send(
        f"Factorio server is **{status_str}** with **{player_count}** players online. (Map: **{current_map or 'None'}**)",
        ephemeral=True
    )

@tree.command(
    name="create",
    description="Create a new Factorio map with specified mods. Optional mod list (mod names separated by ;)."
)
async def create(interaction: discord.Interaction, mapname: str, modlist: str = None):
    await interaction.response.defer(ephemeral=True)
    message = await interaction.original_response()
    print("Command /create received")

    instance_state = get_instance_state()
    if instance_state == "stopped":
        await message.edit(content="Starting EC2 instance for map creation...")
        ec2_client.start_instances(InstanceIds=[INSTANCE_ID])
        await asyncio.sleep(60)

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(SERVER_IP, username=EC2_USER, pkey=PRIVATE_KEY)
        check_cmd = f"ls /home/ec2-user/factorio/saves/{mapname}.zip"
        stdin, stdout, stderr = ssh.exec_command(check_cmd)
        result = stdout.read().decode().strip()
        ssh.close()
        if result:
            await message.edit(content=f"Error: A map named **{mapname}** already exists.")
            return
    except Exception as e:
        await message.edit(content=f"Error checking map existence: {e}")
        return

    if modlist:
        user_mods = [m.strip() for m in modlist.split(";") if m.strip()]
    else:
        user_mods = []

    if user_mods:
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(SERVER_IP, username=EC2_USER, pkey=PRIVATE_KEY)
            for m in user_mods:
                check_mod_cmd = f"ls /home/ec2-user/factorio/mods | grep -i '{m}.*\\.zip'"
                stdin, stdout, stderr = ssh.exec_command(check_mod_cmd)
                mod_files = stdout.read().decode().strip()
                if not mod_files:
                    ssh.close()
                    await message.edit(content=f"Impossible to create the map: '{m}' doesn't exist as a mod.")
                    return
            ssh.close()
        except Exception as e:
            await message.edit(content=f"Error checking mods existence: {e}")
            return

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(SERVER_IP, username=EC2_USER, pkey=PRIVATE_KEY)
        sftp = ssh.open_sftp()
        remote_mod_list_path = "/home/ec2-user/factorio/mods/mod-list.json"
        with sftp.open(remote_mod_list_path, 'r') as f:
            data = json.load(f)
        mandatory_mods = {"base", "elevated-rails", "quality", "space-age"}
        for mod in data.get("mods", []):
            if mod["name"] not in mandatory_mods:
                mod["enabled"] = False
        for m in user_mods:
            for mod in data.get("mods", []):
                if mod["name"] == m:
                    mod["enabled"] = True
        with sftp.open(remote_mod_list_path, 'w') as f:
            json.dump(data, f, indent=2)
        sftp.close()
        ssh.close()
    except Exception as e:
        await message.edit(content=f"Error updating remote mod-list.json: {e}")
        return

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(SERVER_IP, username=EC2_USER, pkey=PRIVATE_KEY)
        await message.edit(content="Creating new map and applying mod configuration...")
        ssh.exec_command("rm -f /home/ec2-user/factorio/.lock")
        create_cmd = f"cd factorio/bin/x64 && ./factorio --create ~/factorio/saves/{mapname}.zip --server-settings ~/factorio/server-settings.json"
        ssh.exec_command(create_cmd)
        await asyncio.sleep(10)
        ssh2 = paramiko.SSHClient()
        ssh2.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh2.connect(SERVER_IP, username=EC2_USER, pkey=PRIVATE_KEY)
        stdin, stdout, stderr = ssh2.exec_command(f"ls /home/ec2-user/factorio/saves/{mapname}.zip")
        created_save = stdout.read().decode().strip()
        ssh2.close()
        ssh.close()
        if not created_save:
            await message.edit(content=f"Error: Map file {mapname}.zip was not created.")
            return
        ec2_client.stop_instances(InstanceIds=[INSTANCE_ID])
        if user_mods:
            mods_used = ", ".join(user_mods)
        else:
            mods_used = "all non-mandatory mods disabled"
        await message.edit(content=f"Map **{mapname}** successfully created with mods: {mods_used}!\n\nPlease now run `/start {mapname}` to run the map.")
    except Exception as e:
        await message.edit(content=f"Error creating new map: {e}")


@tree.command(name="help", description="Show available commands.")
async def help_command(interaction: discord.Interaction):
    print("Command /help received")
    commands_list = """
    **Available Commands:**
    - /start [mapname]: Start the Factorio server for the specified map.
    - /stop: Stop the Factorio server (only if no players are online).
    - /status: Display the server status, current player count and active map.
    - /create [mapname] [modlist]: Create a new map with an optional mod list (mod names separated by semicolons).
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
