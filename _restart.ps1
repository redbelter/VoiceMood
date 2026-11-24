$conns = Get-NetTCPConnection -LocalPort 5095 -State Listen
$conns | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
Start-Sleep 1
Start-Process -WindowStyle Hidden -FilePath 'C:\Users\red\stream\tools\voice-mood\.venv\Scripts\python.exe' -ArgumentList 'C:\Users\red\stream\tools\voice-mood\voice_mood.py'
Start-Sleep 4
$r = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:5095/status
Write-Output $r.Content.Substring(0, [Math]::Min(160, $r.Content.Length))
