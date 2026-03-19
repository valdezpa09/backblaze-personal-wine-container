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

Strategy
--------
1. Try to patch the Win10 GUID into the embedded RT_MANIFEST in-place.
2. If the new XML is too large to fit in the existing resource slot, fall back
   to writing an external sidecar manifest (<exe>.manifest) alongside the PE.
   Wine reads external manifests first, so this is fully effective.

Windows 10 / 11 share the same GUID:
    {8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}

Usage
-----
    patch_pe_manifest.py <pe_file>

Exit codes
----------
    0  — patched successfully (embedded or sidecar), or already had the GUID
    1  — unrecoverable error (bad PE, pefile not installed, etc.)
"""

import os
import re
import sys

WIN10_GUID = "{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"
RT_MANIFEST = 24

# Minimal valid sidecar manifest that just declares Win10 compatibility.
# Used when the embedded slot is too small to hold the injected XML.
SIDECAR_MANIFEST = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1">
    <application>
      <supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"/>
    </application>
  </compatibility>
</assembly>
"""


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def has_win10_guid(xml: bytes) -> bool:
    return WIN10_GUID.lower().encode() in xml.lower()


def inject_guid(xml: bytes) -> "bytes | None":
    """
    Return new XML with the Win10 GUID inserted, or None if no suitable
    insertion point could be found.
    """
    try:
        text = xml.decode("utf-8", errors="replace")
    except Exception:
        return None

    win10_tag = f'<supportedOS Id="{WIN10_GUID}"/>'

    # Strategy 1 — insert inside existing <application> block
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
            + text[app_match.end():]
        )
        return new_text.encode("utf-8")

    # Strategy 2 — inject a whole new <compatibility> block before </assembly>
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
            + text[assembly_close.start():]
        )
        return new_text.encode("utf-8")

    return None


def fit_to_slot(new_xml: bytes, slot_size: int) -> "bytes | None":
    """Pad new_xml to exactly slot_size bytes using spaces, or return None."""
    if len(new_xml) <= slot_size:
        return new_xml + b" " * (slot_size - len(new_xml))
    trimmed = new_xml.rstrip()
    if len(trimmed) <= slot_size:
        return trimmed + b" " * (slot_size - len(trimmed))
    return None


# ---------------------------------------------------------------------------
# Sidecar manifest fallback
# ---------------------------------------------------------------------------

def write_sidecar(pe_path: str) -> bool:
    """
    Write a <pe_path>.manifest sidecar file that declares Win10 compatibility.
    Wine loads this automatically and it takes priority over the embedded manifest.
    Returns True on success, False on failure.
    """
    sidecar_path = pe_path + ".manifest"
    try:
        with open(sidecar_path, "w", encoding="utf-8") as fh:
            fh.write(SIDECAR_MANIFEST)
        print(f"OK:    Wrote sidecar manifest → {sidecar_path!r}")
        return True
    except Exception as exc:
        print(f"ERROR: Could not write sidecar manifest {sidecar_path!r}: {exc}",
              file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# PE patching
# ---------------------------------------------------------------------------

def patch_pe(path: str) -> bool:
    try:
        import pefile  # type: ignore
    except ImportError:
        print(
            "ERROR: pefile is not installed.\n"
            "       Run: pip3 install pefile --break-system-packages",
            file=sys.stderr,
        )
        return False

    try:
        pe = pefile.PE(path, fast_load=True)
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]]
        )
    except Exception as exc:
        print(f"ERROR: Cannot parse PE {path!r}: {exc}", file=sys.stderr)
        return False

    if not hasattr(pe, "DIRECTORY_ENTRY_RESOURCE"):
        print(f"INFO:  No resource section in {path!r} — writing sidecar manifest")
        pe.close()
        return write_sidecar(path)

    with open(path, "rb") as fh:
        raw = bytearray(fh.read())

    patched_embedded = False
    slot_too_small    = False
    found_manifest    = False

    for res_type in pe.DIRECTORY_ENTRY_RESOURCE.entries:
        type_id   = getattr(res_type, "id",   None)
        type_name = str(getattr(res_type, "name", "") or "").upper()

        if type_id != RT_MANIFEST and type_name != "RT_MANIFEST":
            continue

        for res_id_entry in res_type.directory.entries:
            for lang_entry in res_id_entry.directory.entries:
                data_rva  = lang_entry.data.struct.OffsetToData
                slot_size = lang_entry.data.struct.Size

                try:
                    offset = pe.get_offset_from_rva(data_rva)
                except Exception:
                    continue

                found_manifest = True
                original = bytes(raw[offset: offset + slot_size])

                if has_win10_guid(original):
                    print(f"SKIP:  {path!r} already contains Win10/11 GUID")
                    pe.close()
                    return True

                new_xml = inject_guid(original)
                if new_xml is None:
                    print(f"WARN:  No insertion point in manifest of {path!r}",
                          file=sys.stderr)
                    slot_too_small = True
                    continue

                fitted = fit_to_slot(new_xml, slot_size)
                if fitted is None:
                    print(
                        f"INFO:  New manifest ({len(new_xml)} B) > slot ({slot_size} B) "
                        f"in {path!r} — will use sidecar manifest instead",
                        file=sys.stderr,
                    )
                    slot_too_small = True
                    continue

                raw[offset: offset + slot_size] = fitted
                patched_embedded = True

    pe.close()

    if patched_embedded:
        with open(path, "wb") as fh:
            fh.write(raw)
        print(f"OK:    Patched {path!r} with Windows 10/11 supportedOS GUID (embedded)")
        return True

    # Embedded patch wasn't possible — fall back to sidecar
    if slot_too_small or not found_manifest:
        print(f"INFO:  Falling back to sidecar manifest for {path!r}")
        return write_sidecar(path)

    print(f"SKIP:  No patchable RT_MANIFEST found in {path!r} — writing sidecar")
    return write_sidecar(path)


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
