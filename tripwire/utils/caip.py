"""CAIP-2 chain identifier utilities."""


def caip2_to_chain_id(network: str) -> int:
    """Extract numeric chain ID from a CAIP-2 identifier.

    Example:
        >>> caip2_to_chain_id("eip155:8453")
        8453
    """
    parts = network.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid CAIP-2 identifier: {network}")
    try:
        return int(parts[1])
    except ValueError:
        raise ValueError(
            f"Non-numeric chain ID in CAIP-2 identifier '{network}': "
            f"expected an integer after ':', got '{parts[1]}'"
        )
