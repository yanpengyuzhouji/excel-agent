# Run as Administrator after explicitly enabling all-interface Docker publishing.
$ErrorActionPreference = 'Stop'
$taskName = 'ExcelWorkbook-Docker-LAN'
if (-not (Get-NetFirewallRule -Name $taskName -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name $taskName -DisplayName 'Excel Docker LAN ports' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8091,8092 -RemoteAddress LocalSubnet -Profile Any | Out-Null
}
if (Get-Command New-NetFirewallHyperVRule -ErrorAction SilentlyContinue) {
    $taskVm = Get-NetFirewallHyperVVMCreator | Where-Object FriendlyName -eq 'WSL'
    if ($taskVm -and -not (Get-NetFirewallHyperVRule -Name 'ExcelWorkbook-WSL-Docker-LAN' -ErrorAction SilentlyContinue)) {
        New-NetFirewallHyperVRule -Name 'ExcelWorkbook-WSL-Docker-LAN' -DisplayName 'Excel Docker WSL LAN ports' -Direction Inbound -VMCreatorId $taskVm.VMCreatorId -Protocol TCP -LocalPorts 8091,8092 -RemoteAddresses '192.168.0.0/16','10.0.0.0/8','172.16.0.0/12' -Action Allow | Out-Null
    }
}
