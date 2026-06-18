"""
discord.py — Discord integration for P2P Monitor v1.4.0
All embed payloads, post_discord(), bot API helpers, bot setup, bot command runner.
"""

import json
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
# NOTE: screenshot.py also imports from discord.py, but defers that import to
# inside ScreenshotService._worker() at runtime to avoid a circular import at
# module load time. Do NOT move that import to the top of screenshot.py.
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
    """Standard embed description: account line only.
    Mention is intentionally removed — real pings go via top-level content + allowed_mentions.
    The mention param is kept for call-site compat but is no longer embedded.
    """
    return f"**Account:** {folder}"


def normalize_mention_id(raw: str) -> str:
    """Normalize <@123>, <@!123>, or 123 → '123'. Returns '' if not a valid snowflake."""
    import re as _re
    if not raw:
        return ''
    m = _re.search(r'(\d{15,21})', raw)
    return m.group(1) if m else ''


def apply_ping(payload: dict, mention_id: str, enabled: bool) -> dict:
    """
    Attach top-level content + allowed_mentions to payload when ping is enabled.
    Returns the (mutated) payload dict.
    If enabled is False or mention_id is empty, payload is returned unchanged.
    """
    uid = normalize_mention_id(mention_id)
    if not enabled or not uid:
        return payload
    mention_str = f"<@{uid}>"
    existing = payload.get('content', '')
    if existing:
        if mention_str not in existing:
            payload['content'] = f"{mention_str} {existing}"
    else:
        payload['content'] = mention_str
    payload['allowed_mentions'] = {'users': [uid]}
    return payload

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

def levelup_payload(mention, folder, skill, level, total_level=None, is_99=False):
    title = "🎆 Level 99! 🎆" if is_99 else "🎉 Level Up!"
    fields = [{"name": "Skill", "value": skill,      "inline": True},
              {"name": "Level", "value": str(level),  "inline": True}]
    if total_level:
        fields.append({"name": "Total Level", "value": str(total_level), "inline": True})
    return _embed(title, _desc(mention, folder), fields, 0xffd700)

def screenshot_payload(account, trigger):
    return _embed("📸 Screenshot", f"**Account:** {account}\n**Trigger:** {trigger}", [], 0x7a8099)

def combined_daily_summary_payload(mention, rows, window_str=''):
    desc = "**Daily Summary**"
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


def _is_discord_404(err_str):
    """Detect Discord 404 / Unknown Channel (10003) / Unknown Webhook (10015).
    Returns True if the error indicates a deleted resource that can be recreated."""
    if not err_str:
        return False
    if 'HTTP 404' in err_str:
        return True
    if '10003' in err_str:
        return True
    if '10015' in err_str:
        return True
    return False


def _is_discord_auth_error(err_str):
    """Detect 401 Unauthorized or 403 Forbidden (bot kicked/token invalid)."""
    if not err_str:
        return False
    if 'HTTP 401' in err_str:
        return True
    if 'HTTP 403' in err_str:
        return True
    if '50001' in err_str:
        return True
    return False


def _add_recovery_footer(payload, message):
    """Add a footer note to an embed payload indicating recovery happened."""
    import copy as _copy
    if not payload or not isinstance(payload, dict):
        return payload
    payload = _copy.deepcopy(payload)
    embeds = payload.get('embeds', [])
    if embeds and isinstance(embeds[0], dict):
        existing_footer = embeds[0].get('footer', {}).get('text', '')
        if existing_footer:
            embeds[0]['footer'] = {'text': f"{existing_footer} | {message}"}
        else:
            embeds[0]['footer'] = {'text': message}
    return payload


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
        invalidate_threads(account)                         → None  (evict from _threads_verified)
        ensure_threads(account)                             → None  (re-create threads)
        run_bot_setup()                                     → None  (re-create channels/webhooks)
        save_cfg()                                          → None  (persist config to disk)
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

    # ── Self-healing helpers ───────────────────────────────────────────────────
    def _find_ch_name_for_webhook(self, url):
        """Given a webhook URL (possibly with ?thread_id=), find its channel name."""
        if not url:
            return None
        base_url = url.split('?')[0] if '?' in url else url
        for name, wh in self._cfg().get('bot_webhook_urls', {}).items():
            if wh.split('?')[0] == base_url:
                return name
        return None

    def _find_ch_name_for_thread(self, account, url):
        """Extract thread_id from URL and find its channel name."""
        if not url:
            return None, None
        tid = None
        for sep in ('?thread_id=', '&thread_id='):
            if sep in url:
                tid = url.split(sep)[1].split('&')[0]
                break
        if not tid:
            return None, None
        acct_threads = self._cfg().get('bot_thread_ids', {}).get(account, {})
        for ch_name, stored_tid in acct_threads.items():
            if str(stored_tid) == str(tid):
                return ch_name, tid
        return None, tid

    def _invalidate_thread(self, account, ch_name):
        """Remove a stale thread ID and evict account from verified set."""
        cfg = self._cfg()
        thread_ids = cfg.get('bot_thread_ids', {})
        acct_threads = thread_ids.get(account, {})
        if ch_name in acct_threads:
            del acct_threads[ch_name]
            self._cb['log'](f"🔧 [{account}] Invalidated stale thread for #{ch_name}")
            return True
        return False

    def _invalidate_channel(self, ch_name):
        """Remove a stale channel ID, webhook URL, and all thread entries for it."""
        cfg = self._cfg()
        changed = False
        if ch_name in cfg.get('bot_channel_ids', {}):
            del cfg['bot_channel_ids'][ch_name]
            changed = True
        if ch_name in cfg.get('bot_webhook_urls', {}):
            del cfg['bot_webhook_urls'][ch_name]
            changed = True
        for acct, threads in cfg.get('bot_thread_ids', {}).items():
            if ch_name in threads:
                del threads[ch_name]
                changed = True
        if changed:
            self._cb['log'](f"🔧 Invalidated stale channel #{ch_name}")
        return changed

    def _invalidate_webhook(self, ch_name):
        """Remove a stale webhook URL so bot setup recreates it."""
        cfg = self._cfg()
        if ch_name in cfg.get('bot_webhook_urls', {}):
            del cfg['bot_webhook_urls'][ch_name]
            self._cb['log'](f"🔧 Invalidated stale webhook for #{ch_name}")
            return True
        return False

    def _handle_post_error(self, err, url, account, retry_fn):
        """
        Check if a failed post is recoverable. If it's a 404, invalidate the
        stale resource, trigger recreation via callbacks, and call retry_fn
        with the updated URL to retry.

        Args:
            err       — error string from the failed post
            url       — the URL that failed
            account   — account name for thread context
            retry_fn  — callable(new_url) -> (ok, err) that retries the post

        Returns True if recovery + retry succeeded, False otherwise.
        """
        if not err:
            return False

        if _is_discord_auth_error(err):
            cfg = self._cfg()
            if 'HTTP 401' in err:
                cfg['bot_setup_done'] = False
                self._cb['log']("🤖 Bot token is invalid or expired — update token in Settings and re-run Bot Setup")
            else:
                self._cb['log']("🤖 Bot was removed from server or lacks permissions — re-invite and re-run Bot Setup")
            return False

        if not _is_discord_404(err):
            return False

        # ── 404 recovery ──────────────────────────────────────────────────
        self._cb['log'](f"🔧 [{account}] Discord 404 detected — attempting recovery...")

        # Determine what was deleted
        ch_name_thread, tid = self._find_ch_name_for_thread(account, url) if account else (None, None)
        ch_name_wh = self._find_ch_name_for_webhook(url)

        recovered = False

        if ch_name_thread and tid:
            # Thread was deleted — invalidate and recreate
            self._invalidate_thread(account, ch_name_thread)
            if 'invalidate_threads' in self._cb:
                self._cb['invalidate_threads'](account)
            if 'ensure_threads' in self._cb:
                self._cb['ensure_threads'](account)
                recovered = True
        elif ch_name_wh:
            # Webhook or channel was deleted
            if '10015' in err:
                self._invalidate_webhook(ch_name_wh)
            else:
                self._invalidate_channel(ch_name_wh)
            if 'run_bot_setup' in self._cb:
                try:
                    result = self._cb['run_bot_setup']()
                    # _run_bot_setup returns (ok, msg) tuple
                    if isinstance(result, tuple):
                        setup_ok = result[0]
                    else:
                        setup_ok = bool(result)
                    if setup_ok:
                        recovered = True
                    else:
                        self._cb['log'](f"🔧 Recovery bot setup returned failure: {result}")
                except Exception as e:
                    self._cb['log'](f"🔧 Recovery bot setup failed: {e}")
            if account and 'ensure_threads' in self._cb:
                if 'invalidate_threads' in self._cb:
                    self._cb['invalidate_threads'](account)
                self._cb['ensure_threads'](account)
        elif not url and '10003' in err:
            # Bot-image post with no webhook URL — the monitor channel itself
            # was deleted. Invalidate it and re-run bot setup to recreate.
            self._invalidate_channel('monitor')
            if 'run_bot_setup' in self._cb:
                try:
                    result = self._cb['run_bot_setup']()
                    if isinstance(result, tuple):
                        setup_ok = result[0]
                    else:
                        setup_ok = bool(result)
                    if setup_ok:
                        recovered = True
                    else:
                        self._cb['log'](f"🔧 Recovery bot setup returned failure: {result}")
                except Exception as e:
                    self._cb['log'](f"🔧 Recovery bot setup failed: {e}")
            if account and 'ensure_threads' in self._cb:
                if 'invalidate_threads' in self._cb:
                    self._cb['invalidate_threads'](account)
                self._cb['ensure_threads'](account)

        if not recovered:
            self._cb['log'](f"🔧 [{account}] Could not identify deleted resource — run Bot Setup manually")
            return False

        # Save config after recovery
        if 'save_cfg' in self._cb:
            self._cb['save_cfg']()

        # Resolve the new URL after recreation
        new_url = url
        if ch_name_thread:
            new_url_base, _ = self.wh_with_thread(ch_name_thread, account)
            if new_url_base:
                new_url = new_url_base
        elif ch_name_wh:
            new_url_base, _ = self.wh_with_thread(ch_name_wh, account)
            if new_url_base:
                new_url = new_url_base

        # Retry via caller-supplied function
        self._cb['log'](f"🔧 [{account}] Retrying post after recovery...")
        retry_ok, retry_err = retry_fn(new_url)
        if retry_ok:
            self._cb['log'](f"🔧 [{account}] Recovery successful — message delivered")
        else:
            self._cb['log'](f"🔧 [{account}] Recovery retry failed: {retry_err}")
        return retry_ok

    # ── Public post surface ────────────────────────────────────────────────────
    def post_event(self, account, event_type, payload, url=None):
        """Post an event embed. Mute-guarded. Enqueues screenshot if configured.
        If screenshot enqueue fails, falls back to posting the embed without screenshot."""
        if self._cb['is_muted'](account):
            return
        if url is None:
            url, _ = self.wh_with_thread(event_type, account)
        if not url:
            return
        if self._cfg().get(f'ss_event_{event_type}', False):
            queued = self._cb['enqueue_screenshot'](SS_PRIORITY_EVENT, account, event_type,
                                                    url=url, payload=payload)
            if queued:
                return  # screenshot worker will post the embed with image
            # Fallback: enqueue refused (queue full, muted, etc.) — post embed-only
            if self._cfg().get('debug', False):
                self._cb['log'](f'[DEBUG] [{account}] Event screenshot enqueue failed — '
                                f'falling back to embed-only for {event_type}')
        ok, err = post_discord(url, payload)
        if not ok:
            def _retry(new_url, _p=payload):
                return post_discord(new_url, _add_recovery_footer(_p,
                    "⚠ Thread/channel was recreated — screenshot may be delayed"))
            if not self._handle_post_error(err, url, account, _retry):
                self._cb['log'](f"  🚫 Discord failed: {err}")

    def post_drop(self, account, drop_types, value):
        """Build and post a drop embed. Uses drop-priority screenshot if enabled.
        If screenshot enqueue fails, falls back to posting the embed without screenshot."""
        if self._cb['is_muted'](account):
            return
        url, _ = self.wh_with_thread('drops', account)
        if not url:
            url, _ = self.wh_with_thread('default', account)
        if not url:
            return
        payload = drop_payload(self.mention(), account, drop_types, value)
        if self._cfg().get('ping_drops', False):
            apply_ping(payload, self.mention(), True)
        if self._cfg().get('ss_event_drops', False):
            queued = self._cb['enqueue_screenshot'](SS_PRIORITY_DROPS, account, 'drop',
                                                    url=url, payload=payload)
            if queued:
                return  # screenshot worker will post embed with image
            # Fallback: enqueue refused — post embed-only
            if self._cfg().get('debug', False):
                self._cb['log'](f'[DEBUG] [{account}] Drop screenshot enqueue failed — '
                                f'falling back to embed-only')
        ok, err = post_discord(url, payload)
        if not ok:
            def _retry(new_url, _p=payload):
                return post_discord(new_url, _add_recovery_footer(_p,
                    "⚠ Thread/channel was recreated — screenshot may be delayed"))
            if not self._handle_post_error(err, url, account, _retry):
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
        apply_ping(payload, self.mention(), self._cfg().get('ping_task', False))
        self.post_event(account, 'task', payload, url=url)

    def post_script_event(self, account, ev_key, detail=''):
        """Post a script lifecycle event. Pings if mention is set and ping_script_event is on."""
        if self._cb['is_muted'](account):
            return
        url, _ = self.wh_with_thread('monitor', account)
        if not url:
            url = self._wh('default')
        if not url:
            return
        payload = script_event_payload(self.mention(), account, ev_key, detail=detail)
        # Script events ping if a mention is set AND the script-event ping toggle is on.
        apply_ping(payload, self.mention(),
                   bool(self.mention()) and self._cfg().get('ping_script_event', True))
        ok, err = post_discord(url, payload)
        if not ok:
            def _retry(new_url, _p=payload):
                return post_discord(new_url, _add_recovery_footer(_p,
                    "⚠ Thread/channel was recreated — screenshot may be delayed"))
            if not self._handle_post_error(err, url, account, _retry):
                self._cb['log'](f"  🚫 Discord failed: {err}")


# ── GatewayRunner ──────────────────────────────────────────────────────────────

class GatewayRunner:
    """
    Connects to the Discord Gateway via discord.py, registers slash commands,
    and dispatches interactions back to the watcher via callbacks.

    Slash commands: /ss [account], /s, /force, /launch, /relaunch

    Callbacks: get_rows, get_accounts, on_screenshot, on_launch, on_launch_all,
               on_relaunch, on_relaunch_all, log, get_cfg, is_running
    """

    COMMANDS = [
        {
            'name':        'ss',
            'description': 'Take a screenshot for an account',
            'options': [{'name': 'account', 'description': 'Account name (leave blank for all)',
                         'type': 3, 'required': False, 'autocomplete': True}],
        },
        {'name': 's', 'description': 'Post status of all monitored accounts to #monitor'},
        {
            'name':        'force',
            'description': 'Force a skill, action, or time adjustment for an account',
            'options': [
                {'name': 'account',    'description': 'Account name',
                 'type': 3, 'required': True,  'autocomplete': True},
                {'name': 'adjustment', 'description': 'Action to perform',
                 'type': 3, 'required': True,  'autocomplete': True},
                {'name': 'amount',     'description': 'Clicks — only for -10m/+10m (1-20)',
                 'type': 4, 'required': False, 'min_value': 1, 'max_value': 20},
            ],
        },
        {
            'name':        'launch',
            'description': 'Launch a DreamBot account by preset (skips if already running)',
            'options': [{'name': 'account', 'description': 'Account name, or "all"',
                         'type': 3, 'required': True, 'autocomplete': True}],
        },
        {
            'name':        'relaunch',
            'description': 'Restart a DreamBot account — closes if open, launches fresh',
            'options': [{'name': 'account', 'description': 'Account name, or "all"',
                         'type': 3, 'required': True, 'autocomplete': True}],
        },
    ]

    def __init__(self, cfg, callbacks):
        self.cfg       = cfg
        self.cb        = callbacks
        self.bot_ready = threading.Event()  # set when gateway on_ready fires

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
        cb        = self.cb
        cfg_ref   = self.cfg
        bot_ready = self.bot_ready

        class _Client(discord.Client):
            async def on_ready(self):
                cb['log'](f"🤖 Gateway connected — logged in as {self.user}")
                bot_ready.set()

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

            if cmd in ('launch', 'relaunch'):
                for opt in interaction.data.get('options', []):
                    if opt.get('name') == 'account' and opt.get('focused'):
                        typed   = opt.get('value', '').lower()
                        presets = cfg_ref.get('launcher_presets', [])
                        choices = [{'name': 'All accounts', 'value': 'all'}]
                        choices += [
                            {'name': p['account'], 'value': p['account']}
                            for p in presets
                            if p.get('account') and typed in p['account'].lower()
                        ]
                        _respond(choices[:25])
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

                elif cmd == 'launch':
                    account_arg = opts.get('account', '').strip()
                    if not account_arg:
                        await interaction.response.send_message(
                            'Please specify an account name or "all".',
                            ephemeral=True)
                        return

                    on_launch     = cb.get('on_launch')
                    on_launch_all = cb.get('on_launch_all')
                    if not on_launch or not on_launch_all:
                        await interaction.response.send_message(
                            '⚠ Launch callbacks not available — is the monitor running?',
                            ephemeral=True)
                        return

                    await interaction.response.defer(ephemeral=True)
                    import asyncio as _asyncio
                    loop = _asyncio.get_event_loop()

                    if account_arg.lower() == 'all':
                        results = await loop.run_in_executor(None, on_launch_all)
                        launched = sum(1 for r in results if r.ok and r.action == 'launched')
                        skipped  = sum(1 for r in results if r.action == 'skipped')
                        failed   = sum(1 for r in results
                                       if not r.ok and r.action == 'failed')
                        lines = ['**Launch all complete:**']
                        for r in results:
                            icon = ('✅' if r.ok else
                                    ('⚠️' if r.action == 'skipped' else '❌'))
                            lines.append(f'{icon} **{r.account}**: {r.message}')
                        summary = (f'\n✅ {launched} launched  '
                                   f'⚠️ {skipped} skipped (already open or unsafe)  '
                                   f'❌ {failed} failed')
                        lines.append(summary)
                        msg = '\n'.join(lines)
                        if len(msg) > 1900:
                            msg = (f'**Launch all complete:**\n'
                                   f'✅ {launched} launched  '
                                   f'⚠️ {skipped} skipped  ❌ {failed} failed\n'
                                   f'_(Per-account detail truncated — see monitor log)_')
                        await interaction.followup.send(msg, ephemeral=True)

                    else:
                        result = await loop.run_in_executor(
                            None, lambda: on_launch(account_arg))
                        if result.ok:
                            icon = '✅'
                        elif result.action == 'skipped':
                            icon = '⚠️'
                        else:
                            icon = '❌'
                        await interaction.followup.send(
                            f'{icon} {result.message}', ephemeral=True)

                elif cmd == 'relaunch':
                    account_arg = opts.get('account', '').strip()
                    if not account_arg:
                        await interaction.response.send_message(
                            'Please specify an account name or "all".', ephemeral=True)
                        return

                    on_relaunch     = cb.get('on_relaunch')
                    on_relaunch_all = cb.get('on_relaunch_all')
                    if not on_relaunch or not on_relaunch_all:
                        await interaction.response.send_message(
                            '⚠ Relaunch callbacks not available — is the monitor running?',
                            ephemeral=True)
                        return

                    await interaction.response.defer(ephemeral=True)
                    import asyncio as _asyncio
                    loop = _asyncio.get_event_loop()

                    if account_arg.lower() == 'all':
                        results = await loop.run_in_executor(None, on_relaunch_all)
                        relaunched = sum(1 for r in results if r.ok)
                        skipped    = sum(1 for r in results if r.action == 'skipped')
                        failed     = sum(1 for r in results if not r.ok and r.action == 'failed')
                        lines_out  = ['**Relaunch all complete:**']
                        for r in results:
                            icon = ('✅' if r.ok else ('⚠️' if r.action == 'skipped' else '❌'))
                            lines_out.append(f'{icon} **{r.account}**: {r.message}')
                        lines_out.append(f'\n✅ {relaunched} relaunched  '
                                         f'⚠️ {skipped} skipped  ❌ {failed} failed')
                        msg = '\n'.join(lines_out)
                        if len(msg) > 1900:
                            msg = (f'**Relaunch all complete:**\n'
                                   f'✅ {relaunched} relaunched  ⚠️ {skipped} skipped  '
                                   f'❌ {failed} failed\n'
                                   f'_(Detail truncated — see monitor log)_')
                        await interaction.followup.send(msg, ephemeral=True)
                    else:
                        result = await loop.run_in_executor(None, lambda: on_relaunch(account_arg))
                        icon = ('🔄' if result.ok and result.action == 'relaunched' else
                                '✅' if result.ok else
                                '⚠️' if result.action == 'skipped' else '❌')
                        await interaction.followup.send(f'{icon} {result.message}', ephemeral=True)

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
        from py.util import is_frozen
        if is_frozen():
            # Packaged build — pip is not available. discord.py must be bundled.
            self.cb['log'](
                '🤖 discord.py is not bundled in this build — '
                'please reinstall or update to a build that includes it')
            return False
        self.cb['log']("🤖 discord.py not found — installing...")
        try:
            import subprocess
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

