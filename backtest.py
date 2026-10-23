"""Daily close signals, next-session open execution; research only."""
import math
import numpy as np
import pandas as pd

STRATEGIES = {'ma_cross':'MA5/MA20 黃金/死亡交叉','kd_cross':'KD K/D 交叉（低檔進場）','rsi_reversion':'RSI14 超賣反轉','macd_cross':'MACD DIF/訊號線交叉','volume_breakout':'20 日高點放量突破'}

def signals(d, strategy):
    if strategy == 'ma_cross':
        fast,slow=d.MA5,d.MA20
        entry=(fast>slow)&(fast.shift(1)<=slow.shift(1)); exit_=(fast<slow)&(fast.shift(1)>=slow.shift(1))
    elif strategy == 'kd_cross':
        entry=(d.K>d.D)&(d.K.shift(1)<=d.D.shift(1))&(d.K.shift(1)<40)
        exit_=(d.K<d.D)&(d.K.shift(1)>=d.D.shift(1))
    elif strategy == 'rsi_reversion':
        entry=(d.RSI>30)&(d.RSI.shift(1)<=30);exit_=(d.RSI>=65)
    elif strategy == 'macd_cross':
        entry=(d.DIF>d.DEA)&(d.DIF.shift(1)<=d.DEA.shift(1));exit_=(d.DIF<d.DEA)&(d.DIF.shift(1)>=d.DEA.shift(1))
    elif strategy == 'volume_breakout':
        entry=(d.Close>d.PREV20HIGH)&(d.Volume>=d.VMA20*1.5)
        exit_=(d.Close<d.MA20)
    else: raise ValueError('不支援的策略')
    return entry.fillna(False),exit_.fillna(False)

def simulate(d, strategy, fee_bps=14.25, tax_bps=30, slippage_bps=5, initial=100000):
    """Long only, all-in, integer shares; fees both sides, sell tax only.
    No same-bar execution: signal after close -> next open. Open position marked to last close.
    """
    if len(d)<100: raise ValueError('歷史日線不足 100 根')
    buy, sell=signals(d,strategy)
    cash=float(initial); qty=0; entry_total=0; entry_dt=None; trades=[]; equity=[]
    fee=fee_bps/10000; tax=tax_bps/10000; slip=slippage_bps/10000
    for i,(dt,r) in enumerate(d.iterrows()):
        op=float(r.Open);close=float(r.Close)
        if i>0 and np.isfinite(op) and op>0:
            if qty and bool(sell.iloc[i-1]):
                proceeds=qty*op*(1-slip)*(1-fee-tax)
                pnl=proceeds-entry_total
                trades.append({'entry_date':str(entry_dt.date()),'exit_date':str(dt.date()),'entry_cost':round(entry_total,2),'exit_value':round(proceeds,2),'pnl':round(pnl,2),'return_pct':round(100*pnl/entry_total,2)})
                cash+=proceeds;qty=0;entry_total=0
            elif not qty and bool(buy.iloc[i-1]):
                px=op*(1+slip);q=int(cash/(px*(1+fee)))
                if q>0:
                    entry_total=q*px*(1+fee);cash-=entry_total;qty=q;entry_dt=dt
        equity.append((dt,cash+qty*close))
    eq=pd.Series(dict(equity),dtype=float)
    max_dd=(eq/eq.cummax()-1).min()*100
    closed=len(trades);wins=sum(t['pnl']>0 for t in trades)
    realized=sum(t['pnl'] for t in trades)
    days=(eq.index[-1]-eq.index[0]).days
    total=100*(eq.iloc[-1]/initial-1)
    bh=100*(d.Close.iloc[-1]/d.Close.iloc[0]-1)
    return {'start':str(d.index[0].date()),'end':str(d.index[-1].date()),'bars':len(d),
      'trades_closed':closed,'win_rate_pct':round(wins/closed*100,2) if closed else None,
      'avg_trade_return_pct':round(float(np.mean([t['return_pct'] for t in trades])),2) if closed else None,
      'total_return_pct':round(total,2),'buy_hold_return_pct':round(bh,2),
      'annualized_return_pct':round(((eq.iloc[-1]/initial)**(365.25/days)-1)*100,2) if days>0 and eq.iloc[-1]>0 else None,
      'max_drawdown_pct':round(float(max_dd),2),'open_position':bool(qty),
      'realized_pnl':round(realized,2),'trades':trades[-30:],
      'equity_curve':[{'date':str(t.date()),'equity':round(v,2)} for t,v in eq.iloc[::max(1,len(eq)//100)].items()] + [{'date':str(eq.index[-1].date()),'equity':round(float(eq.iloc[-1]),2)}]}
