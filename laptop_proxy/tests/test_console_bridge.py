import ast
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from console_bridge import (
    BridgeError,
    DISABLE_ICS,
    ENABLE_ICS,
    HOTSPOT_INFO,
    IcsManager,
    LIST_ETHERNET,
    START_HOTSPOT,
    STOP_HOTSPOT,
)


GUID = "4676621A-5C20-4942-A1C1-584C7F7BD4C2"
VPN_GUID = "4B87E160-0000-4000-8000-000000000001"
OLD_GUID = "4B87E160-0000-4000-8000-000000000002"


class FakePowerShell:
    def __init__(self, *, hotspot_started=False):
        self.hotspot_started = hotspot_started
        self.calls = []
        self.fail_on = set()

    def __call__(self, command, **kwargs):
        # v1.3.1: console_bridge passes encoding/errors kwargs now (mbcs decode
        # for localized Windows output); the fake returns str regardless.
        script = command[-1]
        self.calls.append((script, dict(kwargs.get("env") or {})))
        if script in self.fail_on:
            return subprocess.CompletedProcess(command, 1, "", "Windows operation failed")
        if script == LIST_ETHERNET:
            result = [{"guid": GUID, "name": "Ethernet"}]
        elif script == HOTSPOT_INFO:
            result = {"ssid": "Console Bridge", "password": "test-password"}
        elif script == START_HOTSPOT:
            result = {
                "guid": GUID, "name": "Local Area Connection* 4",
                "ssid": "Console Bridge", "password": "test-password",
                "started": self.hotspot_started,
            }
        elif script == ENABLE_ICS:
            result = {
                "source_guid": VPN_GUID,
                "output_guid": GUID,
                "output_name": "Ethernet",
                "previous": [
                    {"guid": OLD_GUID, "type": 0},
                    {"guid": GUID, "type": 1},
                ],
                "owned": True,
            }
        elif script == DISABLE_ICS:
            result = {"restored": True}
        elif script == STOP_HOTSPOT:
            result = {"stopped": True}
        else:
            raise AssertionError("Unexpected PowerShell script")
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")


def test_ethernet_bridge_restores_previous_sharing():
    runner = FakePowerShell()
    bridge = IcsManager(runner=runner)
    assert bridge.list_ethernet()[0].name == "Ethernet"

    details = bridge.start("ethernet", GUID)
    assert details.output_name == "Ethernet"
    assert bridge.active
    assert [call[0] for call in runner.calls].count(ENABLE_ICS) == 1

    bridge.stop()
    assert not bridge.needs_cleanup
    restore_env = [env for script, env in runner.calls if script == DISABLE_ICS][0]
    previous = json.loads(restore_env["CONSOLE_BRIDGE_STATE"])["previous"]
    assert previous == [{"guid": OLD_GUID, "type": 0}, {"guid": GUID, "type": 1}]
    assert not any(script == STOP_HOTSPOT for script, _ in runner.calls)


def test_disconnected_ethernet_cannot_change_ics():
    runner = FakePowerShell()
    bridge = IcsManager(runner=runner)
    with pytest.raises(BridgeError, match="not connected"):
        bridge.start("ethernet", "00000000-0000-4000-8000-000000000001")
    assert all(script != ENABLE_ICS for script, _ in runner.calls)


def test_hotspot_is_stopped_if_ics_fails():
    runner = FakePowerShell(hotspot_started=True)
    runner.fail_on.add(ENABLE_ICS)
    bridge = IcsManager(runner=runner)
    with pytest.raises(BridgeError, match="Windows operation failed"):
        bridge.start("wifi")
    assert not bridge.needs_cleanup
    assert [script for script, _ in runner.calls][-1] == STOP_HOTSPOT


def test_existing_hotspot_is_left_running_on_stop():
    runner = FakePowerShell(hotspot_started=False)
    bridge = IcsManager(runner=runner)
    details = bridge.start("wifi")
    assert details.ssid == "Console Bridge"
    bridge.stop()
    assert not any(script == STOP_HOTSPOT for script, _ in runner.calls)


def test_failed_restore_can_be_retried_before_hotspot_stops():
    runner = FakePowerShell(hotspot_started=True)
    bridge = IcsManager(runner=runner)
    bridge.start("wifi")
    runner.fail_on.add(DISABLE_ICS)
    with pytest.raises(BridgeError):
        bridge.stop()
    assert bridge.needs_cleanup
    assert not any(script == STOP_HOTSPOT for script, _ in runner.calls)
    runner.fail_on.clear()
    bridge.stop()
    assert not bridge.needs_cleanup
    assert [script for script, _ in runner.calls][-1] == STOP_HOTSPOT


def test_windows_client_source_parses():
    ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8"))


MOCK_ICS = r"""
$ErrorActionPreference = 'Stop'
function New-FakeConnection($guid, $name, $enabled, $type, $failEnable = $false) {
    $config = [pscustomobject]@{
        SharingEnabled = [bool]$enabled
        SharingConnectionType = [int]$type
        FailEnable = [bool]$failEnable
    }
    $config | Add-Member -MemberType ScriptMethod -Name EnableSharing -Value {
        param($newType)
        if ($this.FailEnable) { throw 'simulated sharing failure' }
        $this.SharingEnabled = $true
        $this.SharingConnectionType = [int]$newType
    }
    $config | Add-Member -MemberType ScriptMethod -Name DisableSharing -Value {
        $this.SharingEnabled = $false
    }
    return [pscustomobject]@{
        Guid = $guid
        Name = $name
        DeviceName = $(if ($guid -eq $env:TEST_VPN_GUID) { 'tun2socks Tunnel' } else { $name })
        Config = $config
    }
}
$script:connections = @(
    New-FakeConnection $env:TEST_VPN_GUID 'LinkHelm' $false -1
    New-FakeConnection $env:TEST_OLD_GUID 'Existing connection' $true 0
    New-FakeConnection $env:TEST_OUTPUT_GUID 'Ethernet' $true 1
)
$script:manager = [pscustomobject]@{ EnumEveryConnection = $script:connections }
$script:manager | Add-Member -MemberType ScriptMethod -Name NetConnectionProps -Value {
    param($connection)
    return [pscustomobject]@{ Guid = $connection.Guid; Name = $connection.Name; DeviceName = $connection.DeviceName }
}
$script:manager | Add-Member -MemberType ScriptMethod -Name INetSharingConfigurationForINetConnection -Value {
    param($connection)
    return $connection.Config
}
function New-Object {
    param([string]$ComObject)
    if ($ComObject -ne 'HNetCfg.HNetShare') { throw 'Unexpected COM request' }
    return $script:manager
}
function Get-NetAdapter {
    param([string]$Name, [switch]$IncludeHidden, [string]$ErrorAction)
    if ($Name) {
        return [pscustomobject]@{ Status = 'Up'; InterfaceGuid = $env:TEST_VPN_GUID }
    }
    # v1.5.46: real code now looks BOTH adapters up by GUID over -IncludeHidden.
    return @(
        [pscustomobject]@{ Status = 'Up'; InterfaceGuid = $env:TEST_VPN_GUID },
        [pscustomobject]@{ Status = 'Up'; InterfaceGuid = $env:TEST_OUTPUT_GUID }
    )
}
function Get-Snapshot {
    return @($script:connections | ForEach-Object {
        @{ name = $_.Name; enabled = $_.Config.SharingEnabled; type = $_.Config.SharingConnectionType }
    })
}
"""


def run_mock_ics(*, wrong_private=False, fail_enable=False, fail_restore=False, no_prior_sharing=False,
                 output_guid=None, tun_name=None):
    import os
    env = os.environ.copy()
    env.update({
        "TEST_VPN_GUID": VPN_GUID,
        "TEST_OLD_GUID": OLD_GUID,
        "TEST_OUTPUT_GUID": GUID,
        "CONSOLE_OUTPUT_GUID": output_guid or GUID,
    })
    setup = MOCK_ICS
    if tun_name:
        setup += f"$script:connections[0].Name = '{tun_name}'\n"
        setup += "$script:manager.EnumEveryConnection = $script:connections\n"
    if no_prior_sharing:
        setup += "$script:connections[1].Config.SharingEnabled = $false\n"
        setup += "$script:connections[2].Config.SharingEnabled = $false\n"
    if wrong_private:
        setup += "$script:connections[2].Guid = '00000000-0000-4000-8000-000000000003'\n"
        setup += "$script:connections += @(New-FakeConnection $env:TEST_OUTPUT_GUID 'Selected Ethernet' $false -1)\n"
        setup += "$script:manager.EnumEveryConnection = $script:connections\n"
    if fail_enable:
        setup += "$script:connections[0].Config.FailEnable = $true\n"
    if fail_restore:
        setup += "$script:connections[1].Config.FailEnable = $true\n"
    ps = setup + "\ntry {\n  $enabled = & {\n" + ENABLE_ICS + "\n  } | ConvertFrom-Json\n"
    ps += "  if ($enabled -and -not $enabled.error) {\n    $env:CONSOLE_BRIDGE_STATE = $enabled | ConvertTo-Json -Compress -Depth 5\n"
    ps += "    $disabled = & {\n" + DISABLE_ICS + "\n    } | ConvertFrom-Json\n  }\n"
    ps += "  $errorText = ''\n} catch { $errorText = $_.Exception.Message }\n"
    ps += "@{ error = $errorText; partialError = [string]$enabled.error; enabled = [bool]$enabled; "
    ps += "restored = [bool]$disabled; state = (Get-Snapshot) } | ConvertTo-Json -Compress -Depth 6\n"
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        # v1.3.1: localized PowerShell emits GBK; default UTF-8 decode crashed.
        capture_output=True, encoding="mbcs", errors="replace",
        env=env, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell COM simulation requires Windows")
def test_ics_scripts_replace_and_restore_only_selected_pair():
    result = run_mock_ics()
    assert result["error"] == ""
    assert result["enabled"] and result["restored"]
    state = {entry["name"]: entry for entry in result["state"]}
    assert not state["LinkHelm"]["enabled"]
    assert state["Existing connection"]["enabled"] and state["Existing connection"]["type"] == 0
    assert state["Ethernet"]["enabled"] and state["Ethernet"]["type"] == 1


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell COM simulation requires Windows")
def test_ics_scripts_leave_unrelated_sharing_untouched():
    result = run_mock_ics(wrong_private=True)
    assert "already in use" in result["error"]
    assert not result["enabled"]
    state = {entry["name"]: entry for entry in result["state"]}
    assert not state["LinkHelm"]["enabled"]
    assert state["Existing connection"]["enabled"]
    assert state["Ethernet"]["enabled"]


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell COM simulation requires Windows")
def test_ics_scripts_rollback_on_enable_failure():
    result = run_mock_ics(fail_enable=True)
    assert "simulated sharing failure" in result["error"]
    assert not result["enabled"]
    state = {entry["name"]: entry for entry in result["state"]}
    assert not state["LinkHelm"]["enabled"]
    assert state["Existing connection"]["enabled"] and state["Existing connection"]["type"] == 0
    assert state["Ethernet"]["enabled"] and state["Ethernet"]["type"] == 1


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell COM simulation requires Windows")
def test_ics_script_reports_recoverable_partial_rollback():
    result = run_mock_ics(fail_enable=True, fail_restore=True)
    assert "Restoration also failed" in result["partialError"]
    assert not result["restored"]


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell COM simulation requires Windows")
def test_ics_skipped_without_error_when_output_missing():
    # v1.5.46: cloned-NIC machines (Realtek Wi-Fi 7 / Win11) never expose the
    # hotspot adapter in the COM sharing enumeration. Sharing must still
    # succeed via tethering NAT instead of failing + hotspot rollback (the
    # rollback churn knocked the STA off the Wi-Fi Direct group every retry).
    result = run_mock_ics(output_guid="11111111-2222-4333-8444-555555555555")
    assert result["error"] == ""
    assert result["partialError"] == ""
    assert result["enabled"]
    assert result["restored"]  # DISABLE_ICS with owned=false must be a no-op


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell COM simulation requires Windows")
def test_tun_source_matched_by_device_name_when_connection_name_drifted():
    # v1.5.46: NetCfg connection name drifts to 'LinkHelm 31' after historic
    # collisions; the device description ('tun2socks Tunnel') is stable.
    result = run_mock_ics(tun_name="LinkHelm 31")
    assert result["error"] == ""
    assert result["enabled"] and result["restored"]


def test_failed_hotspot_cleanup_keeps_retry_state():
    runner = FakePowerShell(hotspot_started=True)
    runner.fail_on.update({ENABLE_ICS, STOP_HOTSPOT})
    bridge = IcsManager(runner=runner)
    with pytest.raises(BridgeError, match="Hotspot cleanup also failed"):
        bridge.start("wifi")
    assert bridge.needs_cleanup
    runner.fail_on.clear()
    bridge.stop()
    assert not bridge.needs_cleanup


def test_partial_ics_response_is_cleaned_up_before_error():
    class PartialRunner(FakePowerShell):
        def __call__(self, command, **kwargs):
            if command[-1] == ENABLE_ICS:
                result = {
                    "error": "Sharing setup failed; restoration also failed",
                    "source_guid": VPN_GUID,
                    "output_guid": GUID,
                    "output_name": "Ethernet",
                    "previous": [{"guid": OLD_GUID, "type": 0}, {"guid": GUID, "type": 1}],
                    "owned": True,
                }
                self.calls.append((command[-1], dict(kwargs["env"])))
                return subprocess.CompletedProcess(command, 0, json.dumps(result), "")
            return super().__call__(command, **kwargs)

    runner = PartialRunner()
    bridge = IcsManager(runner=runner)
    with pytest.raises(BridgeError, match="restoration also failed"):
        bridge.start("ethernet", GUID)
    assert [script for script, _ in runner.calls][-1] == DISABLE_ICS
    assert not bridge.needs_cleanup


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell COM simulation requires Windows")
def test_ics_scripts_start_and_stop_without_previous_sharing():
    result = run_mock_ics(no_prior_sharing=True)
    assert result["error"] == ""
    assert result["enabled"] and result["restored"]
    assert not any(entry["enabled"] for entry in result["state"])


def test_hotspot_start_error_retries_cleanup_when_windows_started_it():
    class StartupFailure(FakePowerShell):
        def __call__(self, command, **kwargs):
            if command[-1] == START_HOTSPOT:
                self.calls.append((command[-1], dict(kwargs["env"])))
                return subprocess.CompletedProcess(
                    command, 0, json.dumps({"error": "Hotspot unsupported", "started": True}), ""
                )
            return super().__call__(command, **kwargs)

    runner = StartupFailure()
    bridge = IcsManager(runner=runner)
    with pytest.raises(BridgeError, match="Hotspot unsupported"):
        bridge.start("wifi")
    assert [script for script, _ in runner.calls][-1] == STOP_HOTSPOT
    assert not bridge.needs_cleanup


def test_hotspot_credentials_are_read_without_starting_sharing():
    runner = FakePowerShell()
    bridge = IcsManager(runner=runner)
    assert bridge.hotspot_info() == ("Console Bridge", "test-password")
    assert [script for script, _ in runner.calls] == [HOTSPOT_INFO]
    assert not bridge.needs_cleanup


# ── v1.5.45: robust tethering-adapter identification ────────────────────
# Field bug (Win11 + Realtek 8922AE Wi-Fi 7): tethering CLONES the physical
# NIC ("WLAN 4") and NO 'Microsoft Wi-Fi Direct Virtual Adapter' ever exists,
# so the old exactly-one-virtual-adapter check always threw. Also, PS 5.1
# CimInstance has no .Count — an unwrapped function return made the count
# null and the check failed even after finding the adapter.
def test_start_hotspot_identifies_cloned_nic_by_gateway_ip():
    assert "Find-TetheringAdapter" in START_HOTSPOT
    # A) classic Wi-Fi Direct virtual adapter path stays first-choice
    assert "*Microsoft Wi-Fi Direct Virtual Adapter*" in START_HOTSPOT
    # B) cloned-NIC path: NetworkOperatorTethering's well-known gateway IP
    assert "192.168.137.1" in START_HOTSPOT
    # the call site must re-wrap the return stream, or .Count is null
    assert "@(Find-TetheringAdapter)" in START_HOTSPOT


def test_stop_hotspot_never_throws_on_missing_profile():
    # The old script threw 'Cannot find the Windows Mobile Hotspot
    # connection profile.' during teardown — a missing profile (TUN default
    # route / NLA churn) wedged the cleanup with the hotspot still On.
    assert "Cannot find the Windows Mobile Hotspot connection profile" not in STOP_HOTSPOT
    assert "stopped = $false" in STOP_HOTSPOT or "$stopped" in STOP_HOTSPOT
    assert "StartTetheringAsync" not in STOP_HOTSPOT
    assert "StopTetheringAsync" in STOP_HOTSPOT
