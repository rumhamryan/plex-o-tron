# Plex-o-Tron Bot Full Setup Script (Windows PowerShell)

$ErrorActionPreference = "Stop"

Write-Host "--- Starting Plex-o-Tron Bot Full Setup ---" -ForegroundColor Green

# --- Helper Functions ---

function Get-Input {
    param (
        [string]$Prompt,
        [string]$Default = ""
    )
    if ($Default -ne "") {
        $InputVal = Read-Host "$Prompt [$Default]"
        if ($InputVal -eq "") { return $Default } else { return $InputVal }
    } else {
        while ($true) {
            $InputVal = Read-Host "$Prompt"
            if ($InputVal -ne "") { return $InputVal }
            Write-Host "This field is required." -ForegroundColor Red
        }
    }
}

function Get-PlexToken {
    Write-Host "`n--- Plex Authentication Flow ---" -ForegroundColor Cyan
    Write-Host "We will now obtain a Plex Token by logging you in via your browser."

    $ClientId = [Guid]::NewGuid().ToString()
    $ProductName = "Plex Telegram Bot Setup"
    $Headers = @{
        "X-Plex-Product" = $ProductName
        "X-Plex-Version" = "1.0"
        "X-Plex-Platform" = "PowerShell"
        "X-Plex-Client-Identifier" = $ClientId
        "Accept" = "application/json"
    }
    $PinsUrl = "https://plex.tv/api/v2/pins"

    Write-Host "Requesting login PIN..." -NoNewline
    try {
        $PinResponse = Invoke-RestMethod -Uri $PinsUrl -Method Post -Headers $Headers -Body @{ "strong" = "true" }
        Write-Host " Done." -ForegroundColor Green
    } catch {
        Write-Host "`nError contacting Plex: $_" -ForegroundColor Red
        return $null
    }

    $PinId = $PinResponse.id
    $PinCode = $PinResponse.code
    $AuthUrl = "https://app.plex.tv/auth#?context%5Bdevice%5D%5Bproduct%5D=$($ProductName -replace ' ','%20')&clientID=$ClientId&code=$PinCode"

    Write-Host "`nPlease open the following URL in your web browser to authorize the app:"
    Write-Host "`n    $AuthUrl`n" -ForegroundColor Yellow
    Start-Process $AuthUrl

    Write-Host "Waiting for authorization (timeout in 3 minutes)..."

    $StartTime = Get-Date
    while ((Get-Date) -lt $StartTime.AddMinutes(3)) {
        Start-Sleep -Seconds 5
        try {
            $CheckResponse = Invoke-RestMethod -Uri "$PinsUrl/$PinId" -Method Get -Headers $Headers
            if ($CheckResponse.authToken) {
                Write-Host "`nSuccess! Token retrieved." -ForegroundColor Green
                return $CheckResponse.authToken
            }
            Write-Host "." -NoNewline
        } catch {
            Write-Host "." -NoNewline
        }
    }

    Write-Host "`nTimed out waiting for authentication." -ForegroundColor Red
    return $null
}

function Discover-PlexServer {
    param([string]$Token)
    Write-Host "`nSearching for Plex servers associated with your account..." -ForegroundColor Cyan

    $ClientId = [Guid]::NewGuid().ToString()
    $Headers = @{
        "X-Plex-Token" = $Token;
        "X-Plex-Client-Identifier" = $ClientId;
        "Accept" = "application/json"
    }
    try {
        $Resources = Invoke-RestMethod -Uri "https://plex.tv/api/v2/resources?includeHttps=1" -Method Get -Headers $Headers
        $Servers = $Resources | Where-Object { $_.provides -like "*server*" }

        if ($null -eq $Servers -or $Servers.Count -eq 0) {
            Write-Host "No Plex servers found on your account." -ForegroundColor Yellow
            return $null
        }

        $SelectedServer = $null
        if ($Servers.Count -eq 1) {
            $SelectedServer = $Servers[0]
            Write-Host "Found server: $($SelectedServer.name)" -ForegroundColor Green
        } else {
            Write-Host "Multiple servers found. Please choose one:"
            for ($i = 0; $i -lt $Servers.Count; $i++) {
                Write-Host "[$i] $($Servers[$i].name) (Product: $($Servers[$i].product))"
            }
            $Choice = Get-Input -Prompt "Enter selection number" -Default "0"
            $SelectedServer = $Servers[[int]$Choice]
        }

        $Connections = $SelectedServer.connections
        $LocalConn = $Connections | Where-Object { $_.local -eq $true } | Select-Object -First 1
        $RemoteConn = $Connections | Where-Object { $_.local -eq $false } | Select-Object -First 1

        $DefaultUrl = if ($LocalConn) { $LocalConn.uri } else { $RemoteConn.uri }

        Write-Host "`nDetected connection URLs for $($SelectedServer.name):"
        if ($LocalConn) { Write-Host "  - Local: $($LocalConn.uri)" }
        if ($RemoteConn) { Write-Host "  - Remote: $($RemoteConn.uri)" }

        return Get-Input -Prompt "Confirm or enter Plex URL" -Default $DefaultUrl
    } catch {
        Write-Host "Error discovering servers: $_" -ForegroundColor Red
        return $null
    }
}

# --- Step 1: Python Version Check & Selection ---
Write-Host "`nStep 1: Verifying Python Version (>= 3.12 Required)..." -ForegroundColor Yellow

function Get-SystemPython {
    foreach ($cmd in "python", "python3") {
        if (Get-Command $cmd -ErrorAction SilentlyContinue) {
            $ver = & $cmd -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
            if ($ver -eq "3.12" -or $ver -eq "3.13") {
                return $cmd
            }
        }
    }
    return $null
}

$SystemPython = Get-SystemPython

if ($null -ne $SystemPython) {
    Write-Host "Found compatible system Python: $SystemPython" -ForegroundColor Green
} else {
    Write-Host "No system Python 3.12+ detected. uv will attempt to manage Python for you." -ForegroundColor Cyan
}

# --- Step 2: Setup Virtual Environment & Dependencies (using uv) ---
Write-Host "`nStep 2: Setting up Virtual Environment and Dependencies (using uv)..." -ForegroundColor Yellow

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv not found. Installing uv..." -ForegroundColor Yellow
    try {
        powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    } catch {
        Write-Host "Primary installer failed. Trying pip fallback..." -ForegroundColor Yellow
        if ($null -ne $SystemPython) {
            & $SystemPython -m pip install uv --user
        } else {
            python -m pip install uv --user
        }
    }

    # Refresh PATH for the current session
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Host "Error: uv installation failed or not found in PATH." -ForegroundColor Red
        Write-Host "Please install uv manually: https://github.com/astral-sh/uv"
        exit
    }
    Write-Host "uv installed successfully." -ForegroundColor Green
}

Write-Host "Using uv for dependency management..."
try {
    if ($null -ne $SystemPython) {
        Write-Host "Creating venv using system Python..." -ForegroundColor Gray
        Invoke-Expression "uv venv --python $SystemPython"
    } else {
        Write-Host "Creating venv (uv may download Python 3.12)..." -ForegroundColor Gray
        Invoke-Expression "uv venv --python 3.12"
    }
    Invoke-Expression "uv pip sync pyproject.toml"
} catch {
    Write-Host "`nError: uv failed to create a virtual environment." -ForegroundColor Red
    Write-Host "This usually happens if the Python download from GitHub timed out."
    Write-Host "`nTo fix this, please install Python 3.12 or 3.13 manually from python.org" -ForegroundColor Yellow
    Write-Host "Then run this setup script again."
    exit
}

Write-Host "Dependencies installed successfully." -ForegroundColor Green

# --- Step 3: Interactive Configuration ---
Write-Host "`nStep 3: Configuring the Bot..." -ForegroundColor Yellow
$ConfigPath = Join-Path (Get-Location) "config.ini"
$Reconfig = "y"
if (Test-Path $ConfigPath) {
    $Reconfig = Read-Host "config.ini already exists. Re-configure? (y/n) [n]"
}

if ($Reconfig -eq "y") {
    Write-Host "`n[Telegram Settings]" -ForegroundColor Cyan
    $BotToken = Get-Input -Prompt "Enter your Telegram Bot Token"
    $AllowedIds = Get-Input -Prompt "Enter allowed User IDs (comma separated)"

    Write-Host "`n[Plex Settings]" -ForegroundColor Cyan
    $PlexUrl = ""
    $PlexToken = ""
    $AutoPlex = Read-Host "Do you want to automatically retrieve your Plex Token and Server URL? (y/n) [y]"

    if ($AutoPlex -ne "n") {
        $PlexToken = Get-PlexToken
        if ($PlexToken) {
            $DiscoveredUrl = Discover-PlexServer -Token $PlexToken
            if ($DiscoveredUrl) { $PlexUrl = $DiscoveredUrl }
        }
    }

    if ($PlexToken -eq "") { $PlexToken = Get-Input -Prompt "Enter your Plex Authentication Token" }
    if ($PlexUrl -eq "") { $PlexUrl = Get-Input -Prompt "Enter your Plex Server URL" -Default "http://localhost:32400" }

    Write-Host "`n[Host Paths]" -ForegroundColor Cyan
    $DefaultDl = "C:\Downloads\Unsorted"
    $DefaultMov = "C:\Media\Movies"
    $DefaultTv = "C:\Media\TV"

    $DefaultSavePath = Get-Input -Prompt "Download Path (Unsorted)" -Default $DefaultDl
    $MoviesSavePath = Get-Input -Prompt "Movies Library Path" -Default $DefaultMov
    $TvShowsSavePath = Get-Input -Prompt "TV Shows Library Path" -Default $DefaultTv

    $ConfigContent = @"
# file: config.ini

[telegram]
bot_token = $BotToken
allowed_user_ids = $AllowedIds

[plex]
plex_url = $PlexUrl
plex_token = $PlexToken

[host]
default_save_path = $($DefaultSavePath -replace '\\','/')
movies_save_path = $($MoviesSavePath -replace '\\','/')
tv_shows_save_path = $($TvShowsSavePath -replace '\\','/')

[search]
websites = {
    "movies": [
        {"name": "YTS.lt", "search_url": "https://yts.lt/browse-movies/{query}/{quality}/all/0/latest/{year}/all"},
        {"name": "tpb", "search_url": "https://thepiratebay.org/search.php?q={query}&cat=0"}
    ],
    "tv": [
        {"name": "eztvx.to", "search_url": "https://eztvx.to/search/{query}"},
        {"name": "tpb", "search_url": "https://thepiratebay.org/search.php?q={query}&cat=0"}
    ]
}
preferences = {
    "movies": {
        "resolutions": {
            "4k": 5, "2160p": 5, "1080p": 3, "720p": 1
        },
        "codecs": {
            "x265": 2, "hevc": 2, "x264": 1, "h264": 1
        },
        "uploaders": {
            "mazemaze16": 5, "QxR": 5
        }
    },
    "tv": {
        "resolutions": {
            "1080p": 5, "720p": 2
        },
        "codecs": {
            "x265": 2, "hevc": 2, "x264": 1
        },
        "uploaders": {
            "EZTV": 5, "MeGusta": 5
        }
    }
}
"@
    [System.IO.File]::WriteAllText($ConfigPath, $ConfigContent)
    Write-Host "Configuration saved to config.ini." -ForegroundColor Green
}

Write-Host "`n--- Setup Complete! ---" -ForegroundColor Green
Write-Host "To run the bot, use the following command:"
Write-Host "uv run __main__.py" -ForegroundColor Cyan
Write-Host "Or manually:"
Write-Host ".\.venv\Scripts\Activate.ps1; python __main__.py" -ForegroundColor Cyan
