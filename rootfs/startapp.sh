#!/bin/bash
set -x

# Define globals
local_version_file="${WINEPREFIX}dosdevices/c:/ProgramData/Backblaze/bzdata/bzreports/bzserv_version.txt"
install_exe_path="${WINEPREFIX}dosdevices/c:/"
log_file="${STARTUP_LOGFILE:-${WINEPREFIX}dosdevices/c:/backblaze-wine-startapp.log}"
custom_user_agent="backblaze-personal-wine (JonathanTreffler, +https://github.com/JonathanTreffler/backblaze-personal-wine-container), CFNetwork"

# Extracting variables from the PINNED_VERSION file
# Guard against a missing file so FORCE_LATEST_UPDATE=false doesn't silently
# operate with empty strings.
pinned_bz_version_file="/PINNED_BZ_VERSION"
if [ -f "$pinned_bz_version_file" ]; then
    pinned_bz_version=$(sed -n '1p' "$pinned_bz_version_file")
    pinned_bz_version_url=$(sed -n '2p' "$pinned_bz_version_file")
else
    echo "WARN: $pinned_bz_version_file not found — forcing FORCE_LATEST_UPDATE=true"
    pinned_bz_version=""
    pinned_bz_version_url=""
    FORCE_LATEST_UPDATE="true"
fi

export FORCE_LATEST_UPDATE="${FORCE_LATEST_UPDATE:-true}" #disable pinned version since URL is excluded from archive.org
export WINEARCH="win64"
export WINEDLLOVERRIDES="mscoree=" # Disable Mono installation

# FIX: tee to stdout so log_message output appears in `docker logs` as well as the file
log_message() {
    echo "$(date): $1" | tee -a "$log_file"
}

# Pre-initialize Wine
if [ ! -f "${WINEPREFIX}system.reg" ]; then
    echo "WINE: Wine not initialized, initializing"
    wineboot -i
    WINETRICKS_ACCEPT_EULA=1 winetricks -q -f dotnet48
    # Install the Visual C++ 2019 runtime so bzserv.exe's ServiceMain can use
    # native msvcp140/vcruntime140 rather than Wine's built-in stubs.  Without
    # this, C++ exception handling inside the service may behave differently and
    # prevent bzserv.exe from ever calling SetServiceStatus(SERVICE_RUNNING).
    WINETRICKS_ACCEPT_EULA=1 winetricks -q vcrun2019
    log_message "WINE: Initialization done"
fi

# Always enforce Windows 11 – Backblaze >= 9.4 rejects anything older.
# This also upgrades existing prefixes that were configured as Windows 8.
# winetricks is idempotent so running on every start is safe, just adds
# a few seconds. Do NOT remove this — it must run even on existing prefixes.
WINETRICKS_ACCEPT_EULA=1 winetricks -q win11
log_message "WINE: Windows version set to Windows 11"

# Directly set the Windows NT registry keys that Backblaze reads to determine
# the OS version.  winetricks win11 sets the Wine-internal "Version" value but
# some apps bypass that and read these keys from the NT hive directly.
# Build 22621 = Windows 11 22H2 (current supported release).
wine reg add "HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion" /v "CurrentMajorVersionNumber" /t REG_DWORD /d 10 /f 2>/dev/null
wine reg add "HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion" /v "CurrentMinorVersionNumber" /t REG_DWORD /d 0 /f 2>/dev/null
wine reg add "HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion" /v "CurrentBuildNumber" /t REG_SZ /d "22621" /f 2>/dev/null
wine reg add "HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion" /v "CurrentVersion" /t REG_SZ /d "10.0" /f 2>/dev/null
wine reg add "HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion" /v "ProductName" /t REG_SZ /d "Windows 11 Pro" /f 2>/dev/null
wine reg add "HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion" /v "EditionID" /t REG_SZ /d "Professional" /f 2>/dev/null
wine reg add "HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion" /v "InstallationType" /t REG_SZ /d "Client" /f 2>/dev/null
wine reg add "HKLM\\SYSTEM\\CurrentControlSet\\Control\\ProductOptions" /v "ProductType" /t REG_SZ /d "WinNT" /f 2>/dev/null
# Also set in ControlSet001 – in some Wine versions CurrentControlSet is not a
# true symlink so writes to CurrentControlSet don't propagate to ControlSet001,
# which is what wbemprox may read when evaluating Win32_OperatingSystem.ProductType.
wine reg add "HKLM\\SYSTEM\\ControlSet001\\Control\\ProductOptions" /v "ProductType" /t REG_SZ /d "WinNT" /f 2>/dev/null
log_message "WINE: Windows 11 NT registry keys enforced (build 22621)"

# Per-app version overrides – belt-and-suspenders in case an old AppDefaults
# entry in the prefix overrides the global "Version" key for any Backblaze exe.
wine reg add "HKCU\\Software\\Wine\\AppDefaults\\install_backblaze.exe" /v "Version" /t REG_SZ /d "win11" /f 2>/dev/null
wine reg add "HKCU\\Software\\Wine\\AppDefaults\\bzbui.exe" /v "Version" /t REG_SZ /d "win11" /f 2>/dev/null
wine reg add "HKCU\\Software\\Wine\\AppDefaults\\bzserv.exe" /v "Version" /t REG_SZ /d "win11" /f 2>/dev/null
wine reg add "HKCU\\Software\\Wine\\AppDefaults\\bzmenu.exe" /v "Version" /t REG_SZ /d "win11" /f 2>/dev/null

# Set the global Wine version so every subprocess (including installer child
# processes) sees Windows 11.  winetricks win11 is supposed to do this via
# HKCU\Software\Wine\Version but is not reliably persisting the key.
wine reg add "HKCU\\Software\\Wine" /v "Version" /t REG_SZ /d "win11" /f 2>/dev/null
log_message "WINE: Global Wine version forced to win11"

# Hide Wine's ntdll exports (wine_get_version, wine_get_build_id, etc.) so
# that Backblaze cannot detect it is running under Wine and refuse to start.
wine reg add "HKCU\\Software\\Wine" /v "HideWineExports" /t REG_DWORD /d 1 /f 2>/dev/null
log_message "WINE: HideWineExports set (Wine detection disabled)"

#Configure Extra Mounts
for x in {d..z}
do
    if test -d "/drive_${x}" && ! test -d "${WINEPREFIX}dosdevices/${x}:"; then
        log_message "DRIVE: drive_${x} found but not mounted, mounting..."
        ln -s "/drive_${x}/" "${WINEPREFIX}dosdevices/${x}:"
    fi
done

# Set Virtual Desktop
# FIX: quote $WINEPREFIX to handle paths with spaces
cd "$WINEPREFIX" || { log_message "ERROR: Cannot cd to WINEPREFIX ($WINEPREFIX)"; exit 1; }
if [ "$DISABLE_VIRTUAL_DESKTOP" = "true" ]; then
    log_message "WINE: DISABLE_VIRTUAL_DESKTOP=true - Virtual Desktop mode will be disabled"
    winetricks vd=off
else
    # Check if width and height are defined
    if [ -n "$DISPLAY_WIDTH" ] && [ -n "$DISPLAY_HEIGHT" ]; then
        log_message "WINE: Enabling Virtual Desktop mode with $DISPLAY_WIDTH:$DISPLAY_HEIGHT aspect ratio"
        winetricks vd="${DISPLAY_WIDTH}x${DISPLAY_HEIGHT}"
    else
        # Default aspect ratio
        log_message "WINE: Enabling Virtual Desktop mode with recommended aspect ratio"
        winetricks vd="900x700"
    fi
fi

# Disclaimer
    # Check if auto-updates are disabled
if [ "$DISABLE_AUTOUPDATE" = "true" ]; then
    echo "Auto-updates are disabled. Backblaze won't be updated."
else
    # Check the status of FORCE_LATEST_UPDATE
    if [ "$FORCE_LATEST_UPDATE" = "true" ]; then
        echo "FORCE_LATEST_UPDATE is enabled which may brick your installation."
    else
        echo "FORCE_LATEST_UPDATE is disabled. Using known-good version of Backblaze."
    fi
fi

# Function to handle errors
handle_error() {
    echo "Error: $1" | tee -a "$log_file"
    start_app # Start app even if there is a problem with the updater
}

fetch_and_install() {
    # Unlock bzupdates in case a previous start_app run locked it to chmod 555.
    # The Backblaze installer writes there during upgrades; a locked directory
    # causes the installer to fail with a non-zero exit code.
    local bz_updates_dir="${WINEPREFIX}drive_c/Program Files (x86)/Backblaze/bzupdates"
    if [ -d "$bz_updates_dir" ]; then
        chmod 755 "$bz_updates_dir"
        log_message "INSTALLER: bzupdates unlocked for installer run"
    fi

    cd "$install_exe_path" || handle_error "INSTALLER: can't navigate to $install_exe_path"
    if [ "$FORCE_LATEST_UPDATE" = "true" ]; then
        log_message "INSTALLER: FORCE_LATEST_UPDATE=true - downloading latest version"
        curl -L "https://www.backblaze.com/win32/install_backblaze.exe" --output "install_backblaze.exe"
    else
        log_message "INSTALLER: FORCE_LATEST_UPDATE=false - downloading pinned version $pinned_bz_version from archive.org"
        curl -A "$custom_user_agent" -L "$pinned_bz_version_url" --output "install_backblaze.exe" || handle_error "INSTALLER: error downloading from $pinned_bz_version_url"
    fi

    # Patch the embedded RT_MANIFEST resource in the installer PE and in all
    # existing Backblaze executables to add the Windows 10 <supportedOS> GUID.
    #
    # Wine 10+ implements the Windows 8.1+ GetVersionEx compatibility shim:
    # any executable whose embedded manifest lacks a Win10 <supportedOS> GUID
    # sees version 6.2 from GetVersionEx, causing Backblaze to abort with
    # "MajorVerTooOld".  The outer installer (install_backblaze.exe) passes
    # once patched, but it immediately invokes bzdoinstall.exe (and other
    # helpers) from the existing 9.x installation to perform the upgrade.
    # Those binaries have no Win10 manifest and also see 6.2, so we must
    # pre-patch every .exe in the existing Backblaze directory as well.
    local _patcher=/usr/local/bin/patch_pe_manifest.py

    if [ ! -f "$_patcher" ]; then
        log_message "INSTALLER: WARNING — $_patcher not found, skipping PE manifest patching"
    else
        # 1. Patch the freshly downloaded installer.
        if python3 "$_patcher" "install_backblaze.exe"; then
            log_message "INSTALLER: PE manifest patch succeeded (embedded or sidecar)"
        else
            log_message "INSTALLER: WARNING — manifest patch failed for install_backblaze.exe, MajorVerTooOld likely"
        fi

        # 2. Pre-patch every .exe in the existing Backblaze installation so that
        #    child processes launched by the installer also see Windows 10.
        local _bz_dir="${WINEPREFIX}drive_c/Program Files (x86)/Backblaze"
        if [ -d "$_bz_dir" ]; then
            log_message "INSTALLER: pre-patching existing Backblaze executables for Win10 manifest"
            while IFS= read -r -d '' _exe; do
                python3 "$_patcher" "$_exe" 2>/dev/null
            done < <(find "$_bz_dir" -name "*.exe" -print0)
            log_message "INSTALLER: pre-patching done"
        fi
    fi

    log_message "INSTALLER: Starting install_backblaze.exe"
    # Run without silent flags – Backblaze's installer is a custom PE, not
    # NSIS/Inno, and /S is not a recognised flag (causes immediate exit 9).
    installer_debug_log="/config/install-debug.log"
    # Rotate log so each run produces a clean file (prevents unbounded growth
    # and makes it easy to grep the log for the most recent install attempt).
    mv -f "$installer_debug_log" "${installer_debug_log}.prev" 2>/dev/null || true
    WINEDEBUG=-all,+ver,+wbemprox WINEARCH="$WINEARCH" WINEPREFIX="$WINEPREFIX" \
        wine "install_backblaze.exe" 2>"$installer_debug_log" \
        || handle_error "INSTALLER: Failed to install Backblaze"
}

start_app() {
    # Lock the bzupdates directory so Backblaze cannot download and run its own
    # internal updater.  Without this, the updater kills bzbui.exe mid-run and
    # the new installer hangs under Wine, leaving the VNC screen black.
    local bz_updates_dir="${WINEPREFIX}drive_c/Program Files (x86)/Backblaze/bzupdates"
    mkdir -p "$bz_updates_dir"
    chmod 555 "$bz_updates_dir"
    log_message "STARTAPP: bzupdates directory locked (preventing Backblaze self-update)"

    log_message "STARTAPP: Starting Backblaze version $(cat "$local_version_file" 2>/dev/null || echo unknown)"

    # Watchdog loop: restart bzbui.exe whenever it exits.  This handles the
    # case where the app is killed by an (already-blocked) internal update
    # attempt or any other unexpected exit.
    # NOTE: this loop never returns — handle_error relies on this behaviour.
    while true; do
        wine "${WINEPREFIX}drive_c/Program Files (x86)/Backblaze/bzbui.exe" -noquiet
        log_message "STARTAPP: bzbui.exe exited (code $?), restarting in 10 seconds..."
        sleep 10
    done
}

if [ -f "${WINEPREFIX}drive_c/Program Files (x86)/Backblaze/bzbui.exe" ]; then
    check_url_validity() {
        url="$1"
        if http_code=$(curl -s -o /dev/null -w "%{http_code}" "$url"); then
            if [ "$http_code" -eq 200 ]; then
                content_type=$(curl -s -I "$url" | grep -i content-type | cut -d ':' -f2)
                if echo "$content_type" | grep -q "xml"; then
                    return 0 # Valid XML content found
                fi
            fi
        fi
        return 1 # Invalid or unavailable content
    }

    compare_versions() {
        local_version="$1"
        compare_version="$2"

        if dpkg --compare-versions "$local_version" lt "$compare_version"; then
            return 0 # The compare_version is higher
        else
            return 1 # The local version is higher or equal
        fi
    }

    # Check if auto-updates are disabled
    if [ "$DISABLE_AUTOUPDATE" = "true" ]; then
        log_message "UPDATER: DISABLE_AUTOUPDATE=true, Auto-updates are disabled. Starting Backblaze without updating."
        start_app
    fi

    # Update process for force_latest_update set to true or not set
    if [ "$FORCE_LATEST_UPDATE" = "true" ]; then
        # Main auto update logic
        if [ -f "$local_version_file" ]; then
            log_message "UPDATER: FORCE_LATEST_UPDATE=true, checking for a new version"
            urls="
                https://ca000.backblaze.com/api/clientversion.xml
                https://ca001.backblaze.com/api/clientversion.xml
                https://ca002.backblaze.com/api/clientversion.xml
                https://ca003.backblaze.com/api/clientversion.xml
                https://ca004.backblaze.com/api/clientversion.xml
                https://ca005.backblaze.com/api/clientversion.xml
            "

            for url in $urls; do
                if check_url_validity "$url"; then
                    xml_content=$(curl -s "$url") || handle_error "UPDATER: Failed to fetch XML content"
                    xml_version=$(echo "$xml_content" | grep -o '<update win32_version="[0-9.]*"' | cut -d'"' -f2)
                    local_version=$(cat "$local_version_file") || handle_error "UPDATER: Failed to read local version from $local_version_file"
                    log_message "UPDATER: Installed Version=$local_version"
                    log_message "UPDATER: Latest Version=$xml_version"
                    if compare_versions "$local_version" "$xml_version"; then
                        log_message "UPDATER: Newer version found - downloading and installing the newer version..."
                        fetch_and_install
                        start_app # Exit after successful download+installation and start app
                    else
                        log_message "UPDATER: The installed version is up to date."
                        start_app # Exit autoupdate and start app
                    fi
                fi
            done

            handle_error "No valid XML content found or all URLs are unavailable."
        else
            handle_error "Local version file not found. Exiting."
        fi
    else
        # Update process for force_latest_update set to false or anything else
        if [ -f "$local_version_file" ]; then
            local_version=$(cat "$local_version_file") || handle_error "UPDATER: Failed to read local version file"
            log_message "UPDATER: FORCE_LATEST_UPDATE=false"
            log_message "UPDATER: Installed Version=$local_version"
            log_message "UPDATER: Pinned Version=$pinned_bz_version"

            if compare_versions "$local_version" "$pinned_bz_version"; then
                log_message "UPDATER: Newer version found - downloading and installing the newer version..."
                fetch_and_install
                start_app # Exit after successful download+installation and start app
            else
                log_message "UPDATER: Installed version is up to date. There may be a newer version available when using FORCE_LATEST_UPDATE=true"
                start_app # Exit autoupdate and start app
            fi
        else
            handle_error "UPDATER: Local version file does not exist. Exiting updater."
        fi
    fi
else # Client currently not installed
    fetch_and_install
    start_app
fi
