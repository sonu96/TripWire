"""Compute keccak256 topic0 hash from Solidity event signatures."""

import re
from hashlib import sha3_256 as _keccak256_fallback

_HEX_TOPIC_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _keccak256(data: bytes) -> bytes:
    """Compute keccak256 hash. Uses pysha3/pycryptodome if available, else hashlib."""
    try:
        from Crypto.Hash import keccak
        h = keccak.new(digest_bits=256)
        h.update(data)
        return h.digest()
    except ImportError:
        pass
    try:
        import sha3  # pysha3
        return sha3.keccak_256(data).digest()
    except ImportError:
        pass
    # Python 3.11+ hashlib has sha3_256 but NOT keccak256.
    # eth-hash with pycryptodome backend is the standard approach.
    # As a last resort, try eth_abi's dependency chain.
    try:
        from eth_hash.auto import keccak
        return keccak(data)
    except ImportError:
        raise ImportError(
            "No keccak256 implementation found. Install pycryptodome: pip install pycryptodome"
        )


def compute_topic0(event_signature: str) -> str:
    """Compute the keccak256 topic0 hash from an event signature.

    If the input is already a 0x-prefixed 66-char hex string, returns it
    lowercased (pass-through for already-hashed values).

    Examples:
        >>> compute_topic0("Transfer(address,address,uint256)")
        '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
        >>> compute_topic0("0xDDF252...")  # already a hash
        '0xddf252...'
    """
    sig = event_signature.strip()
    if _HEX_TOPIC_RE.match(sig):
        return sig.lower()
    digest = _keccak256(sig.encode("utf-8"))
    return "0x" + digest.hex()
