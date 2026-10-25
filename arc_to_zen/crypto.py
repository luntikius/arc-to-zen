from __future__ import annotations

import hashlib
import subprocess

from .common import MigrationError
from . import diagnostics


def aes_cbc(value, key, iv, decrypt=True):
    """macOS CommonCrypto AES-128-CBC with PKCS#7; no external crypto runtime."""
    import ctypes
    if len(key) != 16 or len(iv) != 16:
        raise MigrationError("Invalid AES key or IV length.")
    if decrypt and (not value or len(value) % 16):
        raise MigrationError("Arc encrypted value is corrupt or the Keychain key is incorrect.")
    library = ctypes.CDLL('/usr/lib/system/libcommonCrypto.dylib')
    crypt = library.CCCrypt
    crypt.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    crypt.restype = ctypes.c_int
    output = ctypes.create_string_buffer(len(value) + 16)
    count = ctypes.c_size_t()
    # kCCDecrypt=1 / kCCEncrypt=0, kCCAlgorithmAES=0, kCCOptionPKCS7Padding=1.
    status = crypt(1 if decrypt else 0, 0, 1, key, len(key), iv, value, len(value),
                   output, len(output), ctypes.byref(count))
    if status:
        raise MigrationError("Arc encrypted value is corrupt or the Keychain key is incorrect.")
    return output.raw[:count.value]


class ArcDecryptor:
    """Lazy Keychain access: inspect and dry-run never construct/read a key.

    macOS Chromium OSCrypt v10: PBKDF2-SHA1, saltysalt, 1003 iterations,
    128-bit AES-CBC, IV of 16 spaces. Unknown encryption versions are skipped.
    """
    def __init__(self, key_provider=None):
        self.provider = key_provider or self.keychain
        self.key = None

    @staticmethod
    @diagnostics.traced('keychain')
    def keychain():
        try:
            result = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", "Arc Safe Storage", "-a", "Arc", "-w"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MigrationError("Arc Keychain access failed or timed out.") from exc
        if result.returncode or not result.stdout:
            raise MigrationError("Arc Keychain access denied or Arc Safe Storage missing.")
        # security adds exactly one newline to its output.
        return result.stdout.removesuffix(b"\n")

    def __call__(self, ciphertext):
        if not ciphertext:
            return b""
        if not ciphertext.startswith(b"v10"):
            return None
        if self.key is None:
            self.key = hashlib.pbkdf2_hmac("sha1", self.provider(), b"saltysalt", 1003, 16)
        return aes_cbc(ciphertext[3:], self.key, b" " * 16)
