#!/usr/bin/env python3
"""Minimal AXML (binary AndroidManifest.xml) parser.

aapt1/aapt2's own dump tools vary in how (or whether) they print uses-sdk
attributes across build-tools versions, so LabCare CI verifies the SDK levels
straight from the binary chunk format itself.

    python3 scripts/verify_apk_sdk.py <apk> [--min-sdk N] [--target-sdk N]

Exits 0 when the compiled manifest satisfies the required levels, 1 otherwise;
in either case it prints the parsed uses-sdk values for the build log.
"""
import struct
import sys
import zipfile

CHUNK_AXML_HEADER = 0x00080003
CHUNK_STRING_POOL = 0x001C0001
CHUNK_START_ELEMENT = 0x00100102

TYPE_STRING = 0x03
TYPE_INT_DEC = 0x10
NULL = 0xFFFFFFFF


def parse_string_pool(data):
    (chunk_type, header_size, size, string_count, style_count,
     flags, strings_start, styles_start) = struct.unpack_from("<HHIIIIII", data, 0)
    utf8 = bool(flags & 0x100)
    offsets = [struct.unpack_from("<I", data, header_size + 4 * i)[0]
               for i in range(string_count)]
    strings = []
    for rel in offsets:
        p = strings_start + rel
        if utf8:
            # utf-8 entries: u16 len (utf-16 code units), u8 len, bytes, NUL
            _ulen = data[p]
            p += 1
            blen = data[p]
            p += 1
            strings.append(data[p:p + blen].decode("utf-8", "replace"))
        else:
            (length,) = struct.unpack_from("<H", data, p)
            p += 2
            if length & 0x8000:
                (length2,) = struct.unpack_from("<H", data, p)
                p += 2
                length = ((length & 0x7FFF) << 16) | length2
            strings.append(data[p:p + length * 2].decode("utf-16le", "replace"))
    return strings


def parse_manifest(data):
    """Walk the XML chunks collecting <element, {attr:value}> pairs."""
    if len(data) < 8 or struct.unpack_from("<I", data, 0)[0] != CHUNK_AXML_HEADER:
        raise ValueError("not an AXML file")
    total = struct.unpack_from("<I", data, 4)[0]
    off = 8
    strings = []
    elements = []
    while off < total:
        chunk_type, header_size, size = struct.unpack_from("<HHI", data, off)
        body = data[off:off + size]
        if chunk_type == CHUNK_STRING_POOL:
            strings = parse_string_pool(body)
        elif chunk_type == CHUNK_START_ELEMENT:
            (line, comment, ns, name, attr_start, attr_size, attr_count,
             id_idx, cls_idx, style_idx) = struct.unpack_from("<IIIIHHHHHH", body, 8)
            elem = strings[name]
            attrs = {}
            aoff = 16 + attr_start  # attributes follow the fixed element header
            for i in range(attr_count):
                ans, aname, araw, zero = struct.unpack_from("<IIIB", body, aoff)
                atype = body[aoff + 18 - 3] if False else struct.unpack_from("<B", body, aoff + 15)[0]
                adata = struct.unpack_from("<I", body, aoff + 16)[0]
                if atype == TYPE_INT_DEC:
                    val = adata
                elif atype == TYPE_STRING and adata < len(strings):
                    val = strings[adata]
                elif araw != NULL and araw < len(strings):
                    val = strings[araw]
                else:
                    val = adata
                attrs[strings[aname]] = val
                aoff += attr_size
            elements.append((elem, attrs))
        off += size
    return elements


def main():
    apk = sys.argv[1]
    min_want = target_want = None
    args = sys.argv[2:]
    while args:
        a = args.pop(0)
        if a == "--min-sdk":
            min_want = int(args.pop(0))
        elif a == "--target-sdk":
            target_want = int(args.pop(0))

    with zipfile.ZipFile(apk) as z:
        data = z.read("AndroidManifest.xml")

    try:
        elements = parse_manifest(data)
    except Exception as e:
        print(f"FATAL: cannot parse AXML manifest: {e}", file=sys.stderr)
        return 2

    uses_sdk = None
    for elem, attrs in elements:
        if elem == "uses-sdk":
            uses_sdk = attrs
            break

    if uses_sdk is None:
        print("FATAL: manifest has no uses-sdk element", file=sys.stderr)
        return 1

    min_sdk = uses_sdk.get("minSdkVersion")
    target_sdk = uses_sdk.get("targetSdkVersion")
    print(f"uses-sdk: minSdkVersion={min_sdk} targetSdkVersion={target_sdk}")

    failures = []
    if min_want is not None and (not isinstance(min_sdk, int) or min_sdk < min_want):
        failures.append(f"minSdkVersion {min_sdk} < required {min_want}")
    if target_want is not None and (not isinstance(target_sdk, int) or target_sdk < target_want):
        failures.append(f"targetSdkVersion {target_sdk} < required {target_want}")
    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
