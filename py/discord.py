"""
discord.py — Discord integration for P2P Monitor
All embed payloads, post_discord(), bot API helpers, bot setup, bot command runner.
"""

import json
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from py.screenshot import SS_PRIORITY_EVENT, SS_PRIORITY_DROPS  # used by DiscordRouter


# ── Shared constants ───────────────────────────────────────────────────────────
DROP_ICONS = {'collection': '📒', 'untradeable': '💎', 'valuable': '💰', 'pet': '🐾'}


# ── Shared utilities ───────────────────────────────────────────────────────────
from py.util import now_str, fmt_ts

def _embed(title, desc, fields, color, image_filename=None):
    e = {
        "title":       title,
        "description": desc,
        "color":       color,
        "footer":      {"text": f"P2P Monitor — {fmt_ts(now_str())}"},
    }
    if fields:
        e["fields"] = fields
    if image_filename:
        e["image"] = {"url": f"attachment://{image_filename}"}
    return {"embeds": [e]}

# ── Embed payload builders ─────────────────────────────────────────────────────
def _desc(mention, folder):
    """Standard embed description: optional mention + account line."""
    return (f"<@{mention}>\n" if mention else "") + f"**Account:** {folder}"

def quest_started_payload(mention, folder, quest):
    return _embed("📜 Quest Started", _desc(mention, folder),
                  [{"name": "Quest", "value": quest, "inline": False}], 0x3a86ff)

def quest_payload(mention, folder, quest):
    return _embed("🏆 Quest Completed", _desc(mention, folder),
                  [{"name": "Quest", "value": quest, "inline": False}], 0x5bc65b)

def slayer_task_payload(mention, folder, monster, count):
    return _embed("🗡️ New Slayer Task", _desc(mention, folder),
                  [{"name": "Monster",    "value": monster,    "inline": True},
                   {"name": "Kill Count", "value": str(count), "inline": True}], 0xe07b39)

def slayer_complete_payload(mention, folder, monster, tasks_done, points_earned, total_points):
    fields = []
    if monster:
        fields.append({"name": "Task",         "value": monster,          "inline": True})
    if tasks_done is not None:
        fields.append({"name": "Tasks Done",   "value": str(tasks_done),  "inline": True})
    if points_earned is not None:
        fields.append({"name": "Points Earned","value": f"{points_earned:,}","inline": True})
        fields.append({"name": "Total Points", "value": f"{total_points:,}", "inline": True})
    return _embed("✅ Slayer Task Complete", _desc(mention, folder), fields, 0x5bc65b)

def slayer_skipped_payload(mention, folder, monster, reason):
    return _embed("⏭️ Slayer Task Skipped", _desc(mention, folder),
                  [{"name": "Monster", "value": monster, "inline": True},
                   {"name": "Reason",  "value": reason,  "inline": True}], 0xff9900)

def drop_payload(mention, folder, drop_types, item):
    TITLES = {'collection': 'Collection Log', 'untradeable': 'Untradeable Drop',
              'valuable':   'Valuable Drop',  'pet':         'Pet Drop'}
    COLORS = {'collection': 0x3a86ff, 'untradeable': 0x7b2fff,
              'valuable':   0xffd700,  'pet':         0x57ff6e}
    if isinstance(drop_types, str):
        drop_types = [drop_types]
    icons  = ' '.join(DROP_ICONS.get(t, '🎁') for t in drop_types)
    titles = ' + '.join(TITLES.get(t, t.title()) for t in drop_types)
    priority = ['pet', 'collection', 'untradeable', 'valuable']
    color  = next((COLORS[t] for t in priority if t in drop_types), 0xffffff)
    return _embed(f"{icons} {titles}", _desc(mention, folder),
                  [{"name": "Item", "value": item, "inline": False}], color)

def task_payload(mention, folder, task, activity):
    return _embed("📋 Task Update", _desc(mention, folder),
                  [{"name": "Task",     "value": task     or "—", "inline": True},
                   {"name": "Activity", "value": activity or "—", "inline": True}], 0x00d4ff)

def chat_payload(mention, folder, chat, response):
    return _embed("💬 Chat Event", _desc(mention, folder),
                  [{"name": "Chat",     "value": chat[:500]     or "—", "inline": False},
                   {"name": "Response", "value": response[:500] or "—", "inline": False}], 0xbb86fc)

def error_payload(mention, folder, label, detail, task_context=''):
    title = f"❌ Error — {task_context}" if task_context else "❌ Error Detected"
    return _embed(title, _desc(mention, folder),
                  [{"name": "Error",  "value": label,                   "inline": False},
                   {"name": "Detail", "value": (detail or "—")[:400],   "inline": False}], 0xff4444)

def script_event_payload(mention, folder, event, detail=''):
    icons  = {'start': '▶️', 'stop': '⏹️', 'pause': '⏸️', 'resume': '▶️'}
    colors = {'start': 0x00d4ff, 'stop': 0xff4444, 'pause': 0xffaa00, 'resume': 0x00cc88}
    titles = {'start': 'Script Started', 'stop': 'Script Stopped',
              'pause': 'Script Paused',  'resume': 'Script Resumed'}
    icon  = icons.get(event, 'ℹ️')
    color = colors.get(event, 0x7a8099)
    title = titles.get(event, event.title())
    fields = [{"name": "Event", "value": f"{icon} {title}", "inline": True}]
    if detail:
        fields.append({"name": "Detail", "value": detail, "inline": True})
    return _embed(f"{icon} Script Event", _desc(mention, folder), fields, color)

def death_payload(mention, folder, context=''):
    return _embed("💀 Character Died", _desc(mention, folder),
                  [{"name": "Detail", "value": context or "Oh dear, you are dead!", "inline": False}],
                  0xff0000)

def levelup_payload(mention, folder, skill, level, total_level=None):
    fields = [{"name": "Skill", "value": skill,      "inline": True},
              {"name": "Level", "value": str(level),  "inline": True}]
    if total_level:
        fields.append({"name": "Total Level", "value": str(total_level), "inline": True})
    return _embed("🎉 Level Up!", _desc(mention, folder), fields, 0xffd700)

def screenshot_payload(account, trigger):
    return _embed("📸 Screenshot", f"**Account:** {account}\n**Trigger:** {trigger}", [], 0x7a8099)

def combined_daily_summary_payload(mention, rows, window_str=''):
    desc = (f"<@{mention}>\n" if mention else "") + "**Daily Summary**"
    if window_str:
        desc += f"\n{window_str}"
    lines = []
    for r in rows:
        line = (
            f"**{r['account']}**  |  "
            f"Quests: {r.get('quests',0)}  "
            f"Tasks: {r.get('tasks',0)}  "
            f"Chats: {r.get('chats',0)}  "
            f"Errors: {r.get('errors',0)}  "
            f"Drops: {r.get('drops',0)}  "
            f"Deaths: {r.get('deaths',0)}  "
            f"Levels: {r.get('levels',0)}  |  "
            f"Uptime: {r.get('uptime','—')}  Break: {r.get('break_str','—')}"
        )
        lines.append(line)
    return _embed("📊 Daily Summary", desc + "\n\n" + "\n".join(lines), [], 0x7b2fff)

def status_text_payload(rows):
    lines = ["```",
             f"{'Account':<20} {'Task':<20} {'Activity':<18} {'Uptime':<10} {'Break':<10} Status",
             "-" * 95]
    for r in rows:
        lines.append(
            f"{r['account']:<20} {r['task']:<20} {r['activity']:<18} "
            f"{r.get('uptime','—'):<10} {r.get('break_time','—'):<10} {r['status']}"
        )
    lines.append("```")
    return {"content": "\n".join(lines)}

# ── HTTP helpers ───────────────────────────────────────────────────────────────
def _read_http_error(e):
    """Extract body text from an HTTPError for logging. Returns 'HTTP {code}: {body}'."""
    try:
        body = e.read().decode('utf-8', errors='replace')[:300]
    except Exception:
        body = ''
    return f"HTTP {e.code}: {body}"


# ── Post ───────────────────────────────────────────────────────────────────────
def post_discord(url, payload, image_path=None):
    """Post to Discord webhook. Returns (ok: bool, err: str)."""
    if not url or not url.strip().startswith('http'):
        return False, "No URL"
    try:
        if image_path and Path(image_path).exists():
            import copy
            boundary = "P2PMonitorBoundary7f3d"
            fname    = Path(image_path).name
            if payload and isinstance(payload, dict) and payload.get('embeds'):
                payload = copy.deepcopy(payload)
                for emb in payload['embeds']:
                    if 'image' not in emb:
                        emb['image'] = {"url": f"attachment://{fname}"}
            body  = b""
            body += f"--{boundary}\r\n".encode()
            body += b'Content-Disposition: form-data; name="payload_json"\r\n'
            body += b'Content-Type: application/json\r\n\r\n'
            body += json.dumps(payload).encode() + b"\r\n" if payload else b"{}\r\n"
            with open(image_path, 'rb') as fh:
                img_data = fh.read()
            body += f"--{boundary}\r\n".encode()
            body += f'Content-Disposition: form-data; name="file"; filename="{fname}"\r\n'.encode()
            body += b'Content-Type: image/png\r\n\r\n'
            body += img_data + b"\r\n"
            body += f"--{boundary}--\r\n".encode()
            req = urllib.request.Request(url, data=body,
                headers={'Content-Type': f'multipart/form-data; boundary={boundary}',
                         'User-Agent': 'P2PMonitor'}, method='POST')
        else:
            data = json.dumps(payload).encode('utf-8')
            req  = urllib.request.Request(url, data=data,
                headers={'Content-Type': 'application/json',
                         'User-Agent': 'P2PMonitor'}, method='POST')
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = resp.status in (200, 204)
            return (ok, '') if ok else (False, f"HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        return False, _read_http_error(e)
    except Exception as e:
        return False, str(e)

# ── Bot image delivery ────────────────────────────────────────────────────────
def post_bot_image(channel_id, token, account, image_path):
    """
    Post an image file to a Discord channel using the bot token (multipart).
    Returns (ok: bool, err: str). Unified delivery path used by the screenshot
    worker — keeps all Discord I/O in discord.py.
    """
    try:
        boundary = "P2PMonitorBotBoundary9a2f"
        caption  = json.dumps({"content": f"📸 **{account}**"})
        fname    = Path(image_path).name
        with open(image_path, 'rb') as fh:
            img_data = fh.read()
        body  = b""
        body += f"--{boundary}\r\n".encode()
        body += b'Content-Disposition: form-data; name="payload_json"\r\n'
        body += b'Content-Type: application/json\r\n\r\n'
        body += caption.encode() + b"\r\n"
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="file"; filename="{fname}"\r\n'.encode()
        body += b'Content-Type: image/png\r\n\r\n'
        body += img_data + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        req = urllib.request.Request(url, data=body,
            headers={'Authorization': f'Bot {token}',
                     'Content-Type':  f'multipart/form-data; boundary={boundary}',
                     'User-Agent':    'P2PMonitor'},
            method='POST')
        with urllib.request.urlopen(req, timeout=15) as resp:
            ok = resp.status in (200, 204)
            return (ok, '') if ok else (False, f"HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        return False, _read_http_error(e)
    except Exception as e:
        return False, str(e)


# ── Bot API ────────────────────────────────────────────────────────────────────
def bot_api(token, method, path, payload=None, timeout=10):
    """Make a Discord bot API call. Returns (data_or_None, error_str)."""
    url = f"https://discord.com/api/v10{path}"
    try:
        if payload is not None:
            data = json.dumps(payload).encode('utf-8')
            req  = urllib.request.Request(url, data=data,
                headers={'Authorization': f'Bot {token}',
                         'Content-Type':  'application/json',
                         'User-Agent':    'P2PMonitor'}, method=method)
        else:
            req = urllib.request.Request(url,
                headers={'Authorization': f'Bot {token}',
                         'User-Agent':    'P2PMonitor'}, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return (json.loads(body) if body else {}, '')
    except urllib.error.HTTPError as e:
        return None, _read_http_error(e)
    except Exception as ex:
        return None, str(ex)

def bot_setup_discord(token, server_id, log_fn=None):
    """
    Auto-create P2P Monitor category, channels, webhooks in a Discord server.
    Returns updated cfg fragment: {bot_channel_ids, bot_webhook_urls, bot_setup_done}.
    """
    def log(msg):
        if log_fn:
            log_fn(msg)

    CHANNEL_NAMES = ['monitor', 'tasks', 'quests', 'chat', 'errors', 'drops', 'deaths', 'levelup']
    CATEGORY_NAME = 'P2P Monitor'

    channels, err = bot_api(token, 'GET', f'/guilds/{server_id}/channels')
    if channels is None:
        raise Exception(f"Could not fetch server channels: {err}")

    category = next((ch for ch in channels
                     if ch.get('type') == 4 and ch.get('name','').lower() == CATEGORY_NAME.lower()), None)
    if category:
        cat_id = category['id']
        log(f"🤖 Reusing existing category '{CATEGORY_NAME}'")
    else:
        data, err = bot_api(token, 'POST', f'/guilds/{server_id}/channels',
                            {'name': CATEGORY_NAME, 'type': 4})
        if data is None:
            raise Exception(f"Could not create category: {err}")
        cat_id = data['id']
        log(f"🤖 Created category '{CATEGORY_NAME}'")

    channel_ids = {}
    for name in CHANNEL_NAMES:
        existing = next((ch for ch in channels
                         if ch.get('type') == 0 and ch.get('name','').lower() == name.lower()), None)
        if existing:
            channel_ids[name] = existing['id']
            if existing.get('parent_id') != cat_id:
                bot_api(token, 'PATCH', f'/channels/{existing["id"]}', {'parent_id': cat_id})
            log(f"🤖 Reusing channel #{name}")
        else:
            data, err = bot_api(token, 'POST', f'/guilds/{server_id}/channels',
                                {'name': name, 'type': 0, 'parent_id': cat_id})
            if data is None:
                raise Exception(f"Could not create channel #{name}: {err}")
            channel_ids[name] = data['id']
            log(f"🤖 Created channel #{name}")

    webhook_urls = {}
    for name, ch_id in channel_ids.items():
        wh_list, err = bot_api(token, 'GET', f'/channels/{ch_id}/webhooks')
        if wh_list is None:
            wh_list = []
        existing_wh = next((w for w in wh_list if w.get('token')), None) if wh_list else None
        if existing_wh:
            wh_url = f"https://discord.com/api/webhooks/{existing_wh['id']}/{existing_wh['token']}"
            webhook_urls[name] = wh_url
            log(f"🤖 Reusing webhook for #{name}")
        else:
            data, err = bot_api(token, 'POST', f'/channels/{ch_id}/webhooks',
                                {'name': f'P2P Monitor — {name}'})
            if data is None:
                raise Exception(f"Could not create webhook for #{name}: {err}")
            wh_url = f"https://discord.com/api/webhooks/{data['id']}/{data['token']}"
            webhook_urls[name] = wh_url
            log(f"🤖 Created webhook for #{name}")

    return {'bot_channel_ids': channel_ids, 'bot_webhook_urls': webhook_urls, 'bot_setup_done': True}

def bot_ensure_thread(token, channel_id, account_name, log_fn=None):
    """Find or create a thread for account_name in channel_id. Returns thread_id or None."""
    def log(msg):
        if log_fn:
            log_fn(msg)

    all_threads = []
    active_data, _ = bot_api(token, 'GET', f'/channels/{channel_id}/threads/active')
    if active_data and isinstance(active_data.get('threads'), list):
        all_threads.extend(active_data['threads'])
    for endpoint in [
        f'/channels/{channel_id}/threads/archived/public?limit=100',
        f'/channels/{channel_id}/threads/archived/private?limit=100',
    ]:
        data, _ = bot_api(token, 'GET', endpoint)
        if data and isinstance(data.get('threads'), list):
            all_threads.extend(data['threads'])

    existing = next((t for t in all_threads
                     if t.get('name','').lower() == account_name.lower()), None)
    if existing:
        tid = existing['id']
        if existing.get('thread_metadata', {}).get('archived'):
            bot_api(token, 'PATCH', f'/channels/{tid}', {'archived': False, 'locked': False})
        log(f"🤖 Reusing thread '{account_name}' in channel {channel_id}")
        return tid

    data, err = bot_api(token, 'POST', f'/channels/{channel_id}/threads',
                        {'name': account_name, 'type': 11, 'auto_archive_duration': 10080})
    if data is None:
        log(f"🤖 Could not create thread '{account_name}': {err}")
        return None
    log(f"🤖 Created thread '{account_name}' in channel {channel_id}")
    return data['id']


# ── DiscordRouter ──────────────────────────────────────────────────────────────

class DiscordRouter:
    """
    Owns all Discord delivery routing on behalf of LogWatcher.
    Resolves webhook URLs, thread IDs, mute state, and screenshot decisions.

    Watcher calls post_event / post_drop / post_task.
    This class decides URL, thread, and whether a screenshot accompanies the post.

    Callbacks:
        get_cfg()                                           → live cfg dict
        log(msg)
        is_muted(account)                                   → bool
        enqueue_screenshot(priority, account, trigger,
                           url, payload)                    → None
    """

    _CH_MAP = {
        'default': 'monitor', 'monitor': 'monitor',
        'quest':   'quests',  'task':    'tasks',
        'chat':    'chat',    'error':   'errors',
        'drops':   'drops',   'death':   'deaths',
        'levelup': 'levelup',
    }

    def __init__(self, callbacks):
        self._cb = callbacks

    # ── Internal helpers ───────────────────────────────────────────────────────
    def _cfg(self):
        return self._cb['get_cfg']()

    def mention(self):
        return self._cfg().get('mention_id', '').strip()

    def _wh(self, key):
        cfg     = self._cfg()
        ch_name = self._CH_MAP.get(key, 'monitor')
        bot_wh  = cfg.get('bot_webhook_urls', {}).get(ch_name, '').strip()
        manual  = cfg.get(f'webhook_{ch_name}', '').strip()
        return bot_wh or manual

    def _thread_id(self, account, key):
        ch_name = self._CH_MAP.get(key, 'monitor')
        return self._cfg().get('bot_thread_ids', {}).get(account, {}).get(ch_name)

    def wh_with_thread(self, key, account):
        """Return (url_with_thread_param, None). Used by ScreenshotService."""
        url = self._wh(key)
        tid = self._thread_id(account, key) if account else None
        if tid and url:
            sep = '&' if '?' in url else '?'
            return f"{url}{sep}thread_id={tid}", None
        return url, None

    def resolve_url(self, account, *keys):
        """First non-empty webhook URL from keys, falling back to 'default'."""
        for key in keys:
            url, _ = self.wh_with_thread(key, account)
            if url:
                return url
        url, _ = self.wh_with_thread('default', account)
        return url or ''

    # ── Public post surface ────────────────────────────────────────────────────
    def post_event(self, account, event_type, payload, url=None):
        """Post an event embed. Mute-guarded. Enqueues screenshot if configured."""
        if self._cb['is_muted'](account):
            return
        if url is None:
            url, _ = self.wh_with_thread(event_type, account)
        if not url:
            return
        if self._cfg().get(f'ss_event_{event_type}', False):
            self._cb['enqueue_screenshot'](SS_PRIORITY_EVENT, account, event_type,
                                           url=url, payload=payload)
        else:
            ok, err = post_discord(url, payload)
            if not ok:
                self._cb['log'](f"  🚫 Discord failed: {err}")

    def post_drop(self, account, drop_types, value):
        """Build and post a drop embed. Uses drop-priority screenshot if enabled."""
        if self._cb['is_muted'](account):
            return
        url, _ = self.wh_with_thread('drops', account)
        if not url:
            url, _ = self.wh_with_thread('default', account)
        if not url:
            return
        payload = drop_payload(self.mention(), account, drop_types, value)
        if self._cfg().get('ss_event_drops', False):
            self._cb['enqueue_screenshot'](SS_PRIORITY_DROPS, account, 'drop',
                                           url=url, payload=payload)
        else:
            ok, err = post_discord(url, payload)
            if not ok:
                self._cb['log'](f"  🚫 Discord failed: {err}")

    def post_task(self, account, task_name, activity,
                  title_override=None, footer_override=None):
        """Build and post a task embed via post_event (screenshot handled there)."""
        if self._cb['is_muted'](account):
            return
        url, _ = self.wh_with_thread('task', account)
        if not url:
            url, _ = self.wh_with_thread('default', account)
        if not url:
            return
        payload = task_payload(self.mention(), account, task_name, activity)
        if title_override:
            payload['embeds'][0]['title'] = title_override
        if footer_override:
            payload['embeds'][0]['footer'] = {'text': footer_override}
        self.post_event(account, 'task', payload, url=url)

    def post_script_event(self, account, ev_key):
        """Post a script lifecycle event (no screenshot)."""
        if self._cb['is_muted'](account):
            return
        url, _ = self.wh_with_thread('monitor', account)
        if not url:
            url = self._wh('default')
        if not url:
            return
        ok, err = post_discord(url, script_event_payload(self.mention(), account, ev_key))
        if not ok:
            self._cb['log'](f"  🚫 Discord failed: {err}")


# ── GatewayRunner ──────────────────────────────────────────────────────────────

class GatewayRunner:
    """
    Connects to the Discord Gateway via discord.py, registers slash commands,
    and dispatches interactions back to the watcher via callbacks.

    Replaces BotRunner (polling) — no channel ID needed, no message polling.
    Slash commands: /ss [account], /s

    Callbacks supplied by LogWatcher:
        get_rows()                           → list of account row dicts
        get_accounts()                       → list of account name strings
        on_screenshot(account, ch_id, token) → None
        log(msg)
        get_cfg()                            → live cfg dict
        is_running()                         → bool
    """

    COMMANDS = [
        {
            'name':        'ss',
            'description': 'Take a screenshot for an account',
            'options': [{
                'name':         'account',
                'description':  'Account name (leave blank for all)',
                'type':         3,   # STRING
                'required':     False,
                'autocomplete': True,
            }],
        },
        {
            'name':        's',
            'description': 'Post status of all monitored accounts to #monitor',
        },
        {
            'name':        'force',
            'description': 'Force a skill, action, or time adjustment for an account',
            'options': [
                {
                    'name':         'account',
                    'description':  'Account name',
                    'type':         3,   # STRING
                    'required':     True,
                    'autocomplete': True,
                },
                {
                    'name':         'adjustment',
                    'description':  'Action to perform',
                    'type':         3,   # STRING
                    'required':     True,
                    'autocomplete': True,
                },
                {
                    'name':         'amount',
                    'description':  'Number of times to click — only used for -10m / +10m (1-20)',
                    'type':         4,   # INTEGER
                    'required':     False,
                    'min_value':    1,
                    'max_value':    20,
                },
            ],
        },
    ]

    def __init__(self, cfg, callbacks):
        self.cfg = cfg
        self.cb  = callbacks

    def run(self):
        token = self.cfg.get('bot_token', '').strip()
        if not token:
            return

        # Ensure discord.py is available
        if not self._ensure_discord_py():
            return

        import discord

        # Register slash commands
        app_id = self._get_app_id(token)
        if app_id:
            self._register_commands(token, app_id)
        else:
            self.cb['log']("🤖 Could not fetch app ID — slash commands not registered")

        # Build and run the async gateway client
        cb      = self.cb
        cfg_ref = self.cfg

        class _Client(discord.Client):
            async def on_ready(self):
                cb['log'](f"🤖 Gateway connected — logged in as {self.user}")

            async def on_interaction(self, interaction):
                # type 4 = APPLICATION_COMMAND_AUTOCOMPLETE
                # type 2 = APPLICATION_COMMAND
                if interaction.type.value == 4:
                    await _autocomplete(interaction)
                elif interaction.type == discord.InteractionType.application_command:
                    await _dispatch(interaction, self)

        async def _autocomplete(interaction):
            cmd = interaction.data.get('name', '').lower()
            token_val = cfg_ref.get('bot_token', '').strip()

            def _respond(choices):
                bot_api(token_val, 'POST',
                        f"/interactions/{interaction.id}/{interaction.token}/callback",
                        {'type': 8, 'data': {'choices': choices[:25]}})

            if cmd in ('ss', 'force'):
                for opt in interaction.data.get('options', []):
                    if opt.get('name') == 'account' and opt.get('focused'):
                        typed    = opt.get('value', '').lower()
                        accounts = cb['get_accounts']()
                        choices  = []
                        if cmd == 'ss':
                            choices.append({'name': 'All accounts', 'value': 'all'})
                        choices += [{'name': a, 'value': a} for a in accounts
                                    if typed in a.lower()]
                        _respond(choices)
                        return
                    if opt.get('name') == 'adjustment' and opt.get('focused'):
                        typed   = opt.get('value', '').lower()
                        all_adjustments = [
                            'Stats', 'Loot',
                            '-10m', '+10m',
                            'Skip', 'Quest',
                            'Attack', 'Strength', 'Defence', 'Range',
                            'Agility', 'Herblore', 'Thieving',
                            'Mining', 'Smithing', 'Fishing', 'Cooking',
                            'Prayer', 'Magic', 'Runecrafting', 'Construction',
                            'Crafting', 'Fletching', 'Slayer', 'Hunter',
                            'Firemaking', 'Woodcutting', 'Farming', 'Sailing',
                        ]
                        choices = [{'name': k, 'value': k}
                                   for k in all_adjustments if typed in k.lower()]
                        _respond(choices)
                        return

        async def _dispatch(interaction, client):
            cmd  = interaction.data.get('name', '').lower()
            opts = {o['name']: o.get('value', '')
                    for o in interaction.data.get('options', [])}
            arg  = opts.get('account', '').strip()
            cfg  = cb['get_cfg']()

            # Thread scope: if invoked inside an account monitor thread, apply to that account
            ch_id         = str(interaction.channel_id)
            scope_account = None
            for acc, threads in cfg.get('bot_thread_ids', {}).items():
                if str(threads.get('monitor', '')) == ch_id:
                    scope_account = acc
                    break

            def resolve_targets():
                accounts = cb['get_accounts']()
                if scope_account:
                    return [scope_account] if scope_account in accounts else []
                if not arg or arg.lower() == 'all':
                    return list(accounts)
                return [a for a in accounts if arg.lower() in a.lower()]

            try:
                if cmd == 'ss':
                    targets = resolve_targets()
                    if not targets:
                        accounts = cb['get_accounts']()
                        await interaction.response.send_message(
                            f"No account matching '{arg}'. "
                            f"Monitored: {', '.join(accounts) or 'none'}",
                            ephemeral=True)
                        return
                    await interaction.response.defer(ephemeral=True)
                    token_val = cfg_ref.get('bot_token', '').strip()
                    for acc in targets:
                        tid  = cfg.get('bot_thread_ids', {}).get(acc, {}).get('monitor')
                        dest = tid if tid else ch_id
                        # on_screenshot calls get_focused_wid() (blocking subprocess) —
                        # must run off the event loop thread
                        threading.Thread(
                            target=cb['on_screenshot'],
                            args=(acc, dest, token_val), daemon=True).start()
                    await interaction.followup.send(
                        f"📸 Screenshot queued for: {', '.join(targets)}",
                        ephemeral=True)

                elif cmd == 's':
                    await interaction.response.defer(ephemeral=True)
                    rows    = cb['get_rows']()
                    payload = (status_text_payload(rows) if rows
                               else {"content": "No accounts monitored yet."})
                    monitor_ch = cfg.get('bot_channel_ids', {}).get('monitor', '').strip()
                    post_ok = False
                    if monitor_ch:
                        token_val = cfg_ref.get('bot_token', '').strip()
                        _, post_err = bot_api(token_val, 'POST',
                                              f'/channels/{monitor_ch}/messages', payload)
                        if post_err:
                            cb['log'](f"🤖 /s post failed: {post_err}")
                        else:
                            post_ok = True
                    else:
                        cb['log']("🤖 /s: no monitor channel ID configured")
                    reply = ("📊 Status posted to #monitor" if post_ok
                             else "⚠ Status post failed — check monitor logs.")
                    await interaction.followup.send(reply, ephemeral=True)

                elif cmd == 'force':
                    account    = opts.get('account', '').strip()
                    adjustment = opts.get('adjustment', '').strip()
                    accounts   = cb['get_accounts']()
                    matched    = next((a for a in accounts if a.lower() == account.lower()), None)
                    if not matched:
                        await interaction.response.send_message(
                            f"No account matching '{account}'. "
                            f"Monitored: {', '.join(accounts) or 'none'}",
                            ephemeral=True)
                        return

                    valid_adjustments = [
                        'Stats', 'Loot', '-10m', '+10m', 'Skip', 'Quest',
                        'Attack', 'Strength', 'Defence', 'Range', 'Agility',
                        'Herblore', 'Thieving', 'Mining', 'Smithing', 'Fishing',
                        'Cooking', 'Prayer', 'Magic', 'Runecrafting', 'Construction',
                        'Crafting', 'Fletching', 'Slayer', 'Hunter',
                        'Firemaking', 'Woodcutting', 'Farming', 'Sailing',
                    ]
                    if adjustment not in valid_adjustments:
                        await interaction.response.send_message(
                            f"Unknown action '{adjustment}'. "
                            f"Use autocomplete to pick a valid option.",
                            ephemeral=True)
                        return

                    await interaction.response.defer(ephemeral=True)
                    token_val = cfg_ref.get('bot_token', '').strip()

                    if adjustment in ('-10m', '+10m'):
                        # Time adjustment — click N times
                        amount = int(opts.get('amount', 1))
                        threading.Thread(
                            target=cb['on_force'],
                            args=(matched, adjustment, amount), daemon=True).start()
                        await interaction.followup.send(
                            f"⏱ Clicking {adjustment} × {amount} for {matched}",
                            ephemeral=True)

                    elif adjustment in ('Stats', 'Loot'):
                        # Panel toggle — open, screenshot, post to monitor thread, close
                        tid = cfg.get('bot_thread_ids', {}).get(matched, {}).get('monitor')
                        dest = tid if tid else str(interaction.channel_id)
                        threading.Thread(
                            target=cb['on_force_panel'],
                            args=(matched, adjustment, dest, token_val),
                            daemon=True).start()
                        await interaction.followup.send(
                            f"📊 Opening {adjustment} panel for {matched} — screenshot incoming",
                            ephemeral=True)

                    else:
                        # Skill / action — single click, no response needed
                        threading.Thread(
                            target=cb['on_force_skill'],
                            args=(matched, adjustment), daemon=True).start()
                        await interaction.followup.send(
                            f"🎯 Forcing {adjustment} for {matched}",
                            ephemeral=True)

            except Exception as e:
                cb['log'](f"🤖 Interaction error ({cmd}): {e}")
                try:
                    await interaction.followup.send(
                        "⚠ Command failed — check monitor logs.", ephemeral=True)
                except Exception:
                    pass

        import asyncio

        intents = discord.Intents.default()
        client  = _Client(intents=intents)

        async def _run_until_stopped():
            try:
                await client.start(token)
            except Exception as e:
                self.cb['log'](f"🤖 Gateway error: {e}")

        async def _shutdown():
            await client.close()

        loop = asyncio.new_event_loop()

        def _watchdog():
            while self.cb['is_running']():
                time.sleep(2)
            # Signal clean shutdown. Guard against the loop already being closed
            # if client.start() exited early and run() tore down the loop first.
            try:
                asyncio.run_coroutine_threadsafe(_shutdown(), loop)
            except RuntimeError:
                pass

        threading.Thread(target=_watchdog, daemon=True).start()
        try:
            loop.run_until_complete(_run_until_stopped())
        except Exception:
            pass
        finally:
            try:
                loop.run_until_complete(asyncio.sleep(0))  # drain pending callbacks
                loop.close()
            except Exception:
                pass

    def _ensure_discord_py(self):
        try:
            import discord  # noqa: F401
            return True
        except ImportError:
            pass
        self.cb['log']("🤖 discord.py not found — installing...")
        try:
            import subprocess, sys
            subprocess.check_call(
                [sys.executable, '-m', 'pip', 'install', 'discord.py',
                 '--break-system-packages', '--quiet'],
                timeout=120)
            self.cb['log']("🤖 discord.py installed successfully")
            return True
        except Exception as e:
            self.cb['log'](f"🤖 Failed to install discord.py: {e}")
            return False

    def _get_app_id(self, token):
        data, _ = bot_api(token, 'GET', '/oauth2/applications/@me')
        return data.get('id') if data else None

    def _register_commands(self, token, app_id):
        server_id = self.cfg.get('bot_server_id', '').strip()
        if not server_id:
            self.cb['log']("🤖 No Server ID — slash commands not registered")
            return
        path      = f'/applications/{app_id}/guilds/{server_id}/commands'
        ok_count  = 0
        fail_count = 0
        for cmd in self.COMMANDS:
            _, err = bot_api(token, 'POST', path, cmd)
            if not err:
                ok_count += 1
            else:
                fail_count += 1
                self.cb['log'](f"🤖 Failed to register /{cmd['name']}: {err}")
        if fail_count:
            self.cb['log'](f"🤖 {fail_count} slash command(s) failed to register in guild {server_id}")


# ── BotRunner tombstone (removed v5.4.0) ──────────────────────────────────────
# Replaced by GatewayRunner. Name kept so stale imports raise a clear error.
class BotRunner:
    def __init__(self, *a, **kw):
        raise RuntimeError(
            "BotRunner was removed in v5.4.0. Use GatewayRunner instead.")
