#!/usr/bin/env python3
"""SwitchBot Direct API Bridge v2.0.0 — HA Add-on
Real-time device states with Event Bus integration.

v2.0 additions:
  - Event Bus SSE subscriber: event-driven confirmation
  - Battery trend tracking: predict replacement dates
  - Motion reliability scoring: compare HA vs SwitchBot
  - Lock event cross-referencing
  - Persistent battery/motion data in /data/switchbot_v2.json

Endpoints:
  GET  /health, /devices, /status/<id>, /status/motion, /status/lock
  GET  /status/blinds, /status/fans, /status/climate, /status/all
  POST /command/<id>/<cmd>, /lock/lock, /lock/unlock
  POST /blind/<id>/<pos>, /fan/<id>/<speed>
  GET  /events, /door/history
  GET  /battery/trends — Battery degradation tracking
  GET  /motion/reliability — Motion sensor accuracy scoring
  GET  /event-log — Recent event-driven actions
"""
import os, json, time, logging, threading, hashlib, hmac, base64, uuid
from datetime import datetime
from collections import deque, defaultdict
from flask import Flask, jsonify, request
import requests as http
import sseclient

TOKEN = os.environ.get('SB_TOKEN', '')
SECRET = os.environ.get('SB_SECRET', '')
API_PORT = int(os.environ.get('API_PORT', '8098'))
POLL_INTERVAL = int(os.environ.get('POLL_INTERVAL', '30'))
HA_URL = os.environ.get('HA_URL', 'http://localhost:8123')
HA_TOKEN = os.environ.get('HA_TOKEN', '')
EVENT_BUS_URL = os.environ.get('EVENT_BUS_URL', 'http://localhost:8092')

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

# v2.0: Battery trends and motion reliability
battery_trends = {}  # {device_id: [{date, level}]}
motion_reliability = {}  # {device_id: {ha_events, sb_events, mismatches}}
event_actions = deque(maxlen=100)
DATA_V2 = '/data/switchbot_v2.json'

ALIASES = {}
MOTION_SENSORS = {}
LOCK_DEVICES = {}
BLIND_DEVICES = {}
FAN_DEVICES = {}
CLIMATE_SENSORS = {}

def load_v2_data():
    global battery_trends, motion_reliability
    try:
        if os.path.exists(DATA_V2):
            d = json.load(open(DATA_V2))
            battery_trends = d.get('battery_trends', {})
            motion_reliability = d.get('motion_reliability', {})
            logger.info(f'Loaded v2 data: {len(battery_trends)} battery trends')
    except: pass

def save_v2_data():
    try:
        json.dump({'battery_trends': battery_trends, 'motion_reliability': motion_reliability, 'saved': datetime.now().isoformat()}, open(DATA_V2, 'w'), indent=2)
    except: pass

def track_battery(did, level):
    """v2.0: Record battery level for trend analysis."""
    if level is None or level == 0:
        return
    today = datetime.now().strftime('%Y-%m-%d')
    if did not in battery_trends:
        battery_trends[did] = []
    # Only record once per day
    if not battery_trends[did] or battery_trends[did][-1].get('date') != today:
        battery_trends[did].append({'date': today, 'level': level})
        # Keep last 90 days
        battery_trends[did] = battery_trends[did][-90:]

def predict_replacement(did):
    """v2.0: Predict battery replacement date based on trend."""
    trend = battery_trends.get(did, [])
    if len(trend) < 7:
        return None
    # Calculate daily drain rate
    recent = trend[-14:]  # Last 2 weeks
    if len(recent) < 2:
        return None
    drain = (recent[0]['level'] - recent[-1]['level']) / len(recent)
    if drain <= 0:
        return None  # Battery not draining (or charging)
    current = recent[-1]['level']
    days_remaining = int(current / drain)
    return {'days_remaining': days_remaining, 'daily_drain': round(drain, 2), 'current': current}

def handle_event(ev):
    """v2.0: React to Event Bus events."""
    eid = ev.get('entity_id', '')
    new = ev.get('new_state', '')
    old = ev.get('old_state', '')
    sig = ev.get('significant', False)
    
    action = None
    
    # Lock state change from HA — verify with SwitchBot
    if 'lock' in eid and sig:
        logger.info(f'EVENT: Lock change detected ({old}->{new}) — verifying with SwitchBot')
        # Query SwitchBot lock directly for confirmation
        for did in LOCK_DEVICES:
            sb_status = fetch_status(did)
            if sb_status:
                sb_lock = sb_status.get('lockState', 'unknown')
                expected = 'locked' if new == 'locked' else 'unlocked'
                if sb_lock != expected:
                    logger.warning(f'MISMATCH: HA says {new} but SwitchBot says {sb_lock}')
                    action = f'lock_mismatch_{new}_vs_{sb_lock}'
                else:
                    action = f'lock_confirmed_{new}'
    
    # Motion detected in HA — cross-reference with SwitchBot motion
    elif 'motion' in eid and new == 'on':
        # Track HA motion event
        for did in MOTION_SENSORS:
            if did not in motion_reliability:
                motion_reliability[did] = {'ha_events': 0, 'sb_events': 0, 'mismatches': 0}
            motion_reliability[did]['ha_events'] += 1
        action = 'motion_ha_tracked'
    
    # Door unlock + motion within 60s = someone let in
    elif 'lock' in eid and new == 'unlocked':
        action = 'unlock_detected'
    
    if action:
        event_actions.append({'time': datetime.now().isoformat(), 'event': eid, 'action': action, 'old': old, 'new': new})

def event_bus_subscriber():
    """v2.0: SSE subscriber thread."""
    while True:
        try:
            logger.info(f'Connecting to Event Bus SSE: {EVENT_BUS_URL}/events/stream')
            response = http.get(f'{EVENT_BUS_URL}/events/stream', stream=True, timeout=None)
            client = sseclient.SSEClient(response)
            logger.info('Event Bus SSE connected')
            for event in client.events():
                try:
                    ev = json.loads(event.data)
                    handle_event(ev)
                except json.JSONDecodeError:
                    pass
                except Exception as e:
                    logger.error(f'Event handling error: {e}')
        except Exception as e:
            logger.error(f'Event Bus SSE error: {e}')
        logger.info('Reconnecting to Event Bus in 10s...')
        time.sleep(10)

def battery_tracker_loop():
    """v2.0: Periodically track battery levels."""
    while True:
        for did in list(LOCK_DEVICES.keys()) + list(MOTION_SENSORS.keys()) + list(CLIMATE_SENSORS.keys()):
            with cache_lock:
                c = device_cache.get(did, {})
            level = c.get('status', {}).get('battery')
            if level is not None:
                track_battery(did, level)
        save_v2_data()
        time.sleep(3600)  # Every hour

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
        if 'Motion' in dtype: MOTION_SENSORS[did] = name
        elif 'Lock' in dtype: LOCK_DEVICES[did] = name
        elif 'Roller' in dtype or 'Blind' in dtype or 'Curtain' in dtype: BLIND_DEVICES[did] = name
        elif 'Fan' in dtype or 'Circulator' in dtype: FAN_DEVICES[did] = name
        elif 'Meter' in dtype or 'Sensor' in dtype or 'WoIO' in dtype: CLIMATE_SENSORS[did] = name
    logger.info(f'Loaded {len(device_list)} devices')
    return device_list

def fetch_status(did):
    data = sb_get(f'/devices/{did}/status')
    if data:
        with cache_lock:
            old = device_cache.get(did, {}).get('status', {})
            device_cache[did] = {'status': data, 'timestamp': time.time(), 'name': NAMES.get(did, did)}
            if old and data != old:
                event = {'device': NAMES.get(did, did), 'id': did, 'time': datetime.now().isoformat(), 'changes': {}}
                for k in data:
                    if k in old and data[k] != old[k]:
                        event['changes'][k] = {'from': old[k], 'to': data[k]}
                if event['changes']:
                    events_log.append(event)
                    if len(events_log) > 100: events_log.pop(0)
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
    critical = list(MOTION_SENSORS.keys()) + list(LOCK_DEVICES.keys())
    while True:
        for did in critical:
            fetch_status(did)
        track_door()
        time.sleep(POLL_INTERVAL)

def resolve_id(id_or_alias):
    return ALIASES.get(id_or_alias.lower().replace(' ', '_'), id_or_alias)

@app.route('/')
def index():
    return jsonify({'name': 'SwitchBot Direct API Bridge', 'version': '2.0.0', 'devices': len(device_list), 'cached': len(device_cache), 'events': len(events_log), 'battery_tracked': len(battery_trends)})

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'devices': len(device_list), 'cached': len(device_cache), 'poll_interval': POLL_INTERVAL, 'event_bus': 'connected' if event_actions else 'waiting'})

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

# v2.0 endpoints
@app.route('/battery/trends')
def battery_trends_endpoint():
    results = {}
    for did, trend in battery_trends.items():
        name = NAMES.get(did, did)
        prediction = predict_replacement(did)
        results[name] = {'trend': trend[-30:], 'prediction': prediction}
    return jsonify(results)

@app.route('/motion/reliability')
def motion_reliability_endpoint():
    return jsonify(motion_reliability)

@app.route('/event-log')
def event_log():
    return jsonify(list(event_actions)[-20:])

if __name__ == '__main__':
    logger.info(f'SwitchBot Direct API Bridge v2.0.0 on port {API_PORT}')
    load_v2_data()
    fetch_devices()
    threading.Thread(target=poll_loop, daemon=True).start()
    # v2.0: Start Event Bus SSE subscriber
    threading.Thread(target=event_bus_subscriber, daemon=True).start()
    # v2.0: Start battery tracker
    threading.Thread(target=battery_tracker_loop, daemon=True).start()
    logger.info('Background poller + Event Bus subscriber + battery tracker started')
    app.run(host='0.0.0.0', port=API_PORT, debug=False)
