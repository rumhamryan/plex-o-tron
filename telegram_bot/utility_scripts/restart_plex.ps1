# This script's sole purpose is to restart the Plex Media Server on Windows.

Write-Host "Wrapper script initiated. Attempting to restart Plex..."

# Attempt to restart the service. Note: This requires the script to be run with Administrator privileges.
# The default service name is usually "Plex Media Server" if installed as a service.
$ServiceName = "Plex Media Server"

if (Get-Service $ServiceName -ErrorAction SilentlyContinue) {
    Write-Host "Restarting service: $ServiceName"
    Restart-Service -Name $ServiceName -Force
    Write-Host "Plex restart command sent."
} else {
    Write-Host "Service '$ServiceName' not found. Checking for process..."
    $Process = Get-Process "Plex Media Server" -ErrorAction SilentlyContinue
    if ($Process) {
        Write-Host "Stopping process..."
        $Process | Stop-Process -Force

        # We try to find the executable to start it again.
        # Common path: C:\Program Files (x86)\Plex\Plex Media Server\Plex Media Server.exe
        $PlexPath = "${env:ProgramFiles(x86)}\Plex\Plex Media Server\Plex Media Server.exe"
        if (Test-Path $PlexPath) {
            Write-Host "Starting Plex from: $PlexPath"
            Start-Process -FilePath $PlexPath
            Write-Host "Plex restart command sent (via process restart)."
        } else {
            Write-Host "Error: Could not find Plex Media Server executable at $PlexPath"
            exit 1
        }
    } else {
        Write-Host "Error: Plex Media Server is neither running as a service nor as a process."
        exit 1
    }
}
