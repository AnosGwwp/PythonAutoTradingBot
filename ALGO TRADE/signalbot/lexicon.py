"""Crypto news vocabulary: what a phrase in a headline usually means for price.

Each entry line:   weight | kind | flags | phrase; phrase; ...
  weight   + bullish / - bearish, 0.5 (mild) ... 3 (very strong)
  kind     hack, insolvency, regulation, listing, etf_flows, macro, corporate, adoption, price_move, opinion
  flags    M = major event on its own (hack, delisting, ban, ETF decision, surprise central-bank move...)
           P = only describes a price move that already happened (minor: the market has moved)
  phrase   lower-case regex fragment; a space also matches "-" and line breaks. Matching is whole-word.

How a headline is read (analyze()):
  * the LONGEST phrases win: "lifts ban" beats "ban", "short squeeze" beats "squeeze"
  * NEGATION within the 3 words before a phrase flips it: "denies hack", "no evidence of exploit",
    "fails to rally"; a negated major event is never major
  * HEDGES ("could", "analyst says", "price prediction", a question) cap the severity at minor
  * AMOUNTS: a hack under $1M is not major; flows of $1B+ count extra
  * INTENSIFIERS ("record", "massive", "biggest") strengthen the strongest phrase
"""
from __future__ import annotations

import re
from dataclasses import dataclass

LEXICON = r"""
# ======================================================= security incidents (bearish)
-3   | hack | M | hack; hacks; hacked; hacking; hackers?; exploits?; exploited; exploiters?; drains?; drained; draining;
                  stolen funds; funds stolen; theft; heists?; breach(es)?; breached; compromised; private keys? (leaked|leak|compromised|exposed);
                  rug ?pulls?; rug ?pulled; exit scams?; flash ?loan attacks?; oracle manipulation; reentrancy (attack|bug|exploit);
                  bridge (hack|exploit|attack)s?; 51% attacks?; double ?spend(ing|s)?; infinite mint(ing)?; minting bug;
                  (chain|network|mainnet|blockchain) (halts?|halted|stall(s|ed)?|goes down|went down|outage); halts? block production;
                  stops? producing blocks; consensus (bug|failure|halt); chain split; goes offline; went offline
-2.5 | hack |   | critical (bug|vulnerability|flaw); zero-?day; attackers?; malware; drainer; phishing (attack|scam|campaign)s?;
                  address poisoning; sim ?swap; wallet (drained|compromised|exploit)
-2   | hack |   | outages?; vulnerabilit(y|ies); security (incident|breach|flaw|issue)s?; suspicious (outflows|transactions|activity);
                  attack(s|ed)?; downtime; degraded performance; network congestion; congest(ed|ion)
-1   | hack |   | bugs?; glitch(es)?; delays? in block production
# ======================================================= recovery (bullish relief)
2    | hack |   | recover(s|ed|ing)? (the )?(stolen|lost|hacked) funds; returns? (the )?(stolen|hacked) funds; funds (returned|recovered|restored|safe|are safe|are safu);
                  safu; users? (will be )?(reimbursed|compensated|made whole); reimburs(e|es|ed|ement); compensation (plan|fund); made whole;
                  white ?hat; bounty (accepted|paid); resumes? (withdrawals|deposits|trading|block production); (withdrawals|deposits|trading) (resume|resumed|reopen(ed)?|restored);
                  network (restored|back online|recovers|recovered); back online; patch(es|ed)? (the )?(bug|vulnerability|exploit); (re-?pegs?|re-?pegged|regains? (its )?peg)
0.5  | hack |   | bug bount(y|ies); audit (passed|completed); audited
# ======================================================= solvency, stablecoins, exchanges (bearish, often major)
-3   | insolvency | M | insolven(t|cy); bankrupt(cy)?; files? for bankruptcy; chapter (11|7); bank run; (halts?|suspends?|pauses?|freezes?|freezing|blocks?) (all )?withdrawals;
                  withdrawals? (halted|suspended|paused|frozen|disabled); de-?peg(s|ged|ging)?; loses? (its )?peg; (falls?|slips?|trades?) below (its )?(\$1 )?peg;
                  (exchange|lender|platform|firm|fund) (collapses?|collapsed|implodes?|imploded); liquidity crisis; contagion fears
-2.5 | insolvency |   | contagion; collapse(s|d)?; implod(es|ed|ing); unable to (pay|meet) (withdrawals|obligations|redemptions); missed (payments?|repayment)
-2   | insolvency |   | margin calls?; liquidity (crunch|issues|problems); proof of reserves? (fails?|concerns|questions); redemptions (surge|halted|paused)
# ======================================================= regulation, legal (bearish)
-3   | regulation | M | (sec|cftc|doj|ftc|finra|fca|regulators?|prosecutors?|authorities|government) (sues?|sued|charges?|charged|indicts?|indicted);
                  indict(s|ed|ment); criminal charges; fraud charges; (bans?|banned|banning|outlaws?|outlawed) (crypto|cryptocurrenc(y|ies)|bitcoin|trading|mining|stablecoins?);
                  (china|india|us|u\.s\.|russia|eu|korea|japan|uk|government|regulators?) bans?;
                  (rejects?|rejected|denies|denied|blocks?|blocked) .{0,30}etfs?; etfs? (rejected|denied|blocked); delist(s|ed|ing)?; delisting
-2.5 | regulation |   | ban(s|ned|ning)?; outlaw(s|ed)?; arrest(s|ed)?; crackdown(s)?; cracks down; enforcement actions?; wells notices?; money laundering; laundering;
                  seiz(e|es|ed|ure|ing); confiscat(e|es|ed|ion); fraud(ulent)?; ponzi; scams?; scammers?; terror(ist)? financing
-2   | regulation |   | lawsuits?; sues?; sued; suing; class action; subpoena(s|ed)?; cease[- ]and[- ]desist; unregistered securit(y|ies);
                  (deems?|deemed|classif(y|ies|ied)( as)?|labels?|labeled) (a |an )?securit(y|ies); blacklist(s|ed)?; sanction(s|ed)? (exchange|wallet|addresses|mixer);
                  removes? .{0,25}trading pairs?; (fined|penalty|penalties|fines? of); (freezes?|froze|frozen) (assets|accounts|funds); restricts?; restrictions?;
                  investigat(es|ed|ing|ion|ions); probes?; probed; charges? against
-1.5 | regulation |   | delays? (its |the )?(decision|approval|ruling|review); postpones? (decision|approval|ruling); extends? (its |the )?(review|deadline);
                  crypto tax(es)?; tax(es)? on crypto; capital gains tax (hike|increase); stricter rules; tighter (rules|regulation|oversight);
                  regulatory (uncertainty|scrutiny|pressure|crackdown); warns? (investors|users|of risks); kyc (rules|crackdown); travel rule
# ======================================================= regulation, legal (bullish)
3    | regulation | M | (approves?|approved|greenlights?|green-?lights?|clears?|cleared|authoriz(es|ed)) .{0,30}etfs?; etfs? (approved|approval|greenlit|cleared|authorized);
                  (legaliz(es|ed)|legalize) (bitcoin|crypto|cryptocurrenc(y|ies)); (adopts?|adopted) .{0,20}legal tender;
                  strategic (bitcoin |crypto |digital asset )?reserve; national (bitcoin |crypto )?reserve; (bitcoin|crypto) reserve (bill|law|order)
2.5  | regulation |   | approv(e|es|ed|al|ing); greenlights?; green light; legal tender; legaliz(e|es|ed|ation); wins? (the )?(lawsuit|case|appeal|ruling|court battle);
                  (case|lawsuit|charges) (dismissed|dropped|withdrawn); dismiss(es|ed)? (the )?(case|lawsuit|charges); drops? (the )?(case|lawsuit|charges|investigation|probe);
                  ends? (the )?(probe|investigation|lawsuit|case); clos(es|ed) (the )?(probe|investigation); lifts? (the )?ban; unban(s|ned)?; reverses? (the )?ban;
                  pardons?; pardoned; signed into law; signs? .{0,25}(bill|law)
2    | regulation |   | (secures?|obtains?|receives?|granted|wins?) .{0,15}licen[cs]e; licen[cs]e (granted|approved); registration approved;
                  regulatory (clarity|approval|green light|win); bill pass(es|ed); pass(es|ed)? (the )?.{0,25}(bill|act); pro-?crypto; crypto-?friendly;
                  eases? (rules|restrictions|regulation); relax(es|ed)? (rules|restrictions); safe harbou?r; settles?; settled; settlement
1    | regulation |   | executive order; framework (approved|unveiled|passed); sandbox; clarity act; genius act
2    | regulation |   | clears? .{0,40}of (\$[\d.,]+\w* (\w+ )?)?(fraud|charges|wrongdoing|manipulation|all charges); acquit(s|ted|tal); not guilty;
                  (judge|court|jury) (rules?|ruled|sides?|sided) (for|with|in favou?r of); (case|charges) thrown out
-2   | regulation |   | found guilty; convicted; pleads? guilty; guilty plea; (judge|court|jury) (rules?|ruled|sides?|sided) against
# ======================================================= ETFs, flows, whales, supply (kind etf_flows)
2.5  | etf_flows |   | record (net )?inflows?; (biggest|largest) (daily |weekly )?inflows?; inflows? (surge|jump|soar|top)\w*
-2.5 | etf_flows |   | record (net )?outflows?; (biggest|largest) (daily |weekly )?outflows?; outflows? (surge|jump|soar|top)\w*
2    | etf_flows |   | (etf|etfs|fund|funds) (launch(es|ed)?|debuts?|starts? trading|goes live); launch(es|ed)? .{0,25}etfs?; files? for .{0,30}etfs?; etf filings?;
                  (net )?inflows?; exchange outflows?; (exchange )?reserves? (fall|falls|drop|drops|decline|declines|hits? .{0,10}low)\w*;
                  whales? (buy|buying|bought|accumulat\w*|scoop\w*|add\w*|load\w*); (institutional|institutions?) (buy\w*|demand|adoption|inflows?|interest);
                  accumulat(e|es|ed|ing|ion); adds? .{0,25}to (its |their )?(balance sheet|treasury|reserves?|holdings); (treasury|company|corporate|firm) (buys?|bought|adds?|acquires?);
                  buys? (more )?(bitcoin|btc|ether|eth|solana|sol); token buybacks?; (buys? back|buyback of) .{0,15}tokens; supply shock; short squeeze;
                  shorts? (liquidated|wiped|squeezed|rekt)
-2   | etf_flows |   | (net )?outflows?; exchange inflows?; (exchange )?reserves? (rise|rises|jump|jumps|climb|climbs)\w*; etfs? (lose|loses|lost|bleed|bleeds|bleeding|shed|sheds|see outflows);
                  whales? (sell|selling|sold|dump\w*|offload\w*|unload\w*|move .{0,30}to exchanges?|deposit\w*); sell(s|ing)? (all|its|their|entire|half)\b;
                  sell-?offs?; selling pressure; dumps?; dumped; dumping; offload(s|ed|ing)?; unload(s|ed|ing)?; miners? (sell\w*|dump\w*|capitulat\w*|offload\w*);
                  token unlocks?; unlock (event|schedule); long liquidations?; longs? (liquidated|wiped|rekt); long squeeze; leverage (flush|wipeout); deleverag\w*
-2   | etf_flows |   | (halves?|halved|cuts?|reduces?|reduced|trims?|trimmed|slashes?|slashed|dumps?) .{0,25}(holdings|stake|position|exposure|allocation)
-1.5 | etf_flows |   | sells?; sold; selling; liquidat(ed|ions?); capitulat\w*; distribution; dilution; profit[- ]taking
1.5  | etf_flows |   | buys?; bought; buying; buy the dip; token burns?; burns? .{0,15}tokens; staking (approved|launch\w*|enabled); airdrops?;
                  (raises?|increases?|doubles?|boosts?) .{0,25}(holdings|stake|position|exposure|allocation)
# ======================================================= macro (kind macro): hawkish = bearish for crypto
-3   | macro | M | emergency (rate )?hike; surprise (rate )?hike; (unexpected|shock) (rate )?hike
3    | macro | M | emergency (rate )?cut; surprise (rate )?cut; (unexpected|shock) (rate )?cut
-2   | macro |   | rate hikes?; hikes? (interest )?rates?; rais(e|es|ed|ing) (interest )?rates; hawkish; higher for longer; quantitative tightening; qt;
                  tighten(s|ing)? (policy)?; inflation (rises|rose|jumps|jumped|surges|surged|accelerates|accelerated|heats? up|hotter|hot|sticky|climbs);
                  hotter[- ]than[- ]expected; (cpi|ppi|pce|inflation) (beats|tops|exceeds|above) (forecasts?|expectations?|estimates?);
                  trade war; invasion; invades?; (stock market|stocks|equities|wall street|nasdaq|s&p ?500|dow) (sell-?off|plunges?|plunged|tumbles?|tumbled|crash\w*|sinks?);
                  bank (failure|collapse)s?; banking crisis; (sovereign|debt) default; recession (fears?|risks?|looms?|warning)
-1.5 | macro |   | tariffs?; sanctions?; war; missiles?; airstrikes?; escalat(es|ed|ion|ing); geopolitical (tension|risk|turmoil)s?; risk-?off;
                  yields? (surge|surges|jump|jumps|spike|spikes|soar|soars|rise|rises|climb|climbs); dollar (surges|jumps|strengthens|rallies|soars); dxy (rises|jumps|surges);
                  strong(er)? (jobs|payrolls|labou?r market); recession; government shutdown; debt ceiling; stagflation
2    | macro |   | rate cuts?; cuts? (interest )?rates?; lower(s|ed|ing)? (interest )?rates; dovish; quantitative easing; qe; stimulus; liquidity (injection|boost);
                  eas(e|es|ed|ing) (monetary )?policy; inflation (cools|cooled|eases|eased|falls|fell|slows|slowed|drops|dropped|softens|softened);
                  cooler[- ]than[- ]expected; softer[- ]than[- ]expected; (cpi|ppi|pce|inflation) (below|misses|missed|undershoots) (forecasts?|expectations?|estimates?);
                  trade deal; peace (deal|agreement|accord)
1.5  | macro |   | pivot; ceasefire; cease-?fire; truce; peace talks; tariff (relief|pause|cut|exemption)s?; pauses? tariffs?; risk-?on;
                  yields? (fall|falls|drop|drops|slide|slides|tumble|tumbles|decline|declines); dollar (weakens|slides|falls|tumbles); weak(er)? (jobs|payrolls|labou?r market)
0.5  | macro |   | rate pause; holds? rates (steady|unchanged)
# ======================================================= business, adoption, listings
2.5  | listing |   | lists? .{0,25}(on|at) (coinbase|binance|robinhood|upbit|kraken|okx|bybit); (coinbase|binance|robinhood|upbit|kraken|okx|bybit) (lists?|to list|will list|adds?)
2    | adoption |   | mainnet (launch(es|ed)?|goes live|is live|live); upgrade (goes live|activated|completed|ships|succeeds|successful); hard ?fork (completed|activated|goes live);
                  accepts? (bitcoin|crypto|ether|eth|solana|sol|stablecoins?) (payments?)?; (paypal|visa|mastercard|stripe|blackrock|fidelity|jpmorgan|goldman|vanguard|google|amazon|apple|microsoft|meta) .{0,30}(adds?|launch\w*|integrat\w*|buys?|offers?|allows?|enables?|accepts?);
                  record (users|addresses|transactions|volume|activity|revenue|profit|fees)|all-time high (users|activity|tvl|volume); tvl (hits|surges|rises|jumps|record)
1.5  | adoption |   | partner(s|ed|ing)? with; partnerships?; integrat(es|ed|ion|ions); adopt(s|ed|ion); listing; listed; lists; beats? (earnings|estimates|expectations); revenue (jumps|surges|rises|grows|soars);
                  expands? into; acquir(es|ed|ition); funding round; raises? \$; hashrate (record|all-time high|hits new)
1    | adoption |   | launch(es|ed|ing)?; goes live; upgrades?; upgraded; grows?; growth; growing; profits?; profitable; milestone; integration
-2   | corporate |   | shut(s|ting)? down; shutdowns?; clos(es|ing) (its |the )?(doors|exchange|platform|operations|business); (exits?|exiting|leaves?|pulls? out of) (the )?(market|us|u\.s\.|uk|eu|country);
                  layoffs?; lays? off; job cuts; halts? (operations|trading|services)
-1.5 | corporate |   | suspends?; suspended; halts?; halted; misses? (earnings|estimates|expectations); revenue (falls|drops|declines|slumps|plunges); downgrades?; downgraded;
                  manipulat(e|ed|es|ion); insider trading; exodus; hashrate (drops?|falls?|plunges?|declines?); fears?
-1   | corporate |   | pauses?; paused; delays?; delayed; postpones?; postponed; warns?; warning; concerns?; controvers(y|ial); losses; fud; doubts?; struggles?; uncertainty
# ======================================================= sentiment words (not just price)
1.5  | opinion |   | bullish; bull market; bull run; golden cross; uptrend; optimism; optimistic; euphoria; greed
-1.5 | opinion |   | bearish; bear market; death cross; downtrend; pessimism; pessimistic; panic; fear (index|grips); extreme fear
# ======================================================= price moves that already happened (minor)
-1.5 | price_move | P | plunges?; plunged; plunging; plummets?; plummeted; crash(es|ed|ing)?; nosedives?; nosedived; tanks?; tanked; craters?; cratered; bloodbath; rout;
                  wipes? out; wiped out; breaks? below; (falls?|fell|drops?|dropped|slips?|slipped) below; loses? (key )?support; lowest (level |price )?(since|in);
                  multi-?(week|month|year) lows?; (weekly|monthly|yearly) lows?
-1   | price_move | P | falls?; fell; falling; drops?; dropped; dropping; slides?; slid; sliding; slumps?; slumped; sinks?; sank; sunk; tumbles?; tumbled; dips?; dipped;
                  retreats?; retreated; slips?; slipped; declines?; declined; declining; loses?; lost; losing; sheds?; weakens?; weakened; wobbles?; correction; corrects; lower
1.5  | price_move | P | surges?; surged; surging; soars?; soared; soaring; skyrockets?; skyrocketed; rockets?; rocketed; moons?; mooning; pumps?; pumped; spikes?; spiked; explodes?; exploded;
                  breaks? above; breaks? out; breakout; reclaims?; reclaimed; (hits?|reaches?|sets?|notches?) (a )?(new )?(record|all-time high|ath); record highs?; all-time highs?; ath;
                  highest (level |price )?(since|in); multi-?(week|month|year) highs?
1    | price_move | P | rises?; rose; rising; gains?; gained; gaining; climbs?; climbed; climbing; jumps?; jumped; jumping; rall(y|ies|ied|ying); rebounds?; rebounded;
                  recovers?; recovered; recovering; bounces?; bounced; edges? (up|higher); higher; advances?; advanced; strengthens?; tops \$
"""

# Words that make a headline less than news (severity capped at minor):
# a guess or an opinion piece (always) ...
GUESS = re.compile(
    r"\?\s*$|\b(?:could|might|may|would|likely|unlikely|possibl[ey]|potential(?:ly)?|expected to|set to|poised to|eyes?|eyeing|mulls?|"
    r"considers?|considering|weighs?|weighing|plans? to|proposes?|proposed|proposal|predicts?|predicted|predictions?|forecasts?|"
    r"outlook|thinks?|believes?|argues?|opinion|op-?ed|editorial|explainer|guide|how to|what is|what are|why|here's|here is|"
    r"podcast|newsletter|recap|roundup|week in review|weekly|live updates?|live blog|price (?:prediction|analysis|forecast)|"
    r"technical analysis|sponsored|press release|partner content|interview|rumou?rs?|rumou?red|reportedly|unconfirmed|"
    r"speculat\w*|if)\b", re.I)
# ... aftermath: a court case or anniversary about something that already happened (always) ...
AFTERMATH = re.compile(r"\b(?:sentenced|sentencing|jailed|prison|trial|testif\w*|court hears|anniversary|years? ago|"
                       r"looking back|retrospective|lessons from)\b", re.I)
# ... commentary and data analysis (only when the headline has no hard fact of its own).
COMMENTARY = re.compile(
    r"\b(?:analysts?|analysis|strategists?|historically|history (?:says|shows|suggests)|indicators?|metrics?|charts?|"
    r"on-?chain (?:data|analysis|metrics)|data (?:shows|suggests)|(?:value|buy|sell|danger) zone|phase|"
    r"cryptoquant|glassnode|santiment|kaiko|coinglass|matrixport|10x research)\b", re.I)
FACT_KINDS = {"hack", "insolvency", "regulation", "listing", "etf_flows", "macro"}
NEGATORS = re.compile(
    r"\b(?:no|not|never|none|denies|denied|deny|denying|false|fake|debunk\w*|without|avoid\w*|prevent\w*|thwart\w*|foil\w*|"
    r"unaffected|isn't|wasn't|won't|doesn't|didn't|aren't|weren't|cannot|can't|fails? to|failed to|unlikely to|rules? out|"
    r"no evidence of|despite)\s+(?:\S+\s+){0,2}$", re.I)
INTENSIFIERS = re.compile(r"\b(?:record|massive|huge|biggest|largest|historic|unprecedented|sweeping|mega|giant|"
                          r"major|billions?)\b", re.I)
AMOUNT = re.compile(r"\$\s?(\d+(?:[.,]\d+)?)\s*(k|thousand|m|mn|million|b|bn|billion)\b", re.I)
# Subjects that make a story about the whole crypto market (not one coin or one company).
MARKET_TERMS = re.compile(
    r"\b(?:crypto(?:currency)? markets?|cryptocurrencies|crypto(?:currency)? industry|crypto prices|digital asset markets?|"
    r"altcoins|altcoin market|stablecoins?|tether|usdt|usdc|crypto exchanges|crypto (?:sector|space|traders|investors)|"
    r"total crypto market cap|crypto market cap|crypto(?: market)? (?:crash|rally|sell-?off|rout)|all crypto|crypto ban|"
    r"crypto (?:regulation|bill|law|tax))\b", re.I)


@dataclass(frozen=True)
class Entry:
    pattern: re.Pattern
    weight: float
    kind: str
    major: bool
    price_only: bool
    phrase: str


HEADER = re.compile(r"^([-+]?\d+(?:\.\d+)?)\s*\|\s*(\w+)\s*\|\s*([A-Z]*)\s*\|(.*)$")


def _compile() -> list[Entry]:
    entries, current = [], None
    for line in LEXICON.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        h = HEADER.match(line)
        if h:                                      # "weight | kind | flags | phrases"
            current = (float(h.group(1)), h.group(2), "M" in h.group(3), "P" in h.group(3))
            phrases = h.group(4)
        else:
            phrases = line                         # continuation of the previous entry line
        for ph in phrases.split(";"):
            ph = ph.strip()
            if not ph:
                continue
            rx = re.sub(r"(?<!\\) ", r"[\\s-]+", ph)
            tail = "" if ph.endswith("\\$") else r"(?![a-z0-9])"   # "raises $" must match "raises $50M"
            entries.append(Entry(re.compile(rf"(?<![a-z0-9])(?:{rx}){tail}", re.I), *current, ph))
    return entries


ENTRIES = _compile()


def _amount_usd(text: str) -> float | None:
    best = None
    for num, unit in AMOUNT.findall(text):
        v = float(num.replace(",", "")) * {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6,
                                             "b": 1e9, "bn": 1e9, "billion": 1e9}[unit.lower()]
        best = max(best or 0.0, v)
    return best


@dataclass
class Reading:
    score: float                 # sum of weights: + bullish, - bearish
    strength: float              # strongest single (non-price) phrase, absolute
    major: bool
    price_only: bool             # every phrase found only describes a past price move
    hedged: bool
    kind: str
    phrases: list[str]           # what was recognised, for the explanation
    amount_usd: float | None


def analyze(text: str) -> Reading:
    """Read one headline (or summary)."""
    found = []                                     # (start, end, entry)
    for e in ENTRIES:
        for m in e.pattern.finditer(text):
            found.append((m.start(), m.end(), e))
    found.sort(key=lambda x: (-(x[1] - x[0]), x[0]))
    taken: list[tuple[int, int, Entry, bool]] = []
    for s, t, e in found:                          # longest first, no overlaps
        if any(s < t2 and s2 < t for s2, t2, _, _ in taken):
            continue
        clause = re.split(r"[,;:.!?|]", text[max(0, s - 60):s])[-1]
        taken.append((s, t, e, bool(NEGATORS.search(clause))))
    amount = _amount_usd(text)
    score, strength, major, kinds, phrases = 0.0, 0.0, False, {}, []
    for s, t, e, negated in sorted(taken, key=lambda x: x[0]):
        w = -e.weight * 0.5 if negated else e.weight
        if e.kind == "hack" and e.major and not negated and amount is not None and amount < 1e6:
            w = w * 0.6                            # small incident
        score += w
        if not e.price_only:
            strength = max(strength, abs(w))
        if e.major and not negated and not (e.kind == "hack" and amount is not None and amount < 1e6):
            major = True
        kinds[e.kind] = kinds.get(e.kind, 0.0) + abs(w)
        phrases.append(("not " if negated else "") + text[s:t].lower())
    if INTENSIFIERS.search(text) and strength:
        strength, score = strength * 1.3, score * 1.2
    if amount and amount >= 1e9 and kinds.get("etf_flows"):
        strength = max(strength, 2.5)
    price_only = bool(taken) and all(e.price_only for _, _, e, _ in taken)
    kind = max(kinds, key=kinds.get) if kinds else "other"
    fact = any(e.kind in FACT_KINDS and not e.price_only and abs(e.weight) >= 2 and not neg for _, _, e, neg in taken)
    hedged = bool(GUESS.search(text) or AFTERMATH.search(text) or (COMMENTARY.search(text) and not fact))
    return Reading(score, strength, major, price_only, hedged, kind, phrases, amount)


def size() -> int:
    return len(ENTRIES)
