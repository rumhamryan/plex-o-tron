# This script's sole purpose is to restart the Plex Media Server on Windows.

Write-Host "Wrapper script initiated. Attempting to restart Plex..."

# Attempt service-based restart/start first. This requires Administrator privileges.
$serviceCandidates = @("Plex Media Server", "PlexService")
$resolvedService = $null

foreach ($candidate in $serviceCandidates) {
    $service = Get-Service -Name $candidate -ErrorAction SilentlyContinue
    if ($service) {
        $resolvedService = $service
        break
    }
}

if (-not $resolvedService) {
    $resolvedService = Get-Service -ErrorAction SilentlyContinue |
        Where-Object {
            $_.DisplayName -match "Plex.*Media.*Server" -or $_.Name -eq "PlexService"
        } |
        Select-Object -First 1
}

if ($resolvedService) {
    $serviceName = $resolvedService.Name
    if ($resolvedService.Status -eq "Running") {
        Write-Host "Restarting service: $serviceName"
        Restart-Service -Name $serviceName -Force
    } else {
        Write-Host "Starting service: $serviceName"
        Start-Service -Name $serviceName
    }
    Write-Host "Plex restart command sent."
    exit 0
}

Write-Host "Plex service not found. Checking for process and executable..."
$process = Get-Process -Name "Plex Media Server" -ErrorAction SilentlyContinue | Select-Object -First 1

$candidatePaths = @()
if ($process) {
    try {
        if ($process.Path) {
            $candidatePaths += $process.Path
        }
    } catch {
        # Accessing Process.Path can fail for protected processes; continue with known paths.
    }
}

$candidatePaths += @(
    "$env:LOCALAPPDATA\Plex Media Server\Plex Media Server.exe",
    "${env:ProgramFiles}\Plex\Plex Media Server\Plex Media Server.exe",
    "${env:ProgramFiles(x86)}\Plex\Plex Media Server\Plex Media Server.exe"
)

$plexPath = $null
foreach ($candidatePath in $candidatePaths) {
    if ($candidatePath -and (Test-Path $candidatePath)) {
        $plexPath = $candidatePath
        break
    }
}

if ($process) {
    Write-Host "Stopping process..."
    $process | Stop-Process -Force
}

if ($plexPath) {
    Write-Host "Starting Plex from: $plexPath"
    Start-Process -FilePath $plexPath
    Write-Host "Plex restart command sent (via process launch)."
    exit 0
}

Write-Host "Error: Could not locate Plex service or executable."
exit 1
