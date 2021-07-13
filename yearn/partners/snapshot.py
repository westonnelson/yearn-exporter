import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import List

import pandas as pd
from brownie import web3
from joblib.parallel import Parallel, delayed
from web3._utils.abi import filter_by_name
from web3._utils.events import construct_event_topic_set
from yearn.events import decode_logs, get_logs_asap
from yearn.multicall2 import batch_call
from yearn.partners.charts import make_partner_charts
from yearn.partners.constants import OPEX_COST, get_tier
from yearn.prices import magic
from yearn.utils import contract_creation_block, get_block_timestamp
from yearn.v2.registry import Registry
from yearn.v2.vaults import Vault

logger = logging.getLogger(__name__)


def get_timestamps(blocks):
    data = Parallel(50, 'threading')(delayed(get_block_timestamp)(block) for block in blocks)
    return pd.to_datetime([x * 1e9 for x in data])


@lru_cache()
def get_protocol_fees(address):
    """
    Get all protocol fee payouts for a given vault.

    Fees can be found as vault share transfers to the rewards address.
    """
    vault = Vault.from_address(address)
    rewards = vault.vault.rewards()

    topics = construct_event_topic_set(
        filter_by_name('Transfer', vault.vault.abi)[0],
        web3.codec,
        {'sender': address, 'receiver': rewards},
    )
    logs = decode_logs(get_logs_asap(address, topics))
    return {log.block_number: log['value'] / vault.scale for log in logs}


@dataclass
class Wrapper:
    name: str
    vault: str
    wrapper: str

    def protocol_fees(self):
        return get_protocol_fees(self.vault)

    def balances(self, blocks):
        vault = Vault.from_address(self.vault)
        balances = batch_call([[vault.vault, 'balanceOf', self.wrapper, block] for block in blocks])
        return [balance / vault.scale for balance in balances]

    def total_supplies(self, blocks):
        vault = Vault.from_address(self.vault)
        supplies = batch_call([[vault.vault, 'totalSupply', block] for block in blocks])
        return [supply / vault.scale for supply in supplies]

    def vault_prices(self, blocks):
        prices = Parallel(50, 'threading')(delayed(magic.get_price)(self.vault, block=block) for block in blocks)
        return prices


@dataclass
class WildcardWrapper:
    name: str
    wrapper: str

    def unwrap(self) -> List[Wrapper]:
        registry = Registry()
        topics = construct_event_topic_set(
            filter_by_name('Transfer', registry.vaults[0].vault.abi)[0],
            web3.codec,
            {'receiver': self.wrapper},
        )
        addresses = [str(vault.vault) for vault in registry.vaults]
        from_block = min(ThreadPoolExecutor().map(contract_creation_block, addresses))
        deposits = {log.address for log in get_logs_asap(addresses, topics, from_block)}

        return [
            Wrapper(name=vault.name, vault=str(vault.vault), wrapper=self.wrapper)
            for vault in registry.vaults
            if str(vault.vault) in deposits
        ]


@dataclass
class Partner:
    name: str
    wrappers: List[Wrapper]
    treasury: str = None

    def process(self):
        # unwrap wildcard wrappers to a flat list
        flat_wrappers = []
        for wrapper in self.wrappers:
            if isinstance(wrapper, Wrapper):
                flat_wrappers.append(wrapper)
            elif isinstance(wrapper, WildcardWrapper):
                flat_wrappers.extend(wrapper.unwrap())

        # snapshot wrapper share at each harvest
        wrappers = []
        for wrapper in flat_wrappers:
            logger.info(wrapper.name)
            protocol_fees = wrapper.protocol_fees()
            if not protocol_fees:
                logger.info('no fees for %s', wrapper.name)
                continue

            blocks, protocol_fees = zip(*protocol_fees.items())
            wrap = pd.DataFrame(
                {
                    'block': blocks,
                    'timestamp': get_timestamps(blocks),
                    'protocol_fee': protocol_fees,
                    'balance': wrapper.balances(blocks),
                    'total_supply': wrapper.total_supplies(blocks),
                    'vault_price': wrapper.vault_prices(blocks),
                }
            )
            wrap['balance_usd'] = wrap.balance * wrap.vault_price
            wrap['share'] = wrap.balance / wrap.total_supply
            wrap['payout_base'] = wrap.share * wrap.protocol_fee * (1 - OPEX_COST)
            wrap['wrapper'] = wrapper.wrapper
            wrap['vault'] = wrapper.vault
            wrap = wrap.set_index('block')
            wrappers.append(wrap)
            # save a csv for reporting

        # calculate partner fee tier from cummulative wrapper balances
        partner = pd.concat(wrappers)
        total_balances = pd.pivot_table(partner, 'balance_usd', 'block', 'vault', 'sum').ffill().sum(axis=1)
        tiers = total_balances.apply(get_tier).rename('tier')

        # calculate final payout by vault after tier adjustments
        partner = partner.join(tiers)
        partner['payout'] = partner.payout_base * partner.tier

        self.export_csv(partner)
        payouts = self.export_payouts(partner)

        if partner.payout.sum():
            make_partner_charts(self, partner)

        return partner, payouts

    def export_csv(self, partner):
        path = Path(f'research/partners/{self.name}/partner.csv')
        path.parent.mkdir(parents=True, exist_ok=True)
        partner.to_csv(path)

    def export_payouts(self, partner):
        # calculate payouts grouped by month and vault token
        payouts = pd.pivot_table(partner, 'payout', 'timestamp', 'vault', 'sum').resample('1M').sum()
        # stack from wide to long format with one payment per line
        payouts = payouts.stack().reset_index()
        payouts['treasury'] = self.treasury
        payouts['partner'] = self.name
        # reorder columns
        payouts.columns = ['timestamp', 'token', 'amount', 'treasury', 'partner']
        payouts = payouts[['timestamp', 'partner', 'token', 'treasury', 'amount']]
        payouts.to_csv(Path(f'research/partners/{self.name}/payouts.csv'), index=False)
        return payouts


def process_partners(partners):
    total = 0
    payouts = []
    for partner in partners:
        result, payout = partner.process()
        payouts.append(payout)
        usd = (result.payout * result.vault_price).sum()
        print(partner.name, usd, 'usd to pay')
        total += usd

    print(total, 'total so far')
    path = Path('research/partners/payouts.csv')
    pd.concat(payouts).sort_values('timestamp').to_csv(path, index=False)
    print(f'saved to {path}')
