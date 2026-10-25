"""Authorized Telegram command polling. Only the configured chat can control trading."""
from __future__ import annotations
import json, logging, threading, time, urllib.parse, urllib.request
from typing import Any, Callable, Optional

log = logging.getLogger('bot.telegram')

class TelegramControl:
    def __init__(self, engine: Any, token: str, chat_id: str, http: Optional[Callable] = None):
        self.engine, self.token, self.chat_id = engine, (token or '').strip(), str(chat_id or '').strip()
        self.http = http or self._http
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name='telegram-control', daemon=True)
        self.offset: Optional[int] = None

    @staticmethod
    def _http(url: str, data: Optional[dict] = None, timeout: int = 25) -> dict:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode() if data else None)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode())

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def start(self):
        if self.enabled:
            self.thread.start()
        return self

    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive(): self.thread.join(timeout=2)

    def send(self, message: str):
        if not self.enabled: return
        try:
            self.http(f'https://api.telegram.org/bot{self.token}/sendMessage', {'chat_id': self.chat_id, 'text': message[:3500]}, 8)
        except Exception as exc:
            log.warning('Telegram command response failed: %s', type(exc).__name__)

    def _command(self, text: str) -> str:
        cmd = (text or '').strip().split()[0].split('@')[0].lower() if (text or '').strip() else ''
        if cmd == '/help':
            return 'Commands: /status, /pause, /resume, /reset, /help. /reset resets risk baselines to current equity; it does not close or modify positions/orders.'
        if cmd == '/status':
            s = self.engine.get_state(); r = s.get('risk', {}); h = s.get('system', {}).get('halted', {})
            return (f"{s.get('display_mode')} | {s.get('symbol')}\nTrading: {'HALTED: '+str(h.get('reason')) if h.get('halted') else ('PAUSED' if h.get('paused') else 'running')}\n"
                    f"Equity: {r.get('equity')} USDT | daily loss: {r.get('daily_loss_pct')}% | drawdown: {r.get('drawdown_pct')}%\n"
                    f"Scanner: {s.get('scanner', {}).get('note', 'n/a')}\nAI: {s.get('ai', {}).get('decision', s.get('ai', {}).get('error', 'waiting'))}")
        if cmd == '/pause':
            self.engine.pause(); return 'Trading paused. Monitoring continues; existing positions and orders are left untouched.'
        if cmd == '/resume':
            result = self.engine.resume()
            return 'Trading resumed.' if result.get('ok') else 'Resume refused: ' + str(result.get('error', 'unknown reason'))
        if cmd == '/reset':
            result = self.engine.reset_risk_baseline()
            return ('Risk halt reset; risk baseline reset at current verified equity. Existing positions and orders were not modified. ' +
                    'Trading can resume.' if result.get('ok') else 'Risk reset refused: ' + str(result.get('error', 'unknown reason')))
        return 'Unknown command. Send /help.'

    def run(self):
        base = f'https://api.telegram.org/bot{self.token}/'
        # Discard queued old commands at startup so a stale /resume cannot enable trading.
        try:
            old = self.http(base + 'getUpdates', {'timeout': 0}, 5)
            if old.get('ok'):
                updates = old.get('result', [])
                if updates: self.offset = max(u['update_id'] for u in updates) + 1
        except Exception: pass
        while not self.stop_event.is_set():
            try:
                params = {'timeout': 20}
                if self.offset is not None: params['offset'] = self.offset
                result = self.http(base + 'getUpdates', params, 25)
                if not result.get('ok'): continue
                for update in result.get('result', []):
                    self.offset = int(update.get('update_id', 0)) + 1
                    msg = update.get('message') or update.get('edited_message') or {}
                    chat = str((msg.get('chat') or {}).get('id', ''))
                    if chat != self.chat_id: continue
                    text = msg.get('text', '')
                    if text.startswith('/'):
                        self.send(self._command(text))
            except Exception as exc:
                log.warning('Telegram polling error: %s', type(exc).__name__)
                self.stop_event.wait(3)
