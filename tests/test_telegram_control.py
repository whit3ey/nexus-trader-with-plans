from bot.telegram_control import TelegramControl

class FakeEngine:
    def __init__(self): self.paused=False; self.resets=0
    def pause(self): self.paused=True
    def resume(self): return {'ok': True}
    def reset_risk_baseline(self): self.resets+=1; return {'ok':True,'equity':123,'positions_modified':False,'orders_modified':False}
    def get_state(self): return {'display_mode':'PAPER MODE','symbol':'BTCUSDT','risk':{'equity':123,'daily_loss_pct':0,'drawdown_pct':0},'system':{'halted':{'halted':False,'paused':False}},'scanner':{'note':'ok'},'ai':{'decision':'WAIT'}}

def test_commands_are_available_and_reset_is_explicit():
    e=FakeEngine(); t=TelegramControl(e,'token','123')
    assert 'PAPER MODE' in t._command('/status')
    assert 'paused' in t._command('/pause').lower() and e.paused
    assert 'resumed' in t._command('/resume').lower()
    assert 'baseline reset' in t._command('/reset').lower() and e.resets==1
    assert '/status' in t._command('/help')
