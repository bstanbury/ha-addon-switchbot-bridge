#!/usr/bin/env python3
"""SwitchBot Direct API Bridge v1.0.0 — HA Add-on
Real-time device states, commands, and automation triggers.

Endpoints:
  GET  /health                    — Health check
  GET  /devices                   — List all devices with types
  GET  /status/<id_or_alias>      — Device status (cached or live)
  GET  /status/motion             — All motion sensors
  GET  /status/lock               — Lock + door state
  GET  /status/blinds             — All blinds/covers
  GET  /status/fans               — All fans
  GET  /status/climate            — All temp/humidity sensors
  GET  /status/all                — All cached statuses
  POST /command/<id>/<cmd>        — Send command
  POST /lock/lock                 — Lock front door
  POST /lock/unlock               — Unlock front door
  POST /blind/<id>/<position>     — Set blind position (0-100)
  POST /fan/<id>/<speed>          — Set fan speed (1-100 or off)
  GET  /events                    — Recent state change events
  GET  /door/history              — Door open/close history
"""
import os, json, time, logging, threading, hashlib, hmac, base64, uuid
from datetime import datetime
from flask import Flask, jsonify, request
import requests as http

TOKEN = os.environ.get('SB_TOKEN', '')
SECRET = os.environ.get('SB_SECRET', '')
API_PORT = int(os.environ.get('API_PORT', '8098'))
POLL_INTERVAL = int(os.environ.get('POLL_INTERVAL', '30'))
HA_URL = os.environ.get('HA_URL', 'http://localhost:8123')
HA_TOKEN = os.environ.get('HA_TOKEN', '')

SB_API = 'https://api.switch-bot.com/v1.1'

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('switchbot-bridge')

device_list = []
device_cache = {}
cache_lock = threading.Lock()
NAMES = {}
events_log = []
door_history = []
last_door_state = None

# Device aliases for friendly access
ALIASES = {}

# Device categories
MOTION_SENSORS = {}
LOCK_DEVICES = {}
BLIND_DEVICES = {}
FAN_DEVICES = {}
CLIMATE_SENSORS = {}

def sb_headers():
    t = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())
    sign = base64.b64encode(hmac.new(SECRET.encode(), f'{TOKEN}{t}{nonce}'.encode(), hashlib.sha256).digest()).decode()
    return {'Authorization': TOKEN, 't': t, 'sign': sign, 'nonce': nonce, 'Content-Type': 'application/json'}

def sb_get(path):
    try:
        r = http.get(f'{SB_API}{path}', headers=sb_headers(), timeout=10)
        d = r.json()
        if d.get('statusCode') == 100: return d.get('body', {})
        logger.error(f'SB error: {d.get("message",d)}')
    except Exception as e:
        logger.error(f'SB request failed: {e}')
    return None

def sb_post(path, body):
    try:
        r = http.post(f'{SB_API}{path}', headers=sb_headers(), json=body, timeout=10)
        return r.json()
    except Exception as e:
        return {'statusCode': -1, 'message': str(e)}

def fetch_devices():
    global device_list, NAMES, ALIASES, MOTION_SENSORS, LOCK_DEVICES, BLIND_DEVICES, FAN_DEVICES, CLIMATE_SENSORS
    data = sb_get('/devices')
    if not data: return []
    device_list = data.get('deviceList', [])
    for d in device_list:
        did = d['deviceId']
        name = d.get('deviceName', did)
        dtype = d.get('deviceType', '')
        NAMES[did] = name
        alias = name.lower().replace(' ', '_').replace('(', '').replace(')', '')
        ALIASES[alias] = did
        ALIASES[did] = did
        # Categorize
        if 'Motion' in dtype: MOTION_SENSORS[did] = name
        elif 'Lock' in dtype: LOCK_DEVICES[did] = name
        elif 'Roller' in dtype or 'Blind' in dtype or 'Curtain' in dtype: BLIND_DEVICES[did] = name
        elif 'Fan' in dtype or 'Circulator' in dtype: FAN_DEVICES[did] = name
        elif 'Meter' in dtype or 'Sensor' in dtype or 'WoIO' in dtype: CLIMATE_SENSORS[did] = name
    logger.info(f'Loaded {len(device_list)} devices: {len(MOTION_SENSORS)} motion, {len(LOCK_DEVICES)} lock, {len(BLIND_DEVICES)} blind, {len(FAN_DEVICES)} fan, {len(CLIMATE_SENSORS)} climate')
    return device_list

def fetch_status(did):
    data = sb_get(f'/devices/{did}/status')
    if data:
        with cache_lock:
            old = device_cache.get(did, {}).get('status', {})
            device_cache[did] = {'status': data, 'timestamp': time.time(), 'name': NAMES.get(did, did)}
            # Event detection
            if old and data != old:
                event = {'device': NAMES.get(did, did), 'id': did, 'time': datetime.now().isoformat(), 'changes': {}}
                for k in data:
                    if k in old and data[k] != old[k]:
                        event['changes'][k] = {'from': old[k], 'to': data[k]}
                if event['changes']:
                    events_log.append(event)
                    if len(events_log) > 100: events_log.pop(0)
                    logger.info(f'Event: {NAMES.get(did,did)} {event["changes"]}')
    return data

def track_door():
    global last_door_state
    for did in LOCK_DEVICES:
        with cache_lock:
            c = device_cache.get(did, {}).get('status', {})
        door = c.get('doorState', 'unknown')
        if last_door_state is not None and door != last_door_state:
            door_history.append({'time': datetime.now().isoformat(), 'from': last_door_state, 'to': door})
            if len(door_history) > 100: door_history.pop(0)
        last_door_state = door

def poll_loop():
    """Background: poll critical devices."""
    critical = list(MOTION_SENSORS.keys()) + list(LOCK_DEVICES.keys())
    while True:
        for did in critical:
            fetch_status(did)
        track_door()
        time.sleep(POLL_INTERVAL)

def resolve_id(id_or_alias):
    return ALIASES.get(id_or_alias.lower().replace(' ', '_'), id_or_alias)

# ============================================================
# Endpoints
# ============================================================

@app.route('/')
def index():
    return jsonify({'name': 'SwitchBot Direct API Bridge', 'version': '1.0.0', 'devices': len(device_list), 'cached': len(device_cache), 'events': len(events_log)})

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'devices': len(device_list), 'cached': len(device_cache), 'poll_interval': POLL_INTERVAL})

@app.route('/devices')
def devices():
    if not device_list: fetch_devices()
    return jsonify([{'id': d['deviceId'], 'name': d.get('deviceName',''), 'type': d.get('deviceType',''), 'hub': d.get('hubDeviceId','')} for d in device_list])

@app.route('/status/<id_or_alias>')
def status(id_or_alias):
    did = resolve_id(id_or_alias)
    with cache_lock:
        c = device_cache.get(did)
    if c and (time.time() - c['timestamp']) < POLL_INTERVAL:
        return jsonify({**c, 'source': 'cache', 'age_seconds': round(time.time() - c['timestamp'])})
    data = fetch_status(did)
    if data: return jsonify({'status': data, 'name': NAMES.get(did, did), 'timestamp': time.time(), 'source': 'live'})
    return jsonify({'error': 'Failed'}), 500

@app.route('/status/motion')
def motion():
    results = {}
    for did, name in MOTION_SENSORS.items():
        with cache_lock:
            c = device_cache.get(did, {})
        s = c.get('status', {})
        results[name] = {'detected': s.get('moveDetected', False), 'battery': s.get('battery'), 'brightness': s.get('brightness'), 'age': round(time.time() - c.get('timestamp', 0))}
    return jsonify(results)

@app.route('/status/lock')
def lock_status():
    for did in LOCK_DEVICES:
        with cache_lock:
            c = device_cache.get(did, {})
        s = c.get('status', {})
        return jsonify({'lock': s.get('lockState'), 'door': s.get('doorState'), 'battery': s.get('battery'), 'online': s.get('onlineStatus'), 'age': round(time.time() - c.get('timestamp', 0))})
    return jsonify({'error': 'No lock found'}), 404

@app.route('/status/blinds')
def blinds_status():
    results = {}
    for did, name in BLIND_DEVICES.items():
        data = fetch_status(did)
        if data: results[name] = {'position': data.get('slidePosition', data.get('position', '?')), 'moving': data.get('moving', False), 'battery': data.get('battery')}
    return jsonify(results)

@app.route('/status/fans')
def fans_status():
    results = {}
    for did, name in FAN_DEVICES.items():
        data = fetch_status(did)
        if data: results[name] = {'power': data.get('power', '?'), 'speed': data.get('fanSpeed', data.get('speed', '?')), 'mode': data.get('mode', '?'), 'battery': data.get('battery')}
    return jsonify(results)

@app.route('/status/climate')
def climate_status():
    results = {}
    for did, name in CLIMATE_SENSORS.items():
        data = fetch_status(did)
        if data: results[name] = {'temperature': data.get('temperature'), 'humidity': data.get('humidity'), 'battery': data.get('battery'), 'co2': data.get('CO2')}
    return jsonify(results)

@app.route('/status/all')
def all_status():
    with cache_lock:
        return jsonify({did: {**v, 'age': round(time.time() - v['timestamp'])} for did, v in device_cache.items()})

@app.route('/command/<device_id>/<command>', methods=['POST','GET'])
def command(device_id, command):
    did = resolve_id(device_id)
    param = request.args.get('param', 'default')
    return jsonify(sb_post(f'/devices/{did}/commands', {'command': command, 'parameter': param, 'commandType': 'command'}))

@app.route('/lock/lock', methods=['POST','GET'])
def lock_lock():
    for did in LOCK_DEVICES:
        return jsonify(sb_post(f'/devices/{did}/commands', {'command': 'lock', 'parameter': 'default', 'commandType': 'command'}))
    return jsonify({'error': 'No lock'}), 404

@app.route('/lock/unlock', methods=['POST','GET'])
def lock_unlock():
    for did in LOCK_DEVICES:
        return jsonify(sb_post(f'/devices/{did}/commands', {'command': 'unlock', 'parameter': 'default', 'commandType': 'command'}))
    return jsonify({'error': 'No lock'}), 404

@app.route('/blind/<id_or_alias>/<int:position>', methods=['POST','GET'])
def blind_position(id_or_alias, position):
    did = resolve_id(id_or_alias)
    if position < 0 or position > 100:
        return jsonify({'error': 'Position must be 0-100'}), 400
    return jsonify(sb_post(f'/devices/{did}/commands', {'command': 'setPosition', 'parameter': f'0,ff,{position}', 'commandType': 'command'}))

@app.route('/fan/<id_or_alias>/<action>', methods=['POST','GET'])
def fan_control(id_or_alias, action):
    did = resolve_id(id_or_alias)
    if action.lower() == 'off':
        return jsonify(sb_post(f'/devices/{did}/commands', {'command': 'turnOff', 'parameter': 'default', 'commandType': 'command'}))
    elif action.lower() == 'on':
        return jsonify(sb_post(f'/devices/{did}/commands', {'command': 'turnOn', 'parameter': 'default', 'commandType': 'command'}))
    else:
        try:
            speed = int(action)
            return jsonify(sb_post(f'/devices/{did}/commands', {'command': 'setSpeed', 'parameter': str(speed), 'commandType': 'command'}))
        except:
            return jsonify({'error': 'Use: on, off, or speed 1-100'}), 400

@app.route('/events')
def events():
    limit = request.args.get('limit', 20, type=int)
    return jsonify(events_log[-limit:])

@app.route('/door/history')
def door_hist():
    return jsonify(door_history[-20:])

if __name__ == '__main__':
    logger.info(f'SwitchBot Direct API Bridge v1.0.0 on port {API_PORT}')
    fetch_devices()
    poller = threading.Thread(target=poll_loop, daemon=True)
    poller.start()
    logger.info(f'Background poller started ({POLL_INTERVAL}s)')
    app.run(host='0.0.0.0', port=API_PORT, debug=False)
