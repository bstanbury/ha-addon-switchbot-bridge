#!/usr/bin/env python3
"""SwitchBot Direct API Bridge — HA Add-on
Real-time device states and commands via SwitchBot API.

Endpoints:
  GET  /health                    — Health check
  GET  /devices                   — List all devices
  GET  /status/<device_id>        — Device status (cached or live)
  GET  /status/motion             — All motion sensors
  GET  /status/lock               — Lock + door state
  GET  /status/all                — All cached statuses
  POST /command/<device_id>/<cmd> — Send command
  POST /lock/lock                 — Lock front door
  POST /lock/unlock               — Unlock front door
"""

import os, json, time, logging, threading, hashlib, hmac, base64, uuid
from flask import Flask, jsonify, request
import requests as http_requests

TOKEN = os.environ.get('SB_TOKEN', '')
SECRET = os.environ.get('SB_SECRET', '')
API_PORT = int(os.environ.get('API_PORT', '8098'))
POLL_INTERVAL = int(os.environ.get('POLL_INTERVAL', '30'))

SB_API = 'https://api.switch-bot.com/v1.1'

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('switchbot-bridge')

device_list = []
device_status_cache = {}
cache_lock = threading.Lock()
DEVICE_NAMES = {}

# Critical devices to poll frequently
CRITICAL_DEVICES = {
    'B0E9FE783112': 'Motion Sensor (LR)',
    'B0E9FED7AD33': 'Motion Sensor (MB)',
    'B0E9FE99ADAF': 'Front Door Lock',
}

def sb_headers():
    t = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())
    sign = base64.b64encode(
        hmac.new(SECRET.encode(), f"{TOKEN}{t}{nonce}".encode(), hashlib.sha256).digest()
    ).decode()
    return {'Authorization': TOKEN, 't': t, 'sign': sign, 'nonce': nonce, 'Content-Type': 'application/json'}

def sb_get(path):
    try:
        r = http_requests.get(f"{SB_API}{path}", headers=sb_headers(), timeout=10)
        data = r.json()
        if data.get('statusCode') == 100:
            return data.get('body', {})
        logger.error(f"SB error: {data.get('message', data)}")
        return None
    except Exception as e:
        logger.error(f"SB request failed: {e}")
        return None

def sb_post(path, body):
    try:
        r = http_requests.post(f"{SB_API}{path}", headers=sb_headers(), json=body, timeout=10)
        return r.json()
    except Exception as e:
        return {'statusCode': -1, 'message': str(e)}

def fetch_devices():
    global device_list, DEVICE_NAMES
    data = sb_get('/devices')
    if data:
        device_list = data.get('deviceList', [])
        for d in device_list:
            DEVICE_NAMES[d['deviceId']] = d.get('deviceName', d['deviceId'])
        logger.info(f"Loaded {len(device_list)} devices")
    return device_list

def fetch_status(device_id):
    data = sb_get(f'/devices/{device_id}/status')
    if data:
        with cache_lock:
            device_status_cache[device_id] = {
                'status': data,
                'timestamp': time.time(),
                'name': DEVICE_NAMES.get(device_id, device_id),
            }
    return data

def poll_loop():
    while True:
        for did, name in CRITICAL_DEVICES.items():
            try:
                fetch_status(did)
            except Exception as e:
                logger.error(f"Poll {name}: {e}")
        time.sleep(POLL_INTERVAL)

@app.route('/')
def index():
    return jsonify({'name': 'SwitchBot Direct API Bridge', 'version': '0.1.0', 'cached': len(device_status_cache)})

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'cached': len(device_status_cache), 'poll_interval': POLL_INTERVAL})

@app.route('/devices')
def devices():
    if not device_list:
        fetch_devices()
    return jsonify([{'id': d['deviceId'], 'name': d.get('deviceName',''), 'type': d.get('deviceType','')} for d in device_list])

@app.route('/status/<device_id>')
def status(device_id):
    # Resolve aliases
    aliases = {'motion_lr': 'B0E9FE783112', 'motion_mb': 'B0E9FED7AD33', 'lock': 'B0E9FE99ADAF'}
    device_id = aliases.get(device_id, device_id)
    
    with cache_lock:
        cached = device_status_cache.get(device_id)
    if cached and (time.time() - cached['timestamp']) < POLL_INTERVAL:
        return jsonify({**cached, 'source': 'cache'})
    data = fetch_status(device_id)
    if data:
        return jsonify({'status': data, 'timestamp': time.time(), 'name': DEVICE_NAMES.get(device_id, device_id), 'source': 'live'})
    return jsonify({'error': 'Failed'}), 500

@app.route('/status/motion')
def motion():
    results = {}
    for did, name in [('B0E9FE783112', 'Living Room'), ('B0E9FED7AD33', 'Bedroom')]:
        with cache_lock:
            c = device_status_cache.get(did)
        if c:
            s = c['status']
            results[name] = {'detected': s.get('moveDetected', False), 'battery': s.get('battery'), 'brightness': s.get('brightness'), 'age_seconds': round(time.time() - c['timestamp'])}
        else:
            data = fetch_status(did)
            if data:
                results[name] = {'detected': data.get('moveDetected', False), 'battery': data.get('battery'), 'brightness': data.get('brightness'), 'age_seconds': 0}
    return jsonify(results)

@app.route('/status/lock')
def lock_status():
    did = 'B0E9FE99ADAF'
    with cache_lock:
        c = device_status_cache.get(did)
    if c and (time.time() - c['timestamp']) < POLL_INTERVAL:
        s = c['status']
        return jsonify({'lock': s.get('lockState'), 'door': s.get('doorState'), 'battery': s.get('battery'), 'online': s.get('onlineStatus'), 'age_seconds': round(time.time() - c['timestamp']), 'source': 'cache'})
    data = fetch_status(did)
    if data:
        return jsonify({'lock': data.get('lockState'), 'door': data.get('doorState'), 'battery': data.get('battery'), 'online': data.get('onlineStatus'), 'source': 'live'})
    return jsonify({'error': 'Failed'}), 500

@app.route('/status/all')
def all_status():
    with cache_lock:
        return jsonify({did: {**v, 'age_seconds': round(time.time() - v['timestamp'])} for did, v in device_status_cache.items()})

@app.route('/command/<device_id>/<command>', methods=['POST', 'GET'])
def command(device_id, command):
    param = request.args.get('param', 'default')
    result = sb_post(f'/devices/{device_id}/commands', {'command': command, 'parameter': param, 'commandType': 'command'})
    return jsonify(result)

@app.route('/lock/lock', methods=['POST', 'GET'])
def lock_lock():
    result = sb_post('/devices/B0E9FE99ADAF/commands', {'command': 'lock', 'parameter': 'default', 'commandType': 'command'})
    return jsonify(result)

@app.route('/lock/unlock', methods=['POST', 'GET'])
def lock_unlock():
    result = sb_post('/devices/B0E9FE99ADAF/commands', {'command': 'unlock', 'parameter': 'default', 'commandType': 'command'})
    return jsonify(result)

if __name__ == '__main__':
    logger.info(f'SwitchBot Direct API Bridge v0.1.0 on port {API_PORT}')
    fetch_devices()
    poller = threading.Thread(target=poll_loop, daemon=True)
    poller.start()
    logger.info(f'Background poller started ({POLL_INTERVAL}s interval)')
    app.run(host='0.0.0.0', port=API_PORT, debug=False)
