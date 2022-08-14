import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from random import randrange
from asyncio_mqtt import Client, MqttError
import json
import requests
from slugify import slugify
import socket
import os

BASE_TOPIC = os.environ.get('MQTT_BASE_TOPIC') or 'vrroom'
MQTT_HOST = os.environ.get('MQTT_HOST')
VRROOM_IP = os.environ.get('VRROOM_IP')

if not MQTT_HOST or not VRROOM_IP:
    raise Exception('Missing environment config! Please make sure MQTT_HOST and VRROOM_IP environment variables are set.')

VRROOM_TCP_PORT = 2222
VRROOM_UNIQUE_IDENTIFIER = f'VRROOM_AT_{VRROOM_IP}'

PORT_OPTIONS = [
    '0', '1', '2', '3', '4'
]

FIELD_TO_DESCRIPTION = {
    "portseltx0": "TX0 Port Selection",
    "portseltx1": "TX1 Port Selection",
    # "opmode": "Operation Mode",

    "RX0": "RX0 Input Signal",
    "RX1": "RX1 Input Signal",
    "TX0": "TX0 Output Signal",
    "TX1": "TX1 Output Signal",
    "AUD0": "TX0 Audio Stream",
    "AUD1": "TX1 Audio Stream",
    "AUDOUT": "Audio Output",
    "EARCRX": "eARC RX",
    "SINK0": "TX0 Video Capabilities",
    "EDIDA0": "TX0 Audio Capabilities",
    "SINK1": "TX1 Video Capabilities",
    "EDIDA1": "TX0 Audio Capabilities",
    "EDIDA2": "Audio Output's Capabilities",
    "SPDASC": "Source Name",
}

async def hdfury_vrroom_mqtt_bridge():
    async with AsyncExitStack() as stack:
        tasks = set()
        stack.push_async_callback(cancel_tasks, tasks)

        client = Client(MQTT_HOST)
        await stack.enter_async_context(client)

        await publish_homeassistant_discovery_config(client)

        manager = client.filtered_messages(f"{BASE_TOPIC}/command/#")
        messages = await stack.enter_async_context(manager)
        task = asyncio.create_task(process_commands(messages, client))
        tasks.add(task)

        await client.subscribe(f"{BASE_TOPIC}/command/#")

        task =  asyncio.create_task(update_vrroom_status(client))

        tasks.add(task)

        await asyncio.gather(*tasks)

async def update_vrroom_status(client):
    while True:
        try:
            status = poll_hdfury()
            print(status)
            for key, value in status.items():
                if key in FIELD_TO_DESCRIPTION:
                    await client.publish(f"{BASE_TOPIC}/state/{slugify(FIELD_TO_DESCRIPTION[key])}", value)

        except Exception as inst:
            print(f"---- Exception thrown: {inst}")

        await asyncio.sleep(10)

def poll_hdfury():
    url = f"http://{VRROOM_IP}/ssi/infopage.ssi"
    r = requests.get(url)
    return r.json()

async def send_hdfury_command(command, value, client):
    
    if command == 'tx0-port-selection':
        hdfury_command = 'inseltx0'
    elif command == 'tx1-port-selection':
        hdfury_command = 'inseltx1'
    else:
        print("Unsupported command!")
        return
    message = f'set {hdfury_command} {value}\n'
    print(f'Sending message {message}')

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((VRROOM_IP, VRROOM_TCP_PORT))
    s.sendall(message.encode('ascii'))
    data = s.recv(1024)
    s.close()
    
    print('Received', repr(data))

    decoded_string = data.decode('utf-8').strip()
    if hdfury_command in decoded_string:
        new_value = decoded_string[len(hdfury_command)+1:]
        print(f"Got new value of {new_value}")
        await client.publish(f"{BASE_TOPIC}/state/{command}", new_value)

async def process_commands(messages, client):
    async for message in messages:
        # 🤔 Note that we assume that the message paylod is an
        # UTF8-encoded string (hence the `bytes.decode` call).
        command = message.topic[len(f"{BASE_TOPIC}/command/"):]
        value = message.payload.decode()
        
        print("")
        print(f'-------------- Executing command {command} with {value}')
        try:
            await send_hdfury_command(command, value, client)
        except Exception as inst:
            print(f"---- Exception thrown: {inst}")
        print("")

async def publish_homeassistant_discovery_config(client):
    for key_name, description in FIELD_TO_DESCRIPTION.items():
        if key_name == 'portseltx0' or key_name == 'portseltx1':
            continue
        
        slugged = slugify(description)

        await client.publish(f"homeassistant/sensor/{BASE_TOPIC}/{slugged}/config", 
            json.dumps({
                "name": f"VRROOM {description}", 
                "unique_id": f"{VRROOM_UNIQUE_IDENTIFIER}_{slugged}",
                "state_topic": f"{BASE_TOPIC}/state/{slugged}",
            }),
            retain = True
        )
    
    for i in range(0,2):
        await client.publish(f"homeassistant/select/{BASE_TOPIC}/tx{i}-port-selection/config", 
            json.dumps({
                "name": f"VRROOM TX{i} Input Selection", 
                "unique_id": f"{VRROOM_UNIQUE_IDENTIFIER}_tx{i}-port-selection",
                "command_topic": f"{BASE_TOPIC}/command/tx{i}-port-selection", 
                "state_topic": f"{BASE_TOPIC}/state/tx{i}-port-selection",
                "options": PORT_OPTIONS,
            }),
            retain = True
        )
        


async def cancel_tasks(tasks):
    for task in tasks:
        if task.done():
            continue
        try:
            task.cancel()
            await task
        except asyncio.CancelledError:
            pass

async def main():
    # Run the hdfury_vrroom_mqtt_bridge indefinitely. Reconnect automatically
    # if the connection is lost.
    reconnect_interval = 3  # [seconds]
    while True:
        try:
            await hdfury_vrroom_mqtt_bridge()
        except MqttError as error:
            print(f'Error "{error}". Reconnecting in {reconnect_interval} seconds.')
        finally:
            await asyncio.sleep(reconnect_interval)


asyncio.run(main())