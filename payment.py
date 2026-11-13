# Copyright 2026 Grounder Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Paying one Grounder search from a wallet with x402, when the user has set one up.

A search with no account is free until Grounder's free searches are used up. Grounder then answers HTTP 402 with a payment offer in the ``PAYMENT-REQUIRED``
header (x402 version 2: a base64 JSON object whose ``accepts`` lists what Grounder takes). With ``GROUNDER_X402_PRIVATE_KEY`` set, ``provider._send`` signs one payment
for the offer and sends the same search again with it in ``PAYMENT-SIGNATURE``; Grounder runs the search, settles the payment and answers 200 with ``PAYMENT-RESPONSE``.

What this module will sign, and nothing else (``choose``): the ``exact`` scheme, on Base (``eip155:8453``), in USDC (its contract address below), moving the EIP-3009
``transferWithAuthorization`` way, at most ``GROUNDER_X402_MAX_PER_SEARCH`` dollars (default ``DEFAULT_CAP``), to the ``payTo`` the offer names. The network, the token and the
token's contract are fixed here, never read from the offer, so an offer for another chain or another asset is declined, not paid. The signature is the one an
``exact`` EVM client makes: an EIP-712 ``TransferWithAuthorization`` over USDC's own domain (https://github.com/x402-foundation/x402, specs/schemes/exact/scheme_exact_evm.md).
Signing needs the ``eth-account`` package, imported only here and only when a payment is signed, so a user who never sets a wallet needs nothing more than the plugin had.

Every refusal is a ``Declined`` whose words are for the user. No word of a refusal or a log line holds the key.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import time
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Optional

from agent.secret_scope import get_secret

logger = logging.getLogger(__name__)

KEY_ENV = "GROUNDER_X402_PRIVATE_KEY"  # the wallet's private key: a secret, in Hermes's .env
CAP_ENV = "GROUNDER_X402_MAX_PER_SEARCH"  # the most one search may cost, in US dollars
DEFAULT_CAP = "0.05"
NETWORK = "eip155:8453"  # Base
CHAIN_ID = 8453
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # USDC on Base: the only token this module pays
USDC_DECIMALS = 6
USDC_DOMAIN = ("USD Coin", "2")  # the token's EIP-712 name and version, used when an offer names none
SCHEME = "exact"
DEFAULT_TIMEOUT_S = 60  # how long a signed payment stays valid, when the offer says nothing
MAX_TIMEOUT_S = 3600  # and the longest it is ever left valid, whatever the offer says
REQUIRED_HEADER = "payment-required"  # response headers are kept lower case
SIGNATURE_HEADER = "PAYMENT-SIGNATURE"
RESPONSE_HEADER = "payment-response"
README = "https://github.com/grounder-labs/grounder-hermes-plugin#pay-per-search-with-a-wallet"
MISSING = ("Paying from a wallet needs the `eth-account` package in the Python that runs Hermes, and it is not installed there "
           "(the plugin README says how). Nothing was paid.")
_PRIVATE_KEY = re.compile(r"^(?:0x)?([0-9a-fA-F]{64})$")
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_DIGITS = re.compile(r"^[0-9]{1,30}$")
_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
_ZERO = "0x" + "0" * 40


class Declined(Exception):
    """A payment this plugin will not make, or could not: the message is the reason, in words for the user, and holds no key."""


def wallet_key() -> str:
    """The wallet's private key from this profile's secrets (``KEY_ENV``), or "" when none is set."""
    return str(get_secret(KEY_ENV) or "").strip()


def secret_forms() -> list[str]:
    """The wallet's key as it could be written into text, lower case: as set, and as bare hexadecimal. ``provider._refusal`` refuses an address that holds either. [] with none set."""
    key = wallet_key()
    match = _PRIVATE_KEY.match(key)
    return list(dict.fromkeys([key.lower(), *([match.group(1).lower()] if match else [])])) if key else []


def configured() -> bool:
    """Whether the user has set a wallet: the only thing that ever makes the plugin pay."""
    return bool(wallet_key())


def usd(atomic: int) -> str:
    """An amount of USDC in its smallest unit as dollars: ``9220`` is ``$0.00922``."""
    return "$" + format((Decimal(atomic) / Decimal(10 ** USDC_DECIMALS)).normalize(), "f")


def cap() -> int:
    """The most one search may cost, in USDC's smallest unit, from ``CAP_ENV`` (else ``DEFAULT_CAP`` dollars). A setting that is not a plain amount of dollars is an error, not
    the default: the user set a limit, and a limit that cannot be read must not become a bigger one."""
    raw = str(get_secret(CAP_ENV) or "").strip().removeprefix("$").strip() or DEFAULT_CAP
    try:
        dollars = Decimal(raw)
        if dollars.is_finite() and dollars >= 0:
            return int((dollars * 10 ** USDC_DECIMALS).to_integral_value(rounding=ROUND_FLOOR))
    except ArithmeticError:  # a malformed number, or one too large to hold (1e999999999)
        pass
    raise Declined(f"`{CAP_ENV}` is not an amount of dollars, so nothing was paid.")


def _decode(header: str) -> Any:
    try:
        return json.loads(base64.b64decode(header.strip(), validate=True).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None


def offer(headers: dict[str, str], body: Any) -> dict[str, Any]:
    """The payment offer of a 402: the ``PAYMENT-REQUIRED`` header's object, else the one in the body (``error.details.x402``). ``headers`` has lower-case names."""
    required = _decode(headers.get(REQUIRED_HEADER, "")) if headers.get(REQUIRED_HEADER) else None
    if not isinstance(required, dict):
        error = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), dict) else {}
        details = error.get("details") if isinstance(error.get("details"), dict) else {}
        required = details.get("x402")
    if not isinstance(required, dict) or required.get("x402Version") != 2 or not isinstance(required.get("accepts"), list):
        raise Declined("Grounder's payment offer was not one this plugin can read, so nothing was paid.")
    return required


def _atomic(value: Any) -> Optional[int]:
    text = value if isinstance(value, str) else str(value) if isinstance(value, int) and not isinstance(value, bool) else ""
    return int(text) if _DIGITS.match(text) and int(text) > 0 else None


def _payable(accepted: Any) -> Optional[int]:
    """The price of one entry of the offer's ``accepts`` when it is something this module pays, else None: ``exact`` on Base in USDC, moved by EIP-3009, to an address."""
    if not isinstance(accepted, dict):
        return None
    extra = accepted.get("extra") if isinstance(accepted.get("extra"), dict) else {}
    method = extra.get("assetTransferMethod")  # absent means eip3009; permit2 and the other methods are not signed here
    pay_to = accepted.get("payTo")
    if (accepted.get("scheme") != SCHEME or accepted.get("network") != NETWORK or not isinstance(accepted.get("asset"), str) or accepted["asset"].lower() != USDC.lower()
            or method not in (None, "eip3009") or not isinstance(pay_to, str) or not _ADDRESS.match(pay_to) or pay_to.lower() == _ZERO):
        return None
    return _atomic(accepted.get("amount"))


def choose(required: dict[str, Any], limit: int) -> dict[str, Any]:
    """The entry of the offer to pay: the cheapest of those ``_payable`` says this module pays, if it costs no more than ``limit``. Otherwise ``Declined``, with why."""
    prices = [(price, entry) for entry in required["accepts"] if (price := _payable(entry)) is not None]
    if not prices:
        raise Declined("None of Grounder's payment options is USDC on Base, the only one this plugin pays, so nothing was paid.")
    price, entry = min(prices, key=lambda pair: pair[0])
    if price > limit:
        raise Declined(f"This search costs {usd(price)}, more than the {usd(limit)} a search `{CAP_ENV}` allows, so nothing was paid.")
    return entry


def _timeout(entry: dict[str, Any]) -> int:
    seconds = entry.get("maxTimeoutSeconds")
    return min(max(seconds, 1), MAX_TIMEOUT_S) if isinstance(seconds, int) and not isinstance(seconds, bool) else DEFAULT_TIMEOUT_S


def _name(value: Any, fallback: str) -> str:
    return value if isinstance(value, str) and 0 < len(value) <= 64 else fallback


def sign(entry: dict[str, Any], required: dict[str, Any], key: str) -> str:
    """The ``PAYMENT-SIGNATURE`` header value for ``entry`` (from ``choose``), signed with the wallet's ``key``: the base64 JSON of an x402 version 2 payment payload that echoes
    the offer's ``resource`` and the chosen entry as ``accepted``, with the EIP-3009 authorization and its EIP-712 signature as ``payload``. Raises ``Declined``."""
    match = _PRIVATE_KEY.match(key)
    if not match:
        raise Declined(f"`{KEY_ENV}` is not a private key (64 hexadecimal characters), so nothing was paid.")
    try:
        from eth_account import Account
        from eth_utils import is_address
    except ImportError:
        raise Declined(MISSING) from None
    except Exception as error:  # an `eth-account` that is installed and broken: say so, and pay nothing
        logger.warning("The eth-account package could not be loaded: %s", type(error).__name__)
        raise Declined("The `eth-account` package could not be loaded, so nothing was paid.") from None
    if not is_address(entry["payTo"]):
        raise Declined("Grounder's payment offer named an address that is not valid, so nothing was paid.")
    try:
        account = Account.from_key("0x" + match.group(1))
    except Exception:  # a key outside the curve's range, or whatever this eth-account raises for one; its words are not repeated
        raise Declined(f"`{KEY_ENV}` is not a usable private key, so nothing was paid.") from None
    extra = entry.get("extra") if isinstance(entry.get("extra"), dict) else {}
    authorization = {"from": account.address, "to": entry["payTo"], "value": str(_atomic(entry["amount"])), "validAfter": "0",
                     "validBefore": str(int(time.time()) + _timeout(entry)), "nonce": "0x" + os.urandom(32).hex()}
    domain = {"name": _name(extra.get("name"), USDC_DOMAIN[0]), "version": _name(extra.get("version"), USDC_DOMAIN[1]), "chainId": CHAIN_ID, "verifyingContract": USDC}
    types = {"TransferWithAuthorization": [{"name": "from", "type": "address"}, {"name": "to", "type": "address"}, {"name": "value", "type": "uint256"},
                                           {"name": "validAfter", "type": "uint256"}, {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}]}
    message = {"from": authorization["from"], "to": authorization["to"], "value": int(authorization["value"]), "validAfter": int(authorization["validAfter"]),
               "validBefore": int(authorization["validBefore"]), "nonce": bytes.fromhex(authorization["nonce"][2:])}
    try:
        signed = Account.sign_typed_data(account.key, domain_data=domain, message_types=types, message_data=message)
    except Exception as error:  # fail closed: a signing error pays nothing, and only its class is logged
        logger.warning("A payment could not be signed: %s", type(error).__name__)
        raise Declined("The payment could not be signed, so nothing was paid.") from None
    payload: dict[str, Any] = {"x402Version": 2}
    if isinstance(required.get("resource"), dict):
        payload["resource"] = required["resource"]
    payload["accepted"] = entry
    payload["payload"] = {"signature": "0x" + bytes(signed.signature).hex(), "authorization": authorization}
    if isinstance(required.get("extensions"), dict):
        payload["extensions"] = required["extensions"]  # echoed as the offer sent it, as an x402 client does
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


_REASON = re.compile(r"^[A-Za-z0-9_ .:'-]{1,80}$")


def reason(headers: dict[str, str]) -> str:
    """Why Grounder refused a payment, when its 402 says so in the offer's ``error`` (a short plain word or phrase), else "". ``headers`` has lower-case names."""
    required = _decode(headers.get(REQUIRED_HEADER, "")) if headers.get(REQUIRED_HEADER) else None
    text = required.get("error") if isinstance(required, dict) else None
    return text if isinstance(text, str) and _REASON.match(text) else ""


def receipt(headers: dict[str, str], entry: dict[str, Any]) -> dict[str, Any]:
    """What a paid search tells the result: the price, the network and, when ``PAYMENT-RESPONSE`` names one, the settlement's transaction hash. ``headers`` has lower-case names."""
    settled = _decode(headers.get(RESPONSE_HEADER, "")) if headers.get(RESPONSE_HEADER) else None
    settled = settled if isinstance(settled, dict) else {}
    transaction = settled.get("transaction")
    return {"paid": True, "amount": usd(_atomic(entry["amount"]) or 0), "asset": "USDC", "network": "Base",
            **({"transaction": transaction} if isinstance(transaction, str) and _HASH.match(transaction) else {})}
