#!/usr/bin/env python3
"""
Patch dlls/kernelbase/version.c to disable the GetVersionEx compatibility lie.

Wine 8+ implements a compatibility lie in init_current_version(): any process
whose manifest lacks the Win10 <supportedOS> GUID sees version 6.2 from
GetVersionEx instead of the true 10.0.  This breaks Backblaze when the
installer extracts fresh binaries at runtime – those binaries don't exist on
disk before extraction so we cannot pre-patch their manifests.

The fix: insert 'return TRUE;' immediately after the RtlGetVersion() call
succeeds, skipping the entire manifest-checking / version-capping block.
"""
import sys
import pathlib

path = pathlib.Path('dlls/kernelbase/version.c')

NEEDLE = 'if (!set_ntstatus( RtlGetVersion(&current_version) )) return FALSE;'
SENTINEL = 'disable GetVersionEx compat lie'
INSERTION = (
    '\n'
    '    /* Backblaze compat: disable the GetVersionEx compatibility lie so every\n'
    '     * process (including binaries the installer extracts at runtime from its\n'
    '     * embedded cabinet) receives the true OS version rather than the 6.2 stub.\n'
    '     * Processes without a Win10 <supportedOS> manifest GUID would otherwise\n'
    '     * abort with "MajorVerTooOld". */\n'
    '    return TRUE; /* disable GetVersionEx compat lie */'
)

content = path.read_text()

if SENTINEL in content:
    print(f'{path}: already patched, skipping')
    sys.exit(0)

if NEEDLE not in content:
    print(f'ERROR: target line not found in {path}', file=sys.stderr)
    print(f'  Looking for: {NEEDLE!r}', file=sys.stderr)
    sys.exit(1)

patched = content.replace(NEEDLE, NEEDLE + INSERTION, 1)
path.write_text(patched)
print(f'{path}: patched (GetVersionEx compat lie disabled)')
