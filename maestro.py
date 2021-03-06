#!/usr/bin/python3
# coding: utf-8
import time
import systemd
import sys
import psutil, os
import json
import logging
import threading
import paho.mqtt.client as mqtt
import websocket

from systemd.journal import JournalHandler
from systemd import daemon
from logging.handlers import RotatingFileHandler
from _config_ import _MCZport
from _config_ import _MCZip
from messages import MaestroMessageType, process_infostring
from _config_ import _MQTT_pass
from _config_ import _MQTT_user
from _config_ import _MQTT_authentication
from _config_ import _MQTT_TOPIC_PUB
from _config_ import _MQTT_TOPIC_SUB
from _config_ import _MQTT_PAYLOAD_TYPE
from _config_ import _WS_RECONNECTS_BEFORE_ALERT
from _config_ import _MQTT_port
from _config_ import _MQTT_ip
from commands import MaestroCommand, get_maestro_command, maestrocommandvalue_to_websocket_string, MaestroCommandValue

try:
    import thread
except ImportError:
    import _thread as thread

try:
    import queue
except ImportError:
    import Queue as queue

class SetQueue(queue.Queue):
    """ De-Duplicate message queue to prevent flipping values (Debounce) """
    def _init(self, maxsize):
        queue.Queue._init(self, maxsize)
        self.all_items = set()

    def _put(self, item):
        found = False
        for val in self.all_items:
            if val.command.name == item.command.name:
                found = True
                val.command.value = item.command.value
        if not found:
            queue.Queue._put(self, item)
            self.all_items.add(item)

    def _get(self):
        item = queue.Queue._get(self)
        self.all_items.remove(item)
        return item

get_stove_info_interval = 15.0
websocket_connected = False
socket_reconnect_count = 0
client = None
old_connection_status = None

# Logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s :: %(name)s :: %(levelname)s :: %(message)s')
if psutil.Process(os.getpid()).ppid() == 1:
    # We are using systemd
    journald_handler=JournalHandler()
    logger.addHandler(journald_handler)
else:
    file_handler = RotatingFileHandler('activity.log', 'a', 1000000, 1)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)
stream_handler.setLevel(logging.DEBUG)
logger.addHandler(stream_handler)

CommandQueue = SetQueue()
MaestroInfoMessageCache = {}

# Start
logger.info('Starting Maestro Daemon')

def on_connect_mqtt(client, userdata, flags, rc):
    logger.info("MQTT: Connected to broker. " + str(rc))

def on_message_mqtt(client, userdata, message):
    try:
        maestrocommand = None
        cmd_value = None
        payload = str(message.payload.decode())
        if _MQTT_PAYLOAD_TYPE == 'TOPIC':
            topic = str(message.topic)
            command = topic[str(topic).rindex('/')+1:]
            logger.info(f"Command topic received: {command}")
            maestrocommand = get_maestro_command(command)
            cmd_value = float(payload)
        else:
            logger.info(f"MQTT: Message received: {payload}")
            res = json.loads(payload)
            maestrocommand = get_maestro_command(res["Command"])
            cmd_value = float(res["Value"])
        if maestrocommand.name == "Unknown":
            logger.info(f"Unknown Maestro JSON Command Received. Ignoring. {payload}")
        elif maestrocommand.name == "Refresh":
            logger.info('Clearing the message cache')
            MaestroInfoMessageCache.clear()
        else:
            CommandQueue.put(MaestroCommandValue(maestrocommand, cmd_value))
    except Exception as e: # work on python 3.x
            logger.error('Exception in on_message: '+ str(e))

def recuperoinfo_enqueue():
    """Get Stove information every x seconds as long as there is a websocket connection"""
    threading.Timer(get_stove_info_interval, recuperoinfo_enqueue).start()
    if websocket_connected:
        CommandQueue.put(MaestroCommandValue(MaestroCommand('GetInfo', 0, 'GetInfo'), 0))

def send_connection_status_message(message):
    global old_connection_status
    if old_connection_status != message:
        if _MQTT_PAYLOAD_TYPE == 'TOPIC':
            json_dictionary = json.loads(str(json.dumps(message)))
            for key in json_dictionary:
                logger.info('MQTT: publish to Topic "' + str(_MQTT_TOPIC_PUB+'/'+key) +
                        '", Message : ' + str(json_dictionary[key]))
                client.publish(_MQTT_TOPIC_PUB+'/'+key, json_dictionary[key], 1)
        else:
            client.publish(_MQTT_TOPIC_PUB, json.dumps(message), 1)
        old_connection_status = message

def process_info_message(message):
    """Process websocket array string that has the stove Info message"""
    res = process_infostring(message)
    maestro_info_message_publish = {}
        
    for item in res:
        if item not in MaestroInfoMessageCache:
            MaestroInfoMessageCache[item] = res[item]
            maestro_info_message_publish[item] = res[item]
        elif MaestroInfoMessageCache[item] != res[item]:
            MaestroInfoMessageCache[item] = res[item]
            maestro_info_message_publish[item] = res[item]

    if len(maestro_info_message_publish) > 0:
        if _MQTT_PAYLOAD_TYPE == 'TOPIC':
            json_dictionary = json.loads(str(json.dumps(maestro_info_message_publish)))
            for key in json_dictionary:
                logger.info('MQTT: publish to Topic "' + str(_MQTT_TOPIC_PUB+'/'+key) +
                        '", Message : ' + str(json_dictionary[key]))
                client.publish(_MQTT_TOPIC_PUB+'/'+key, json_dictionary[key], 1)
        else:
            client.publish(_MQTT_TOPIC_PUB, json.dumps(maestro_info_message_publish), 1)


def on_message(ws, message):
    message_array = message.split("|")
    if message_array[0] == MaestroMessageType.Info.value:
        process_info_message(message)
    else:
        logger.info('Unsupported message type received !')

def on_error(ws, error):
    logger.info(error)

def on_close(ws):
    logger.info('Websocket: Disconnected')
    global websocket_connected
    websocket_connected = False

def on_open(ws):
    logger.info('Websocket: Connected')
    send_connection_status_message({"Status":"connected"})
    global websocket_connected
    websocket_connected = True
    socket_reconnect_count = 0
    def run(*args):
        for i in range(360*4):
            time.sleep(0.25)
            while not CommandQueue.empty():
                cmd = maestrocommandvalue_to_websocket_string(CommandQueue.get())
                logger.info("Websocket: Send " + str(cmd))
                ws.send(cmd)        
        logger.info('Closing Websocket Connection')
        ws.close()
    thread.start_new_thread(run, ())

def start_mqtt():
    global client
    logger.info('Connection in progress to the MQTT broker (IP:' +
                _MQTT_ip + ' PORT:'+str(_MQTT_port)+')')
    client = mqtt.Client()
    if _MQTT_authentication:
        client.username_pw_set(username=_MQTT_user, password=_MQTT_pass)
    client.on_connect = on_connect_mqtt
    client.on_message = on_message_mqtt
    client.connect(_MQTT_ip, _MQTT_port)
    client.loop_start()
    if _MQTT_PAYLOAD_TYPE == 'TOPIC':
        logger.info('MQTT: Subscribed to topic "' + str(_MQTT_TOPIC_SUB) + '/#"')
        client.subscribe(_MQTT_TOPIC_SUB+'/#', qos=1)
    else:
        logger.info('MQTT: Subscribed to topic "' + str(_MQTT_TOPIC_SUB) + '"')
        client.subscribe(_MQTT_TOPIC_SUB, qos=1)

if __name__ == "__main__":
    recuperoinfo_enqueue()
    socket_reconnect_count = 0
    start_mqtt()
    systemd.daemon.notify('READY=1')
    while True:
        logger.info("Websocket: Establishing connection to server (IP:"+_MCZip+" PORT:"+_MCZport+")")
        ws = websocket.WebSocketApp("ws://" + _MCZip + ":" + _MCZport,
                                    on_message=on_message,
                                    on_error=on_error,
                                    on_close=on_close)
        ws.on_open = on_open

        ws.run_forever(ping_interval=5, ping_timeout=2)
        time.sleep(1)
        socket_reconnect_count = socket_reconnect_count + 1
        logger.info("Socket Reconnection Count: " + str(socket_reconnect_count))
        if socket_reconnect_count>_WS_RECONNECTS_BEFORE_ALERT:
            send_connection_status_message({"Status":"disconnected"})
            socket_reconnect_count = 0
