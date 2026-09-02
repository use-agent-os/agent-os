#!/usr/bin/env python3
import json, math, subprocess, sys

WALLET       = sys.argv[1]
CHAIN        = sys.argv[2]
LANG         = sys.argv[3] if len(sys.argv) > 3 else 'zh'
LATENCY_S    = float(sys.argv[4]) if len(sys.argv) > 4 else 3.0    # entry lag, seconds
SLIPPAGE_PCT = float(sys.argv[5]) if len(sys.argv) > 5 else 0.05   # one-sided, 0.05 = 5%
GAS_USD      = float(sys.argv[6]) if len(sys.argv) > 6 else 0.2    # your cost per trade
SAMPLE       = int(sys.argv[7])   if len(sys.argv) > 7 else 200    # activity rows, max 400

ZH = (LANG == 'zh')
def _(zh, en): return zh if ZH else en

def run_cli(args, timeout=30):
    r = subprocess.run(['gmgn-cli'] + args + ['--raw'], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr)
    return json.loads(r.stdout)

def unwrap(resp):
    # gmgn-cli --raw already unwraps to the data object; tolerate either shape.
    return resp.get('data', resp) if isinstance(resp, dict) and 'data' in resp else resp

def _f(v, default=0.0):
    try: return float(v)
    except (TypeError, ValueError): return default

def _clamp(x, lo=0.0, hi=1.0):
    return lo if x < lo else hi if x > hi else x

def _b(v):
    if isinstance(v, bool): return v
    if isinstance(v, (int, float)): return v != 0
    if isinstance(v, str): return v.strip().lower() in ('1', 'true', 'yes')
    return False

def _or_default(x, default):
    # `x or default` also replaces a legitimate 0.0 with `default`, which is
    # wrong when 0.0 is a real, meaningful value (e.g. a wallet at exactly
    # break-even ROI) rather than missing data. Only None is "missing" here.
    return default if x is None else x

def fmt_dur(sec):
    sec = _f(sec)
    if sec < 60:    return f"{int(sec)}{_(' 秒','s')}"
    if sec < 3600:  return f"{round(sec/60)}{_(' 分','m')}"
    if sec < 86400: return f"{round(sec/3600,1)}{_(' 小时','h')}"
    return f"{round(sec/86400,1)}{_(' 天','d')}"

def usd(v):
    v = _f(v)
    if abs(v) >= 1_000_000: return f"${v/1_000_000:.2f}M"
    if abs(v) >= 1_000:     return f"${v/1_000:.1f}K"
    return f"${v:.2f}"

# ── 1. Trading stats (7D) ─────────────────────────────────
stats_raw = unwrap(run_cli(['portfolio', 'stats', '--chain', CHAIN, '--wallet', WALLET, '--period', '7d']))
pnl    = stats_raw.get('pnl_stat') or {}
common = stats_raw.get('common') or {}

buy  = int(_f(stats_raw.get('buy', stats_raw.get('buy_count'))))
sell = int(_f(stats_raw.get('sell', stats_raw.get('sell_count'))))
trades = buy + sell
token_num = int(_f(pnl.get('token_num')))
dist = dict(
    gt_5   = int(_f(pnl.get('pnl_gt_5x_num'))),
    x2_5   = int(_f(pnl.get('pnl_2x_5x_num'))),
    x0_2   = int(_f(pnl.get('pnl_0x_2x_num'))),
    n50_0  = int(_f(pnl.get('pnl_nd5_0x_num'))),
    lt_n50 = int(_f(pnl.get('pnl_lt_nd5_num'))),
)
realized_profit = _f(stats_raw.get('realized_profit'))
bought_cost      = _f(stats_raw.get('bought_cost', stats_raw.get('total_cost')))
created_token_count = int(_f(common.get('created_token_count')))

w = dict(
    realized_profit=realized_profit,
    roi=_f(stats_raw.get('realized_profit_pnl', stats_raw.get('pnl'))),
    buy=buy, sell=sell, trades=trades,
    bought_cost=bought_cost,
    avg_buy_usd=(bought_cost / buy if buy else 0.0),
    avg_trade_usd=(realized_profit / sell if sell else 0.0),
    token_num=token_num, winrate=_f(pnl.get('winrate')), dist=dist,
    avg_hold_s=_f(pnl.get('avg_holding_period')),
    created_token_count=created_token_count,
    identity=common.get('twitter_name') or common.get('name') or common.get('ens') or '',
)

if trades == 0:
    print(_( "该地址近 7 天没有真实买卖记录（可能是新钱包，或持有的代币是转入/空投所得）——无法评估战绩，跳过打分。",
             "This address has no real buy/sell activity in the last 7 days (may be a new wallet, or its holdings were transferred/airdropped in) — not enough data to score, skipping."))
    raise SystemExit(0)

# ── 2. Activity sample (paginated, most-recent-first) ─────
def sample_activity(target):
    target = max(20, min(int(target or 200), 400))
    acts, cursor = [], None
    for _try in range(4):
        args = ['portfolio', 'activity', '--chain', CHAIN, '--wallet', WALLET,
                '--limit', str(min(100, target - len(acts)))]
        if cursor: args += ['--cursor', str(cursor)]
        raw = unwrap(run_cli(args))
        page = raw.get('activities') or []
        acts.extend(page)
        cursor = raw.get('next')
        if not cursor or not page or len(acts) >= target:
            break
    return acts[:target]

acts = sample_activity(SAMPLE)

def ev_type(a):
    return (a.get('event_type') or a.get('type') or '').lower()

mcaps = []
for a in acts:
    if ev_type(a) != 'buy':
        continue
    tok = a.get('token') or {}
    supply = _f(tok.get('total_supply'))
    px = _f(a.get('price_usd'))
    if supply > 0 and px > 0:
        mcaps.append(px * supply)
mcaps.sort()
has_mcap_data = bool(mcaps)
entry_under_100k  = (sum(1 for m in mcaps if m < 100_000) / len(mcaps)) if mcaps else 0.0
median_entry_mcap = mcaps[len(mcaps) // 2] if mcaps else 0.0

by_tok = {}
for a in acts:
    addr = (a.get('token') or {}).get('address')
    by_tok.setdefault(addr, []).append(a)
pairs = fast = 0
for evs in by_tok.values():
    evs = sorted(evs, key=lambda e: _f(e.get('timestamp')))
    last_buy = None
    for e in evs:
        et = ev_type(e)
        if et == 'buy':
            last_buy = _f(e.get('timestamp'))
        elif et == 'sell' and last_buy is not None:
            pairs += 1
            if _f(e.get('timestamp')) - last_buy <= 5:
                fast += 1
            last_buy = None
fast_flip_rate = round(fast / pairs, 4) if pairs else 0.0

gas_vals = [_f(a.get('gas_usd')) for a in acts if _f(a.get('gas_usd')) > 0]
has_gas_data = bool(gas_vals)
avg_gas_usd = round(sum(gas_vals) / len(gas_vals), 4) if gas_vals else 0.0

summ = dict(sampled=len(acts), entry_under_100k=round(entry_under_100k, 4),
            median_entry_mcap=round(median_entry_mcap, 2),
            fast_flip_rate=fast_flip_rate, avg_gas_usd=avg_gas_usd)

# ── 3. Dev-reputation (only if this wallet mostly launches, not trades) ──
DEV_SEC_SCAN_N = 3     # how many of its most recent launches to security-scan
is_dev_wallet = created_token_count > 0 and created_token_count > 0.5 * max(1, token_num)

dev = None
if is_dev_wallet:
    try:
        ct = unwrap(run_cli(['portfolio', 'created-tokens', '--chain', CHAIN, '--wallet', WALLET]))
        toks = ct.get('tokens') or []
        open_count  = int(_f(ct.get('open_count')))
        inner_count = int(_f(ct.get('inner_count')))
        if toks or open_count or inner_count:
            # alive = graduated to DEX AND still has real liquidity (not drained)
            alive = sum(1 for t in toks if t.get('is_open') and _f(t.get('pool_liquidity')) >= 4000)
            total = len(toks)
            rug_rate = round((total - alive) / total, 3) if total else 0.0
            ath_mc = _f((ct.get('creator_ath_info') or {}).get('ath_mc'))

            recent = sorted(toks, key=lambda t: -_f(t.get('create_timestamp')))[:DEV_SEC_SCAN_N]
            checked = unsafe = 0
            for t in recent:
                addr = t.get('token_address')
                if not addr:
                    continue
                try:
                    sec = unwrap(run_cli(['token', 'security', '--chain', CHAIN, '--address', addr]))
                except Exception:
                    continue
                checked += 1
                bad = False
                if str(sec.get('is_honeypot', '')).lower() == 'yes':
                    bad = True
                elif CHAIN == 'sol':
                    if not _b(sec.get('renounced_mint')) or not _b(sec.get('renounced_freeze_account')):
                        bad = True
                else:
                    if str(sec.get('open_source', '')).lower() == 'no':
                        bad = True
                if bad:
                    unsafe += 1
            sec_risk_rate = round(unsafe / checked, 3) if checked else 0.0

            surv = (1.0 - rug_rate) if total else _clamp(_f(ct.get('open_ratio')))
            ath_track = _clamp((math.log10(max(1.0, ath_mc)) - 5.0) / 2.0)   # $100k→0, $10M→1
            s = 0.25 + 0.55 * surv
            s -= 0.30 * _clamp((inner_count - 50) / 950.0)   # heavy bonding-curve pileup = factory
            s += 0.15 * ath_track * surv                     # best-ever launch, gated by survival
            s -= 0.35 * sec_risk_rate                        # recent launches failed a security check
            dev = dict(open_count=open_count, inner_count=inner_count,
                       analyzed=total, alive=alive, rugged=max(0, total - alive),
                       rug_rate=rug_rate, ath_mc=ath_mc,
                       sec_checked=checked, sec_unsafe=unsafe, sec_risk_rate=sec_risk_rate,
                       score=round(_clamp(s), 3))
    except Exception:
        dev = None

# ── 4. Style tags ──────────────────────────────────────────
tags = []
def add_tag(emoji, zh, en):
    tags.append(dict(emoji=emoji, text=_(zh, en)))

tn = max(1, token_num)
big_win  = (dist['gt_5'] + dist['x2_5']) / tn
big_loss = dist['lt_n50'] / tn
early, flip = summ['entry_under_100k'], summ['fast_flip_rate']

if dev is not None:
    pct_created = round(created_token_count / tn * 100)
    cnt = created_token_count or dev.get('analyzed', 0)
    rug = dev.get('rug_rate', 0.0)
    if rug >= 0.5:
        add_tag("🏭", f"发币方/Dev：发过 {cnt} 个币（占交易{min(pct_created,100)}%+），rug 率 {round(rug*100)}%（工厂号嫌疑）",
                      f"Token creator/Dev: launched {cnt} tokens (≥{min(pct_created,100)}% of traded tokens), rug rate {round(rug*100)}% (factory suspect)")
    else:
        add_tag("🏭", f"发币方/Dev：发过 {cnt} 个币（占交易{min(pct_created,100)}%+），看 Dev 信誉分",
                      f"Token creator/Dev: launched {cnt} tokens (≥{min(pct_created,100)}% of traded tokens) — check the Dev-reputation score")
    add_tag("🏗️", f"自产自销：交易的币里 {min(pct_created,100)}%+ 是自己发的——进场时机/胜率对这类币没意义",
                  f"Self-dealer: ≥{min(pct_created,100)}% of traded tokens are its own launches — entry-timing/win-rate tags are meaningless here")
if trades >= 2000:
    add_tag("🤖", f"机器人/科学家：7D {trades} 笔，只能机器跟", f"Bot/Quant: {trades} trades in 7D — only a bot can keep up")
if flip >= 0.3:
    add_tag("⚡", f"闪电手：{round(flip*100)}% 的仓位 5 秒内买卖", f"Flash flipper: {round(flip*100)}% of positions bought & sold within 5s")
if dev is None and early >= 0.8:
    add_tag("🎯", f"狙击手：{round(early*100)}% 进场市值 <$100k", f"Sniper: {round(early*100)}% of entries are <$100k mcap")
if w['avg_hold_s'] >= 5*86400 and trades < 200:
    add_tag("💎", "钻石手：持仓久、下手少", "Diamond hands: holds long, trades rarely")
if w['avg_buy_usd'] >= 5000:
    add_tag("🐋", f"巨鲸：单笔平均建仓 {usd(w['avg_buy_usd'])}", f"Whale: avg position size {usd(w['avg_buy_usd'])}")
if dev is None and w['winrate'] >= 0.65 and trades >= 15:
    add_tag("🏆", f"高胜率：{round(w['winrate']*100)}% 的币最终是赚的", f"High win-rate: {round(w['winrate']*100)}% of tokens ended up profitable")
if 0 < w['avg_hold_s'] < 3600 and flip < 0.3 and trades >= 30:
    add_tag("🐇", f"快枪手：平均持仓 {fmt_dur(w['avg_hold_s'])}", f"Quick-draw: avg hold {fmt_dur(w['avg_hold_s'])}")
if dev is None and 0 < summ['median_entry_mcap'] < 30000:
    add_tag("🔦", f"冷门捡漏：中位进场市值仅 {usd(summ['median_entry_mcap'])}", f"Obscure hunter: median entry mcap only {usd(summ['median_entry_mcap'])}")
if w['realized_profit'] > 20000 and big_loss <= 0.05:
    add_tag("📈", "真高手：净赚且极少大亏，止损纪律好", "True skill: net profitable with very few big losses")
elif big_loss >= 0.3 and w['realized_profit'] < 0:
    add_tag("🩸", f"亏损韭菜：{round(big_loss*100)}% 的币亏超 50%，长期净亏", f"Bag holder: {round(big_loss*100)}% of tokens lost over 50%")
elif big_win >= 0.02 and w['winrate'] < 0.35 and w['realized_profit'] > 0:
    add_tag("🎰", "赌狗打法：胜率低但靠少数暴击回本", "Gambler: low win-rate, carried by a few huge hits")
if trades < 60 and w['realized_profit'] > 0 and flip < 0.1:
    add_tag("🐌", "慢工出细活：低频、可复制，最适合跟单", "Slow & steady: low frequency, repeatable — easiest to copy")
if not tags:
    add_tag("🧭", "普通交易者：没有特别突出的风格标签", "Regular trader: no standout style tags")

# ── 5. Track-record score (is this trader actually good?) ──
TRACK_W = dict(tail=0.34, upside=0.28, roi=0.16, win=0.10, size=0.12)
tail_f    = 1 - dist['lt_n50'] / tn
upside_f  = (dist['gt_5'] + dist['x2_5'] + dist['x0_2']) / tn
roi_f     = _clamp((w['roi'] + 0.05) / 0.35)
win_f     = _clamp(w['winrate'] / 0.5)
size_f    = _clamp((tn - 20) / 300)
track_facs = dict(tail=tail_f, upside=upside_f, roi=roi_f, win=win_f, size=size_f)
track_score = round(100 * sum(TRACK_W[k] * _clamp(v) for k, v in track_facs.items()))
TRACK_LABELS = dict(tail=_('止损纪律','Stop-loss discipline'), upside=_('盈利面','Profit share'),
                     roi=_('资金回报','Capital ROI'), win=_('胜率','Win rate'), size=_('样本量','Sample size'))

# ── 6. Copy-tradeability score (can YOU capture it?) ────────
COPY_W = dict(entry=0.22, profit=0.22, hold=0.20, feasible=0.18, edge=0.18)
entry_f    = _clamp(0.12 + (1 - summ['entry_under_100k']))
profit_f   = _clamp(w['avg_trade_usd'] / 80.0)
hold_f     = _clamp((1 - summ['fast_flip_rate'] * 1.6) * _clamp(w['avg_hold_s'] / 172800 + 0.15))
feasible_f = _clamp(1 - w['trades'] / 2500.0)
edge_f     = _clamp(1 - 0.6 * summ['entry_under_100k'] - 0.6 * summ['fast_flip_rate'])
copy_facs = dict(entry=entry_f, profit=profit_f, hold=hold_f, feasible=feasible_f, edge=edge_f)
copy_score = round(100 * sum(COPY_W[k] * v for k, v in copy_facs.items()))
COPY_LABELS = dict(entry=_('进场市值','Entry mcap'), profit=_('单笔利润空间','Profit per trade'),
                    hold=_('持仓 vs 延迟','Hold vs latency'), feasible=_('执行可行性','Execution feasibility'),
                    edge=_('优势类型','Edge type'))

# Self-dealing discount: for a dev wallet, entry-timing/win-rate factors are self-authored — steeply
# discount both display scores (not hidden, just flagged) rather than let them look falsely strong.
SELF_DEAL_DISCOUNT = 0.45
self_dealing = dev is not None
track_disp = round(track_score * SELF_DEAL_DISCOUNT) if self_dealing else track_score
copy_disp  = round(copy_score  * SELF_DEAL_DISCOUNT) if self_dealing else copy_score

# ── 7. Copy-trade backtest ───────────────────────────────────
wallet_pct = (w['realized_profit'] / w['bought_cost']) if w['bought_cost'] > 0 else w['roi']
wallet_pct = _clamp(_or_default(wallet_pct, 0.0001), -0.9, 3.0)   # clamp: dev wallets have near-zero bought_cost, ratio blows up
LOW_MCAP_DRIFT_PER_S = 0.015
drift_per_s = LOW_MCAP_DRIFT_PER_S * (0.3 + 0.7 * summ['entry_under_100k'])
drift = LATENCY_S * drift_per_s
slip = 2 * SLIPPAGE_PCT
gas_pct = (GAS_USD / w['avg_buy_usd']) if w['avg_buy_usd'] > 0 else 0.0
copy_pct = wallet_pct - drift - slip - gas_pct
copy_7d = w['realized_profit'] * (copy_pct / wallet_pct) if wallet_pct else 0.0
bt = dict(wallet_pct=round(wallet_pct,4), copy_pct=round(copy_pct,4), drift=round(drift,4),
          slip=round(slip,4), gas_pct=round(gas_pct,4), wallet_7d=round(w['realized_profit'],1),
          copy_7d=round(copy_7d,1), trap=round(w['realized_profit']-copy_7d,1))

# ── 8. Verdict ────────────────────────────────────────────────
ts, cs = track_disp, copy_disp
if dev is not None:
    ds = round((dev.get('score') or 0) * 100)
    if ds < 40:
        v_emoji, v_text = "🔴", _( f"发币方钱包，Dev 信誉仅 {ds}/100（连环 rug / 存活率低）—— 别碰它发的新盘。",
                                    f"Token-creator wallet, Dev reputation only {ds}/100 (serial-rug / low survival) — stay away from its new launches.")
    else:
        v_emoji, v_text = "🟡", _( f"发币方钱包，Dev 信誉 {ds}/100 —— 看它的存活率与安全记录再决定是否跟它的新盘。",
                                    f"Token-creator wallet, Dev reputation {ds}/100 — check survival rate and security record before following its new launches.")
elif ts >= 65 and cs < 35:
    v_emoji, v_text = "⚠️", _( "高战绩、低可跟单 —— 学它的止损纪律，别抄它的入场；延迟和滑点会把薄利吃成负。",
                                "High track record, low copy-tradeability — learn the stop-loss discipline, don't copy the entries; latency and slippage will turn thin profit negative.")
elif ts >= 60 and cs >= 55:
    v_emoji, v_text = "🟢", _( "战绩真实且可跟单性高 —— 低频、进场不算太早，值得小额跟一跟验证。",
                                "Genuine track record and high copy-tradeability — low frequency, entries not too early, worth a small copy-trade to verify.")
elif ts < 40:
    v_emoji, v_text = "🔴", _("战绩一般偏弱 —— 不建议作为跟单对象。", "Weak track record — not recommended as a copy-trade target.")
else:
    v_emoji, v_text = "🟡", _( "战绩中等 —— 可观察，跟单前先小额验证延迟/滑点损耗。",
                                "Middling track record — worth watching; verify latency/slippage cost with a small trade before copying.")

# ══════════════════════════════════════════════════════════
# OUTPUT
# ══════════════════════════════════════════════════════════
title = _("跟单评分", "Copy-Trade Score")
print(f"┌{'─'*56}┐")
print(f"│{('  '+title):^56}│")
print(f"│{('  '+WALLET[:6]+'...'+WALLET[-4:]+'  ·  '+CHAIN.upper()):^56}│")
if w['identity']:
    print(f"│{('  '+w['identity']):^56}│")
print(f"└{'─'*56}┘")
print()

sec = _("📊 近7天战绩", "📊 7D Trading Stats")
print(f"━━  {sec}  {'━'*(54-len(sec))}")
print(f"  {_('已实现盈亏','Realized P&L')} {usd(w['realized_profit'])}   ROI {w['roi']*100:+.1f}%   "
      f"{_('胜率','Win rate')} {w['winrate']*100:.0f}%   {_('笔数','Trades')} {trades} ({buy}{_('买','buy')}/{sell}{_('卖','sell')})")
print(f"  {_('交易币数','Tokens traded')} {token_num}   {_('均持仓','Avg hold')} {fmt_dur(w['avg_hold_s'])}   "
      f"{_('均单笔建仓','Avg position')} {usd(w['avg_buy_usd'])}")
print()

sec = _("🏷️ 风格标签", "🏷️ Style Tags")
print(f"━━  {sec}  {'━'*(54-len(sec))}")
for t in tags:
    print(f"  {t['emoji']}  {t['text']}")
print()

if self_dealing:
    print(f"  ⚠️ {_('该钱包主要在发币而非交易——下面两个分已按 ×0.45 打折显示，仅供参考。','This wallet mostly launches tokens rather than trading — the two scores below are shown at a ×0.45 discount for reference only.')}")
    print()

sec = _("🎯 真实战绩分（这交易员是不是真有本事）", "🎯 Track-Record Score (is this trader actually good?)")
print(f"━━  {sec}  {'━'*(max(0,54-len(sec)))}")
print(f"  {track_disp}/100")
for k, v in track_facs.items():
    print(f"    · {TRACK_LABELS[k]:14s}  {round(100*_clamp(v)):3d}/100   ({_('权重','w')} {TRACK_W[k]:.0%})")
print()

sec = _("🚀 可跟单分（你跟进后能拿到多少）", "🚀 Copy-Tradeability Score (can YOU capture it?)")
print(f"━━  {sec}  {'━'*(max(0,54-len(sec)))}")
print(f"  {copy_disp}/100")
for k, v in copy_facs.items():
    print(f"    · {COPY_LABELS[k]:14s}  {round(100*v):3d}/100   ({_('权重','w')} {COPY_W[k]:.0%})")
print()

sec = _("🧮 跟单回测", "🧮 Copy-Trade Backtest")
print(f"━━  {sec}  {'━'*(max(0,54-len(sec)))}")
print(f"  {_('假设','Assuming')}: {_('延迟','latency')} {LATENCY_S:.1f}s   {_('单边滑点','one-sided slippage')} {SLIPPAGE_PCT:.1%}   {_('每笔gas','gas/trade')} {usd(GAS_USD)}")
print(f"  {_('钱包本人单笔收益率','Wallet per-trade return')}  {bt['wallet_pct']*100:+.1f}%")
print(f"  {_('- 延迟漂移','- latency drift')}  {bt['drift']*100:.2f}pp    {_('- 双边滑点','- round-trip slippage')}  {bt['slip']*100:.2f}pp    {_('- gas占比','- gas cost')}  {bt['gas_pct']*100:.2f}pp")
print(f"  {_('= 跟单后单笔收益率','= your per-trade return')}  {bt['copy_pct']*100:+.1f}%")
print()
print(f"  7D  {_('钱包本人','wallet')} {usd(bt['wallet_7d'])}  →  {_('跟单预估','copy estimate')} {usd(bt['copy_7d'])}   ({_('抄单损耗','execution drag')} {usd(bt['trap'])})")
if not has_mcap_data:
    print(f"  ⚠️ {_('未取到进场市值数据，延迟漂移按中性假设估算，可能失真','Entry mcap data unavailable — latency drift uses a neutral assumption and may be inaccurate')}")
print()

if dev is not None:
    sec = _("👨‍💻 Dev 信誉分（该钱包作为发币方）", "👨‍💻 Dev Reputation (as a token creator)")
    print(f"━━  {sec}  {'━'*(max(0,54-len(sec)))}")
    ds = round((dev.get('score') or 0) * 100)
    print(f"  {ds}/100")
    print(f"  {_('已开外盘','Graduated')} {dev['open_count']}   {_('卡在内盘','Stuck on curve')} {dev['inner_count']}   "
          f"{_('抽样存活率','Sampled survival')} {round((1-dev['rug_rate'])*100)}% ({dev['alive']}/{dev['analyzed']})")
    print(f"  {_('历史最高市值','All-time-high mcap')} {usd(dev['ath_mc'])}")
    if dev['sec_checked']:
        print(f"  {_('最近发币安全扫描','Recent launches security scan')}: {dev['sec_unsafe']}/{dev['sec_checked']} {_('未过检','failed check')}")
    print()

sec = _("✅ 结论", "✅ Verdict")
print(f"━━  {sec}  {'━'*(max(0,54-len(sec)))}")
print(f"  {v_emoji}  {v_text}")
