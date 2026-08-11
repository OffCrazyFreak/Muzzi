#!/usr/bin/env python3
"""Export Chromium-family cookies to a Netscape file, choosing the RIGHT key.

yt-dlp picks the first keyring entry labelled "Chromium Safe Storage". On this
machine eight applications share that label (discord, Cursor, Code, t3code,
unityhub, Electron, chromium...), so it usually picks one that belongs to
something else and silently produces garbage for most cookies -- including
every YouTube auth cookie, which is exactly the ones that matter.

Try every candidate key and keep the one that actually decrypts.
"""
import sqlite3, shutil, tempfile, os, sys, secretstorage
from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Hash import SHA1

db, out = sys.argv[1], sys.argv[2]
hosts = sys.argv[3] if len(sys.argv) > 3 else "youtube"

t = tempfile.mktemp(); shutil.copy(db, t)
con = sqlite3.connect(t)
rows = con.execute("select host_key,name,encrypted_value,path,is_secure,expires_utc "
                   "from cookies").fetchall()
os.unlink(t)

pws, labels = [b'peanuts'], ['(default "peanuts")']
bus = secretstorage.dbus_init()
for col in secretstorage.get_all_collections(bus):
    if col.is_locked():
        continue
    for item in col.get_all_items():
        if 'Safe Storage' in item.get_label():
            try:
                pws.append(item.get_secret())
                labels.append(f"{item.get_label()} / {item.get_attributes().get('application','?')}")
            except Exception:
                pass


def decrypt(val, key):
    if not val or bytes(val[:3]) not in (b'v10', b'v11'):
        return None
    try:
        d = AES.new(key, AES.MODE_CBC, IV=b' ' * 16).decrypt(bytes(val[3:]))
        d = d[:-d[-1]]
    except Exception:
        return None
    # Chromium 130+ prepends a 32-byte SHA256 of the host to the plaintext.
    for cand in (d, d[32:]):
        try:
            s = cand.decode('utf-8')
        except Exception:
            continue
        if s.isprintable():
            return s
    return None


best, best_key, best_label = -1, None, None
for pw, lbl in zip(pws, labels):
    key = PBKDF2(pw, b'saltysalt', 16, count=1, hmac_hash_module=SHA1)
    n = sum(1 for h, nm, v, *_ in rows if hosts in h and decrypt(v, key))
    if n > best:
        best, best_key, best_label = n, key, lbl

print(f"  key: {best_label}  ({best} '{hosts}' cookies decrypt)")

n = 0
with open(out, "w") as fh:
    fh.write("# Netscape HTTP Cookie File\n")
    for host, name, val, path, secure, exp in rows:
        if hosts not in host:
            continue
        v = decrypt(val, best_key)
        if v is None:
            continue
        # Chromium stores microseconds since 1601; Netscape wants unix seconds.
        e = int(exp / 1000000 - 11644473600) if exp else 0
        fh.write(f"{host}\t{'TRUE' if host.startswith('.') else 'FALSE'}\t{path}\t"
                 f"{'TRUE' if secure else 'FALSE'}\t{max(e,0)}\t{name}\t{v}\n")
        n += 1
print(f"  wrote {n} cookies -> {out}")
