"""纯标准库 AES-128/192/256（CBC / ECB + PKCS7）—— 网易云与酷狗共用。

为什么不用 `cryptography` / `pycryptodome`：这套客户端存在的理由就是「不要再被
装不上的轮子坑」（pymusiclibrary 的 manylinux wheel 里塞的是 Windows DLL）。
S 盒按 Rijndael 定义现场生成、密钥扩展支持 16/24/32 字节密钥，加解密都有，
配 `tests/test_ncm_native.py` 里的 FIPS-197 / NIST SP 800-38A 向量自检。

网易云只用 CBC+AES-128；酷狗那边会用到 AES-256（`md5(random16)[0:32]` 当 32 字节密钥）
和 ECB，所以这里做成通用的。
"""
from __future__ import annotations

__all__ = [
    "aes_cbc_encrypt", "aes_cbc_decrypt", "aes_ecb_encrypt", "aes_ecb_decrypt",
    "pkcs7_pad", "pkcs7_unpad",
]


# --------------------------------------------------------------------------
# 表与基础运算
# --------------------------------------------------------------------------

def _rotl8(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (8 - shift))) & 0xff


def _make_sbox() -> list[int]:
    sbox = [0] * 256
    p, q = 1, 1
    while True:
        p = p ^ ((p << 1) & 0xff) ^ (0x1b if p & 0x80 else 0)
        q ^= (q << 1) & 0xff
        q ^= (q << 2) & 0xff
        q ^= (q << 4) & 0xff
        if q & 0x80:
            q ^= 0x09
        sbox[p] = q ^ _rotl8(q, 1) ^ _rotl8(q, 2) ^ _rotl8(q, 3) ^ _rotl8(q, 4) ^ 0x63
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


SBOX = _make_sbox()
INV_SBOX = [0] * 256
for _i, _v in enumerate(SBOX):
    INV_SBOX[_v] = _i

RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x1b) & 0xff if a & 0x100 else a


def _gmul(a: int, b: int) -> int:
    out = 0
    for _ in range(8):
        if b & 1:
            out ^= a
        a = _xtime(a)
        b >>= 1
    return out & 0xff


def _expand_key(key: bytes) -> list[bytes]:
    """支持 16 / 24 / 32 字节密钥（AES-128 / 192 / 256），返回 Nr+1 组轮密钥。"""
    nk = len(key) // 4
    if len(key) not in (16, 24, 32):
        raise ValueError("AES 密钥长度必须是 16 / 24 / 32 字节")
    total = 4 * (nk + 6 + 1)          # 4 * (Nr + 1) 个字
    words = [key[4 * i:4 * i + 4] for i in range(nk)]
    for i in range(nk, total):
        prev = words[i - 1]
        if i % nk == 0:
            prev = bytes([SBOX[prev[1]] ^ RCON[i // nk - 1], SBOX[prev[2]],
                          SBOX[prev[3]], SBOX[prev[0]]])
        elif nk > 6 and i % nk == 4:
            prev = bytes(SBOX[b] for b in prev)
        words.append(bytes(a ^ b for a, b in zip(words[i - nk], prev)))
    flat = b"".join(words)
    return [flat[16 * r:16 * r + 16] for r in range(len(flat) // 16)]


def _add_round_key(state: list[int], rk: bytes) -> None:
    for i in range(16):
        state[i] ^= rk[i]


def _shift_rows(state: list[int]) -> None:
    # state 是列优先：第 r 行的下标是 r, r+4, r+8, r+12
    for row in range(1, 4):
        vals = [state[row + 4 * col] for col in range(4)]
        vals = vals[row:] + vals[:row]
        for col in range(4):
            state[row + 4 * col] = vals[col]


def _inv_shift_rows(state: list[int]) -> None:
    for row in range(1, 4):
        vals = [state[row + 4 * col] for col in range(4)]
        vals = vals[-row:] + vals[:-row]
        for col in range(4):
            state[row + 4 * col] = vals[col]


def _mix_columns(state: list[int]) -> None:
    for col in range(4):
        i = 4 * col
        a0, a1, a2, a3 = state[i], state[i + 1], state[i + 2], state[i + 3]
        state[i] = _xtime(a0) ^ (_xtime(a1) ^ a1) ^ a2 ^ a3
        state[i + 1] = a0 ^ _xtime(a1) ^ (_xtime(a2) ^ a2) ^ a3
        state[i + 2] = a0 ^ a1 ^ _xtime(a2) ^ (_xtime(a3) ^ a3)
        state[i + 3] = (_xtime(a0) ^ a0) ^ a1 ^ a2 ^ _xtime(a3)


def _inv_mix_columns(state: list[int]) -> None:
    for col in range(4):
        i = 4 * col
        a0, a1, a2, a3 = state[i], state[i + 1], state[i + 2], state[i + 3]
        state[i] = _gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9)
        state[i + 1] = _gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13)
        state[i + 2] = _gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11)
        state[i + 3] = _gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14)


def _encrypt_block(block: bytes, round_keys: list[bytes]) -> bytes:
    rounds = len(round_keys) - 1
    state = list(block)
    _add_round_key(state, round_keys[0])
    for rnd in range(1, rounds):
        state = [SBOX[b] for b in state]
        _shift_rows(state)
        _mix_columns(state)
        _add_round_key(state, round_keys[rnd])
    state = [SBOX[b] for b in state]
    _shift_rows(state)
    _add_round_key(state, round_keys[rounds])
    return bytes(state)


def _decrypt_block(block: bytes, round_keys: list[bytes]) -> bytes:
    rounds = len(round_keys) - 1
    state = list(block)
    _add_round_key(state, round_keys[rounds])
    for rnd in range(rounds - 1, 0, -1):
        _inv_shift_rows(state)
        state = [INV_SBOX[b] for b in state]
        _add_round_key(state, round_keys[rnd])
        _inv_mix_columns(state)
    _inv_shift_rows(state)
    state = [INV_SBOX[b] for b in state]
    _add_round_key(state, round_keys[0])
    return bytes(state)


# --------------------------------------------------------------------------
# 分组模式
# --------------------------------------------------------------------------

def pkcs7_pad(data: bytes) -> bytes:
    pad = 16 - (len(data) % 16)
    return data + bytes([pad]) * pad


def pkcs7_unpad(data: bytes) -> bytes:
    if not data or len(data) % 16:
        raise ValueError("不是合法的 PKCS7 密文长度")
    pad = data[-1]
    if not 1 <= pad <= 16 or data[-pad:] != bytes([pad]) * pad:
        raise ValueError("PKCS7 填充校验失败")
    return data[:-pad]


def aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    keys = _expand_key(key)
    return b"".join(_encrypt_block(data[off:off + 16], keys)
                    for off in range(0, len(data), 16))


def aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    keys = _expand_key(key)
    return b"".join(_decrypt_block(data[off:off + 16], keys)
                    for off in range(0, len(data), 16))


def aes_cbc_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    keys = _expand_key(key)
    prev, out = iv, bytearray()
    for off in range(0, len(data), 16):
        block = bytes(a ^ b for a, b in zip(data[off:off + 16], prev))
        prev = _encrypt_block(block, keys)
        out += prev
    return bytes(out)


def aes_cbc_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    keys = _expand_key(key)
    prev, out = iv, bytearray()
    for off in range(0, len(data), 16):
        block = data[off:off + 16]
        out += bytes(a ^ b for a, b in zip(_decrypt_block(block, keys), prev))
        prev = block
    return bytes(out)
