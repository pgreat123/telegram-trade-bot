"""
DEX-agnostic honeypot check via ERC20 storage-slot balance override.

Gives a scratch address a token balance directly on the token contract
(via eth_call state override), then simulates a transfer() out of that
address. This exercises the TOKEN's own transfer logic -- where classic
honeypot mechanisms live (blacklists, owner-only sell flags, one-way
transfer blocks) -- without touching any DEX router. That makes it work
the same way regardless of whether the pool is on Uniswap V3, Pons V2,
O1 Rwa, Longxyz, or V4.

Honest limitations:
  - eth_call is a single stateless simulation. We can confirm the
    transfer call itself doesn't revert and returns success, but we
    cannot atomically confirm the recipient's balance actually changed
    across two separate calls (that needs a bundled multicall/simulation
    contract, which is a bigger lift -- worth building later if this
    catches too many false negatives).
  - Some honeypots only block selling TO a specific DEX pair address,
    or gate on tx.origin / a cooldown that only trips on a second real
    tx. A transfer-to-scratch-address simulation won't reproduce those.
    This check is a real signal, not a guarantee -- pair it with
    check_liquidity, check_holders_and_tax (GoPlus), and token age as
    you already do; don't rely on this alone.
  - Requires an RPC that supports eth_call state overrides. Confirm
    Robinhood Chain's RPC supports the `stateDiff` param before trusting
    this in production -- if it silently ignores overrides, every check
    will falsely report "can't confirm balance", which the code below
    treats as a fail (safe default), not a pass.
"""
import logging

from web3 import Web3
from web3.exceptions import ContractLogicError

log = logging.getLogger("safety.honeypot_sim")

CANDIDATE_BALANCE_SLOTS = list(range(0, 10))  # try common mapping slots, don't assume slot 0

ERC20_ABI = [
    {
        "constant": True, "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function",
    },
    {
        "constant": False,
        "inputs": [{"name": "recipient", "type": "address"}, {"name": "amount", "type": "uint256"}],
        "name": "transfer", "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable", "type": "function",
    },
]

SCRATCH_HOLDER = Web3.to_checksum_address("0x000000000000000000000000000000000000b1")
SCRATCH_RECIPIENT = Web3.to_checksum_address("0x000000000000000000000000000000000000b2")
PROBE_AMOUNT = 10**18  # 1 whole token unit -- fine for a pass/fail transfer check regardless of actual decimals


def _mapping_slot(key_address: str, slot: int) -> bytes:
    """keccak256(abi.encode(address_key, slot)) -- standard Solidity storage slot for mapping(address => uint256) declared at position `slot`."""
    return Web3.solidity_keccak(["address", "uint256"], [Web3.to_checksum_address(key_address), slot])


def _find_balance_slot_sync(w3: Web3, token: str) -> int | None:
    """Try each candidate slot, confirm via balanceOf that the override actually took effect. Returns the first working slot, or None if none worked."""
    contract = w3.eth.contract(address=token, abi=ERC20_ABI)
    for slot in CANDIDATE_BALANCE_SLOTS:
        storage_key = _mapping_slot(SCRATCH_HOLDER, slot)
        override = {token: {"stateDiff": {Web3.to_hex(storage_key): Web3.to_hex(PROBE_AMOUNT, size=32)}}}
        try:
            reported = contract.functions.balanceOf(SCRATCH_HOLDER).call(
                {"from": SCRATCH_HOLDER}, "latest", override
            )
            if reported == PROBE_AMOUNT:
                return slot
        except Exception:
            continue
    return None


def _simulate_transfer_sync(token_address: str, rpc_url: str) -> dict:
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    token = Web3.to_checksum_address(token_address)

    slot = _find_balance_slot_sync(w3, token)
    if slot is None:
        return {
            "is_honeypot": True,
            "error": "could not establish a token balance via storage override on any common slot "
                     "(non-standard storage layout, proxy contract, or RPC doesn't support state overrides)",
        }

    storage_key = _mapping_slot(SCRATCH_HOLDER, slot)
    override = {token: {"stateDiff": {Web3.to_hex(storage_key): Web3.to_hex(PROBE_AMOUNT, size=32)}}}
    contract = w3.eth.contract(address=token, abi=ERC20_ABI)

    try:
        result = contract.functions.transfer(SCRATCH_RECIPIENT, PROBE_AMOUNT).call(
            {"from": SCRATCH_HOLDER}, "latest", override
        )
    except ContractLogicError as e:
        return {"is_honeypot": True, "error": f"transfer() reverted in simulation: {e}"}
    except Exception as e:
        return {"is_honeypot": True, "error": f"transfer() simulation failed: {e}"}

    if result is False:
        return {"is_honeypot": True, "error": "transfer() returned false instead of reverting -- silent block"}

    return {"is_honeypot": False, "error": ""}