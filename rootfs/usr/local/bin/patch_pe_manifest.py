#!/usr/bin/env python3
"""
patch_pe_manifest.py
Add the Windows 10/11 <supportedOS> GUID to a PE binary's embedded RT_MANIFEST.

Background
----------
Wine 10+ implements the Windows 8.1 GetVersionEx compatibility shim: any PE
whose embedded manifest lacks the Windows 10 <supportedOS> GUID will see OS
version 6.2 (Windows 8) from GetVersionEx, even when the Wine prefix is
configured as win11.  Backblaze 9.4+ checks that value at startup and aborts
with "MajorVerTooOld" if it reads anything below 10.0.

This script finds the RT_MANIFEST resource (type 24) inside a PE, injects the
Windows 10 GUID, and writes the change back in-place within the existing
resource slot, so no section offsets or file sizes change.

Windows 10 / 11 share the same GUID:
    {8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}

Usage
-----
    patch_pe_manifest.py <pe_file>

Exit codes
----------
    0  — patched successfully, or file already had the GUID (no-op)
    1  — unrecoverable error (bad PE, pefile not installed, etc.)
"""

import os
import re
import sys

WIN10_GUID = "{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"
RT_MANIFEST = 24


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def has_win10_guid(xml: bytes) -> bool:
    """Return True if the manifest already contains the Win10 GUID."""
    return WIN10_GUID.lower().encode() in xml.lower()


def inject_guid(xml: bytes) -> "bytes | None":
    """
    Return a new XML byte string with the Win10 GUID inserted, or None if no
    suitable insertion point could be found.

    Insertion strategy (in order of preference):
      1. Inside an existing <application> element inside <compatibility>.
      2. A new <compatibility> block inserted before </assembly>.
    """
    try:
        text = xml.decode("utf-8", errors="replace")
    except Exception:
        return None

    win10_tag = f'<supportedOS Id="{WIN10_GUID}"/>'

    # Strategy 1 — existing <application> block
    app_match = re.search(
        r"(<\s*[Aa]pplication\s*>)(.*?)(</\s*[Aa]pplication\s*>)",
        text,
        re.DOTALL,
    )
    if app_match:
        new_text = (
            text[: app_match.start()]
            + app_match.group(1)
            + app_match.group(2)
            + f"      {win10_tag}\n    "
            + app_match.group(3)
            + text[app_match.end() :]
        )
        return new_text.encode("utf-8")

    # Strategy 2 — inject a whole new <compatibility> block
    compat_block = (
        "\n  <compatibility"
        ' xmlns="urn:schemas-microsoft-com:compatibility.v1">\n'
        "    <application>\n"
        f"      {win10_tag}\n"
        "    </application>\n"
        "  </compatibility>"
    )
    assembly_close = re.search(r"</\s*assembly\s*>", text, re.IGNORECASE)
    if assembly_close:
        new_text = (
            text[: assembly_close.start()]
            + compat_block
            + "\n"
            + text[assembly_close.start() :]
        )
        return new_text.encode("utf-8")

    return None  # No suitable insertion point found


def fit_to_slot(new_xml: bytes, slot_size: int) -> "bytes | None":
    """
    Pad or trim *new_xml* so it fits exactly *slot_size* bytes.

    Padding uses ASCII spaces, which are valid inside XML character data and
    will be ignored by any XML parser.  If even after stripping trailing
    whitespace the new content is larger than the slot, return None.
    """
    if len(new_xml) <= slot_size:
        return new_xml + b" " * (slot_size - len(new_xml))

    # Try stripping trailing whitespace to squeeze into the slot
    trimmed = new_xml.rstrip()
    if len(trimmed) <= slot_size:
        return trimmed + b" " * (slot_size - len(trimmed))

    return None  # Cannot fit without truncating meaningful content


# ---------------------------------------------------------------------------
# PE patching
# ---------------------------------------------------------------------------

def patch_pe(path: str) -> bool:
    """
    Patch the RT_MANIFEST resource in *path*.  Returns True on success or
    no-op, False on unrecoverable failure.
    """
    try:
        import pefile  # type: ignore
    except ImportError:
        print(
            "ERROR: pefile is not installed.\n"
            "       Run: pip3 install pefile --break-system-packages",
            file=sys.stderr,
        )
        return False

    # --- Parse PE -----------------------------------------------------------
    try:
        pe = pefile.PE(path, fast_load=True)
        pe.parse_data_directories(
            directories=[
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]
            ]
        )
    except Exception as exc:
        print(f"ERROR: Cannot parse PE {path!r}: {exc}", file=sys.stderr)
        return False

    if not hasattr(pe, "DIRECTORY_ENTRY_RESOURCE"):
        print(f"SKIP:  No resource section in {path!r}")
        pe.close()
        return True  # Nothing to patch — not an error

    # --- Load raw bytes ------------------------------------------------------
    with open(path, "rb") as fh:
        raw = bytearray(fh.read())

    patched_any = False

    # --- Walk RT_MANIFEST resources -----------------------------------------
    for res_type in pe.DIRECTORY_ENTRY_RESOURCE.entries:
        type_id   = getattr(res_type, "id",   None)
        type_name = str(getattr(res_type, "name", "") or "").upper()

        if type_id != RT_MANIFEST and type_name != "RT_MANIFEST":
            continue

        for res_id_entry in res_type.directory.entries:
            for lang_entry in res_id_entry.directory.entries:
                data_rva = lang_entry.data.struct.OffsetToData
                slot_size = lang_entry.data.struct.Size

                try:
                    offset = pe.get_offset_from_rva(data_rva)
                except Exception:
                    continue

                original = bytes(raw[offset : offset + slot_size])

                # Already patched?
                if has_win10_guid(original):
                    print(f"SKIP:  {path!r} already contains Win10/11 GUID")
                    pe.close()
                    return True

                # Inject the GUID
                new_xml = inject_guid(original)
                if new_xml is None:
                    print(
                        f"WARN:  No suitable insertion point in manifest of "
                        f"{path!r} — skipping",
                        file=sys.stderr,
                    )
                    continue

                # Fit into the existing resource slot
                fitted = fit_to_slot(new_xml, slot_size)
                if fitted is None:
                    print(
                        f"WARN:  New manifest ({len(new_xml)} B) is larger than "
                        f"the existing slot ({slot_size} B) in {path!r} — skipping",
                        file=sys.stderr,
                    )
                    continue

                raw[offset : offset + slot_size] = fitted
                patched_any = True

    pe.close()

    if patched_any:
        with open(path, "wb") as fh:
            fh.write(raw)
        print(f"OK:    Patched {path!r} with Windows 10/11 supportedOS GUID")
        return True

    print(f"SKIP:  No patchable RT_MANIFEST found in {path!r}")
    return True  # Not finding a manifest is not a fatal error for our purposes


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <pe_file>", file=sys.stderr)
        sys.exit(1)

    target = sys.argv[1]
    if not os.path.isfile(target):
        print(f"ERROR: File not found: {target!r}", file=sys.stderr)
        sys.exit(1)

    sys.exit(0 if patch_pe(target) else 1)
