"""prime_chunk.py <lo> <hi> — count and sum primes in [lo, hi)."""
import json
import sys


def chunk(lo, hi):
    sieve = bytearray(b"\x01") * hi
    sieve[0:2] = b"\x00\x00"
    import math
    for i in range(2, int(math.isqrt(hi)) + 1):
        if sieve[i]:
            sieve[i * i:hi:i] = b"\x00" * ((hi - 1 - i * i) // i + 1)
    ps = [n for n in range(max(lo, 2), hi) if sieve[n]]
    return {"count": len(ps), "sum": sum(ps)}


if __name__ == "__main__":
    lo, hi = int(sys.argv[1]), int(sys.argv[2])
    print(json.dumps(chunk(lo, hi)))
