"""Bounded on-chain ownership checks. Unknown evidence never authorizes an entry."""
import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from collections import defaultdict

from .engine import canonical
from .feed import MINT

TOKEN = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
TOKEN22 = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
PUMP = '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'
AMM = 'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA'
ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


def decode58(value):
    n = 0
    for c in value:
        n = n*58 + ALPHABET.index(c)
    return b'\0'*(len(value)-len(value.lstrip('1'))) + n.to_bytes((n.bit_length()+7)//8,'big')


def encode58(value):
    n, out = int.from_bytes(value,'big'), ''
    while n:
        n,r = divmod(n,58)
        out = ALPHABET[r]+out
    return '1'*(len(value)-len(value.lstrip(b'\0')))+out


def curve_address(mint):
    # Solana rejects *any* decompressible Edwards point, including non-prime-order points.
    prime = 2**255-19
    d = -121665*pow(121666,prime-2,prime)%prime
    for bump in range(255,-1,-1):
        raw = hashlib.sha256(b'bonding-curve'+decode58(mint)+bytes([bump])+decode58(PUMP)+b'ProgramDerivedAddress').digest()
        y = int.from_bytes(raw,'little') & (2**255-1)
        x2 = (y*y-1)*pow(d*y*y+1,prime-2,prime)%prime
        on_curve = y < prime and (x2 == 0 or pow(x2,(prime-1)//2,prime)==1)
        if not on_curve:
            return encode58(raw)
    raise ValueError('No curve PDA')


class EvidenceUnavailable(Exception):
    pass


class Rpc:
    def __init__(self, config):
        self.url = os.environ.get(config['rpc_env']) or 'https://api.mainnet-beta.solana.com'
        if not self.url.startswith('https://'):
            raise ValueError('Ownership RPC must use HTTPS')
        self.spacing = config['request_spacing_seconds']
        self.last = 0

    def __call__(self, method, params):
        if method not in ('getAccountInfo','getProgramAccounts','getMultipleAccounts','getSignaturesForAddress','getTransaction'):
            raise ValueError('Read-only RPC method required')
        time.sleep(max(0,self.spacing-(time.monotonic()-self.last)))
        self.last = time.monotonic()
        req = urllib.request.Request(self.url,data=json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':params}).encode(),headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req,timeout=10) as response:
                body = response.read(8_000_001)
                if len(body)>8_000_000:
                    raise EvidenceUnavailable('Ownership response exceeded the scan limit')
                result = json.loads(body)
            if result.get('error'):
                code = result['error'].get('code')
                raise EvidenceUnavailable('Ownership RPC rate limited' if code in (429,-32005) else 'Ownership RPC could not supply the requested evidence')
            return result['result']
        except urllib.error.HTTPError as e:
            raise EvidenceUnavailable(f'Ownership RPC unavailable (HTTP {e.code})') from None
        except (urllib.error.URLError,TimeoutError,ValueError,KeyError,OSError):
            raise EvidenceUnavailable('Ownership RPC unavailable or returned incomplete data') from None


def verified_vaults(mint, accounts, owner_accounts):
    curve = curve_address(mint)
    excluded = set()
    for token in accounts:
        info = token['account']['data']['parsed']['info']
        owner, address = info['owner'], token['pubkey']
        account = owner_accounts.get(owner) or {}
        if owner == curve and account.get('owner') == PUMP:
            excluded.add(address)
        elif account.get('owner') == AMM:
            try:
                raw = base64.b64decode(account['data'][0],validate=True)
                if (len(raw)>=203 and raw[:8]==bytes([241,154,109,4,17,177,109,188]) and
                        encode58(raw[43:75])==mint and encode58(raw[139:171])==address):
                    excluded.add(address)
            except (ValueError,KeyError,TypeError):
                pass
    return excluded


def analyze(mint, mint_account, accounts, owners, transactions, cfg, now):
    """Full holder aggregation + a stated, bounded transaction sample; no safety score."""
    info = mint_account['data']['parsed']['info']
    if mint_account['owner'] not in (TOKEN,TOKEN22) or mint_account['data']['parsed']['type']!='mint':
        raise EvidenceUnavailable('Unsupported mint account')
    supply = int(info['supply'])
    if supply <= 0:
        raise EvidenceUnavailable('Missing token supply')
    flags, unknown = [], []
    if info.get('mintAuthority'):
        flags.append('Mint authority can create more tokens')
    if info.get('freezeAuthority'):
        flags.append('Freeze authority can freeze token accounts')
    # Metadata changes do not move tokens; transfer controls and unknown extensions fail closed.
    extensions = info.get('extensions',[])
    for ext in extensions:
        name = ext.get('extension','unknown')
        if name not in ('metadataPointer','tokenMetadata'):
            flags.append('Unsupported or restrictive token extension: '+name)
    if mint_account['owner']==TOKEN22 and 'extensions' not in info:
        unknown.append('Token-2022 extension data is missing')
    valid, total, account_map = [], 0, {}
    for a in accounts:
        data = a['account']['data']['parsed']
        ti = data['info']
        if data['type']!='account' or ti['mint']!=mint or a['account']['owner']!=mint_account['owner']:
            raise EvidenceUnavailable('Holder account set contains unverified accounts')
        amount = int(ti['tokenAmount']['amount'])
        if amount<0 or a['pubkey'] in account_map:
            raise EvidenceUnavailable('Invalid holder amounts or duplicate accounts')
        account_map[a['pubkey']] = ti['owner']
        total += amount
        valid.append(a)
        if amount and (ti.get('state')=='frozen' or ti.get('delegate')):
            flags.append('A funded holder account is frozen or delegated')
    if total != supply:
        unknown.append('Holder balances do not reconcile to the mint supply')
    excluded = verified_vaults(mint,valid,owners)
    balances = defaultdict(int)
    vault_amount = 0
    for a in valid:
        ti = a['account']['data']['parsed']['info'];amount=int(ti['tokenAmount']['amount'])
        if a['pubkey'] in excluded:
            vault_amount += amount
        else:
            balances[ti['owner']] += amount
    # Quote concentration over distributed supply, so a large curve balance cannot hide a whale.
    distributed = supply-vault_amount
    if distributed<=0:
        unknown.append('No distributed supply to analyze')
    ranked = sorted(balances.items(),key=lambda x:x[1],reverse=True)
    top = ranked[0][1]/distributed if ranked and distributed>0 else None
    top10 = sum(v for _,v in ranked[:10])/distributed if distributed>0 else None
    if top is not None and top > cfg['max_holder_fraction']:
        flags.append('One owner holds more than 10% of distributed supply')
    if top10 is not None and top10 > cfg['max_top10_fraction']:
        flags.append('Ten owners hold more than 35% of distributed supply')
    seen, successful, edges = set(), 0, []
    for tx in transactions:
        if not tx or not tx.get('meta') or tx['meta'].get('err') is not None:
            unknown.append('Transaction evidence is missing or failed')
            continue
        signature = tx.get('transaction',{}).get('signatures',[None])[0]
        if signature in seen:
            continue
        seen.add(signature)
        at = tx.get('blockTime')
        if not isinstance(at,(int,float)) or at>now+2 or now-at>cfg['recent_seconds']:
            continue
        msg = tx['transaction']['message']
        keys = [k.get('pubkey') if isinstance(k,dict) else k for k in msg['accountKeys']]
        related = dict(account_map)
        for balance in tx['meta'].get('preTokenBalances',[])+tx['meta'].get('postTokenBalances',[]):
            if balance.get('mint')==mint and balance.get('owner') and balance['accountIndex']<len(keys):
                related[keys[balance['accountIndex']]]=balance['owner']
        instructions = list(msg.get('instructions',[]))
        for group in tx['meta'].get('innerInstructions') or []:
            instructions.extend(group['instructions'])
        balances_in_tx=tx['meta'].get('preTokenBalances',[])+tx['meta'].get('postTokenBalances',[])
        relevant_tx=any(b.get('mint')==mint for b in balances_in_tx)
        relevant_tx = relevant_tx or any(isinstance(i.get('parsed'),dict) and
            (i['parsed'].get('info',{}).get('mint')==mint or i['parsed'].get('info',{}).get('source') in account_map)
            for i in instructions if i.get('programId') in (TOKEN,TOKEN22))
        if not relevant_tx:
            unknown.append('A sampled transaction had no verifiable token evidence')
            continue
        successful += 1
        initializes_mint=any(isinstance(i.get('parsed'),dict) and
            i['parsed'].get('type') in ('initializeMint','initializeMint2') and
            i['parsed'].get('info',{}).get('mint')==mint for i in instructions)
        for instruction in instructions:
            if instruction.get('programId') not in (TOKEN,TOKEN22):
                continue
            parsed=instruction.get('parsed')
            if not isinstance(parsed,dict):
                unknown.append('A sampled token instruction could not be decoded')
                continue
            kind, ti = parsed.get('type'),parsed.get('info',{})
            relevant = ti.get('mint')==mint or ti.get('account') in related or ti.get('source') in related
            if not relevant:
                continue
            suspicious_control = (kind in ('mintTo','mintToChecked') and not initializes_mint or
                kind=='setAuthority' and ti.get('newAuthority') is not None or kind=='freezeAccount')
            if suspicious_control:
                flags.append('Recent minting, authority change or freeze instruction')
            if kind not in ('transfer','transferChecked','transferCheckedWithFee'):
                continue
            src,dst=ti.get('source'),ti.get('destination')
            if src in excluded or dst in excluded:
                continue  # Verified pool flow is not a wallet-to-wallet distribution flag.
            if src not in related or dst not in related:
                unknown.append('Transfer owner could not be resolved')
                continue
            amount=int(ti.get('amount',ti.get('tokenAmount',{}).get('amount',0)))
            if related[src]!=related[dst]:
                fraction=amount/distributed if distributed>0 else 1
                edges.append((related[src],related[dst],fraction))
                if fraction>=cfg['max_transfer_fraction']:
                    flags.append('Large recent wallet-to-wallet transfer')
    if successful<cfg['minimum_transactions']:
        unknown.append('Insufficient recent transaction evidence')
    fanout=defaultdict(set);moved=defaultdict(float);directions=set()
    for src,dst,amount in edges:
        fanout[src].add(dst);moved[src]+=amount;directions.add((src,dst))
    if any(len(v)>=3 and moved[k]>=.01 for k,v in fanout.items()):
        flags.append('One wallet distributed at least 1% to multiple wallets')
    if any((dst,src) in directions for src,dst in directions):
        flags.append('Circular wallet transfers in the sampled transactions')
    return {'status':'blocked' if flags else 'unknown' if unknown else 'clear',
        'reasons':list(dict.fromkeys(flags+unknown)), 'holder_count':sum(v>0 for v in balances.values()),
        'top_owner_fraction':top,'top10_fraction':top10,'excluded_vault_accounts':len(excluded),
        'transactions_checked':successful,'transfer_edges':len(edges),
        'coverage':'Full current token accounts; bounded recent mint/large-holder transaction sample. Linked-wallet ownership and all transfers are not proven.'}


def check(mint,cfg,rpc=None,now=None):
    now=time.time() if now is None else now
    result={'mint':mint,'checked_at':now,'policy_hash':hashlib.sha256(canonical(cfg).encode()).hexdigest()}
    try:
        if not MINT.fullmatch(mint):
            raise EvidenceUnavailable('Invalid mint address')
        rpc=rpc or Rpc(cfg)
        ma=rpc('getAccountInfo',[mint,{'encoding':'jsonParsed','commitment':'confirmed'}])
        account=ma['value'];slot=ma['context']['slot']
        if not account or account['owner'] not in (TOKEN,TOKEN22):
            raise EvidenceUnavailable('Mint account unavailable or unsupported')
        holder_result=rpc('getProgramAccounts',[account['owner'],{'encoding':'jsonParsed','commitment':'confirmed','minContextSlot':slot,'withContext':True,'filters':[{'memcmp':{'offset':0,'bytes':mint}}]}])
        holders=holder_result['value']
        if len(holders)>5000:
            raise EvidenceUnavailable('Holder scan exceeds 5,000-account coverage limit')
        owner_keys=list(dict.fromkeys(h['account']['data']['parsed']['info']['owner'] for h in holders))
        owner_accounts={}
        # Only potential pool owners need program-account verification.
        for start in range(0,len(owner_keys),100):
            keys=owner_keys[start:start+100]
            vals=rpc('getMultipleAccounts',[keys,{'encoding':'base64','commitment':'confirmed','minContextSlot':slot}])['value']
            if len(vals)!=len(keys):
                raise EvidenceUnavailable('Incomplete owner-account verification')
            owner_accounts.update(zip(keys,vals))
        ranked=sorted(holders,key=lambda h:int(h['account']['data']['parsed']['info']['tokenAmount']['amount']),reverse=True)
        vaults=verified_vaults(mint,holders,owner_accounts)
        addresses=[mint]+[h['pubkey'] for h in ranked if h['pubkey'] not in vaults][:3]
        signatures={}
        for addr in addresses:
            rows=rpc('getSignaturesForAddress',[addr,{'limit':cfg['transaction_limit'],'commitment':'confirmed'}])
            for row in rows:
                if row.get('err') is None and isinstance(row.get('blockTime'),(int,float)) and now-cfg['recent_seconds']<=row['blockTime']<=now+2:
                    signatures[row['signature']]=row['blockTime']
        chosen=sorted(signatures,key=lambda sig:signatures[sig],reverse=True)[:cfg['transaction_limit']]
        txs=[rpc('getTransaction',[sig,{'encoding':'jsonParsed','commitment':'confirmed','maxSupportedTransactionVersion':0}]) for sig in chosen]
        result.update(analyze(mint,account,holders,owner_accounts,txs,cfg,now))
        result['evidence_hash']=hashlib.sha256(canonical([account,holders,owner_accounts,txs]).encode()).hexdigest()
        result['analysis_hash']=hashlib.sha256(__import__('pathlib').Path(__file__).read_bytes()).hexdigest()
    except EvidenceUnavailable as e:
        result.update(status='unknown',reasons=[str(e)])
    except (KeyError,ValueError,TypeError,IndexError,AttributeError):
        result.update(status='unknown',reasons=['Ownership evidence was incomplete or could not be decoded'])
    return result
