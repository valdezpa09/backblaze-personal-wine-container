#!/usr/bin/env python3
"""
Patch dlls/mountmgr.sys/device.c to return synthetic disk extents for
IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS so Backblaze treats Wine-mounted
drives as connected physical disks rather than "unplugged".

Uses string matching instead of a unified diff to avoid line-number
sensitivity across Wine versions.
"""
import sys
import pathlib

path = pathlib.Path('dlls/mountmgr.sys/device.c')

SENTINEL = 'Return synthetic extents so callers (e.g. Backblaze)'

# The FIXME line that identifies the old stub implementation.
FIXME_LINE = 'FIXME( "returning zero-filled buffer for IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS'

# Old stub – everything that needs to be replaced (from the DWORD len line
# through the memset/Information assignment, excluding the status = SUCCESS line
# which is shared with the new code).
OLD = (
    '        DWORD len = min( 32, irpsp->Parameters.DeviceIoControl.OutputBufferLength );\n'
    '\n'
    '        FIXME( "returning zero-filled buffer for IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS\\n" );\n'
    '        memset( irp->AssociatedIrp.SystemBuffer, 0, len );\n'
    '        irp->IoStatus.Information = len;\n'
)

NEW = (
    '        VOLUME_DISK_EXTENTS *ext = irp->AssociatedIrp.SystemBuffer;\n'
    '        DWORD outsize = irpsp->Parameters.DeviceIoControl.OutputBufferLength;\n'
    '\n'
    '        if (outsize < sizeof(*ext))\n'
    '        {\n'
    '            irp->IoStatus.Information = sizeof(*ext);\n'
    '            status = STATUS_BUFFER_TOO_SMALL;\n'
    '            break;\n'
    '        }\n'
    '        /* Return synthetic extents so callers (e.g. Backblaze) treat the\n'
    '         * drive as a connected physical disk rather than "unplugged". */\n'
    '        ext->NumberOfDiskExtents = 1;\n'
    '        ext->Extents[0].DiskNumber = dev->devnum.DeviceNumber;\n'
    '        ext->Extents[0].StartingOffset.QuadPart = 0;\n'
    '        ext->Extents[0].ExtentLength.QuadPart = (LONGLONG)10000 * 255 * 63 * 512;\n'
    '        irp->IoStatus.Information = sizeof(*ext);\n'
)

content = path.read_text()

if SENTINEL in content:
    print(f'{path}: already patched, skipping')
    sys.exit(0)

if FIXME_LINE not in content:
    print(f'{path}: FIXME stub not found – Wine may have already fixed this IOCTL.')
    print(f'  Skipping mountmgr.sys disk-extents patch.')
    sys.exit(0)

if OLD not in content:
    print(f'ERROR: full old stub block not found in {path}', file=sys.stderr)
    print(f'  The code structure around IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS has', file=sys.stderr)
    print(f'  changed beyond what this script can handle. Update patches/patch_mountmgr_disk_extents.py.', file=sys.stderr)
    sys.exit(1)

patched = content.replace(OLD, NEW, 1)
path.write_text(patched)
print(f'{path}: patched (synthetic disk extents for Backblaze enabled)')
