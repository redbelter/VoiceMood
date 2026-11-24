$conns = Get-NetTCPConnection -LocalPort 5095 -State Listen
$conns | ForEach-Object {
    $p = Get-Process -Id $_.OwningProcess
    Write-Output ("pid " + $p.Id + " :: " + $p.ProcessName)
}
