# Run in PowerShell as Administrator (on Windows host, not inside WSL).
# Forwards LAN access to http://192.168.60.51:8765 → WSL Docker on port 8765.
$listenPort = 8765
$wslIp = (wsl.exe hostname -I).Trim().Split(" ")[0]
if (-not $wslIp) { Write-Error "Could not get WSL IP"; exit 1 }

netsh interface portproxy delete v4tov4 listenport=$listenPort listenaddress=0.0.0.0 2>$null
netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=$listenPort connectaddress=$wslIp connectport=$listenPort
netsh advfirewall firewall delete rule name="VscCam" 2>$null
netsh advfirewall firewall add rule name="VscCam" dir=in action=allow protocol=TCP localport=$listenPort

Write-Host "Port proxy: 0.0.0.0:${listenPort} -> ${wslIp}:${listenPort}"
Write-Host "Open from phone/TV: http://192.168.60.51:${listenPort} (use your PC's Wi-Fi IP if different)"
