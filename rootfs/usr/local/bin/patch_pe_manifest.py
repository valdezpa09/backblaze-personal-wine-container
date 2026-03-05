#!/usr/bin/env python3
"""
Patch a Win10 <compatibility> entry into the embedded RT_MANIFEST of a PE.

Usage: patch_pe_manifest.py <pe_file> [<pe_file> ...]

Exit 0 = all files patched (or already had the GUID).
Exit 1 = at least one file could not be patched.
"""
import struct, sys

WIN10_MANIFEST = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">'
    '<compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1">'
    '<application>'
    '<supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"/>'
    '</application>'
    '</compatibility>'
    '</assembly>'
)
WIN10_GUID = '8e0f7a12'


def patch(path):
    """
    Return True if the file was patched (or was already patched).
    Return False on any error.
    """
    try:
        with open(path, 'rb') as fh:
            raw = bytearray(fh.read())
    except OSError as e:
        print(f'{path}: cannot read: {e}', file=sys.stderr)
        return False

    if raw[:2] != b'MZ':
        print(f'{path}: not a PE file', file=sys.stderr)
        return False
    pe = struct.unpack_from('<I', raw, 0x3c)[0]
    if raw[pe:pe+4] != b'PE\0\0':
        print(f'{path}: bad PE signature', file=sys.stderr)
        return False

    ns  = struct.unpack_from('<H', raw, pe+6)[0]
    osz = struct.unpack_from('<H', raw, pe+20)[0]
    oo  = pe + 24
    mg  = struct.unpack_from('<H', raw, oo)[0]
    dd  = oo + (112 if mg == 0x20b else 96)
    rv  = struct.unpack_from('<I', raw, dd+16)[0]    # .rsrc RVA
    sb  = oo + osz

    sva = sro = sh = None
    for i in range(ns):
        h  = sb + i*40
        va = struct.unpack_from('<I', raw, h+12)[0]
        if va == rv:
            sva = va
            sro = struct.unpack_from('<I', raw, h+20)[0]
            sh  = h
            break
    if sh is None:
        print(f'{path}: .rsrc section not found', file=sys.stderr)
        return False

    def r2f(r): return sro + (r - sva)

    def de(o):
        n = (struct.unpack_from('<H', raw, o+12)[0] +
             struct.unpack_from('<H', raw, o+14)[0])
        return [(struct.unpack_from('<I', raw, o+16+i*8)[0] & 0x7fffffff,
                 bool(struct.unpack_from('<I', raw, o+16+i*8+4)[0] >> 31),
                 sro + (struct.unpack_from('<I', raw, o+16+i*8+4)[0] & 0x7fffffff))
                for i in range(n)]

    lf = mo = ms = None
    for t, _, to in de(sro):
        if t != 24: continue                    # RT_MANIFEST = 24
        for r, _, ro2 in de(to):
            if r != 1: continue                 # CREATEPROCESS_MANIFEST_ID = 1
            for _, _, lo in de(ro2):
                mo = r2f(struct.unpack_from('<I', raw, lo)[0])
                ms = struct.unpack_from('<I', raw, lo+4)[0]
                lf = lo
                break
            break
        break

    if lf is None:
        # No RT_MANIFEST resource at all – nothing to patch, not an error.
        print(f'{path}: no RT_MANIFEST resource, skipping', file=sys.stderr)
        return True

    xml = raw[mo:mo+ms].decode('utf-8', 'replace')
    if WIN10_GUID in xml.lower() or '<compatibility' in xml.lower():
        print(f'{path}: already has Win10 compat entry', file=sys.stderr)
        return True

    nb  = WIN10_MANIFEST.encode('utf-8')
    nsz = len(nb)

    if nsz > ms:
        # Replacement doesn't fit in-place – this should never happen with our
        # ~306-byte manifest vs any real application manifest (always ≥ 350 B).
        print(f'{path}: replacement manifest ({nsz}b) > original ({ms}b), cannot patch',
              file=sys.stderr)
        return False

    raw[mo:mo + ms] = nb + bytes(ms - nsz)
    struct.pack_into('<I', raw, lf+4, nsz)

    try:
        with open(path, 'wb') as fh:
            fh.write(raw)
    except OSError as e:
        print(f'{path}: cannot write: {e}', file=sys.stderr)
        return False

    print(f'{path}: patched ({ms}b -> {nsz}b, in-place)', file=sys.stderr)
    return True


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(f'usage: {sys.argv[0]} <file> [<file> ...]', file=sys.stderr)
        sys.exit(1)
    ok = all(patch(p) for p in sys.argv[1:])
    sys.exit(0 if ok else 1)
