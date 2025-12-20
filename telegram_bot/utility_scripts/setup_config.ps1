# Plex-o-Tron Bot Configuration Setup (Windows PowerShell)

Write-Host "--- Plex-o-Tron Bot Configuration Setup ---" -ForegroundColor Cyan
Write-Host "This script will generate a config.ini file for you."

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

    # 1. Request PIN
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

    # 2. Build Auth URL
    $AuthUrl = "https://app.plex.tv/auth#?context%5Bdevice%5D%5Bproduct%5D=$ProductName&clientID=$ClientId&code=$PinCode"

    Write-Host "`nOpening your default browser to authorize the app..."
    Write-Host "URL: $AuthUrl" -ForegroundColor Gray
    Start-Process $AuthUrl

    Write-Host "`nWaiting for authorization (timeout in 3 minutes)..."

    # 3. Poll for Token
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

        if ($Servers.Count -eq 0) {
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

        # Suggest a connection (prefer local if possible)
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

# --- Main Script ---

# 1. Telegram Configuration
Write-Host "`n[Telegram Settings]" -ForegroundColor Yellow
Write-Host "** Get your bot token from @BotFather on Telegram **" -ForegroundColor Yellow
$BotToken = Get-Input -Prompt "Enter your Telegram Bot Token"
$AllowedIds = Get-Input -Prompt "Enter allowed User IDs (comma separated)"

# 2. Plex Configuration
Write-Host "`n[Plex Settings]" -ForegroundColor Yellow

$PlexUrl = ""
$PlexToken = ""

$AutoToken = Read-Host "Do you want to automatically retrieve your Plex Token and Server URL? (y/n) [y]"
if ($AutoToken -ne "n") {
    $PlexToken = Get-PlexToken
    if ($PlexToken) {
        $DiscoveredUrl = Discover-PlexServer -Token $PlexToken
        if ($DiscoveredUrl) {
            $PlexUrl = $DiscoveredUrl
        } else {
            $PlexUrl = Get-Input -Prompt "Enter your Plex Server URL" -Default "http://localhost:32400"
        }
    } else {
        Write-Host "Falling back to manual entry." -ForegroundColor Yellow
        $PlexToken = Get-Input -Prompt "Enter your Plex Authentication Token" -Default ""
        $PlexUrl = Get-Input -Prompt "Enter your Plex Server URL" -Default "http://localhost:32400"
    }
} else {
    $PlexToken = Get-Input -Prompt "Enter your Plex Authentication Token" -Default ""
    $PlexUrl = Get-Input -Prompt "Enter your Plex Server URL" -Default "http://localhost:32400"
}

# 3. Host/Path Configuration
Write-Host "`n[Host Paths]" -ForegroundColor Yellow
$DefaultDl = "C:\Downloads\Unsorted"
$DefaultMov = "C:\Media\Movies"
$DefaultTv = "C:\Media\TV"

$DefaultSavePath = Get-Input -Prompt "Download Path (Unsorted)" -Default $DefaultDl
$MoviesSavePath = Get-Input -Prompt "Movies Library Path" -Default $DefaultMov
$TvShowsSavePath = Get-Input -Prompt "TV Shows Library Path" -Default $DefaultTv

# 4. Construct Content
$ConfigContent = @"
# file: config.ini

[telegram]
# Get your bot token from @BotFather on Telegram
bot_token = $BotToken
# Get your numeric User ID from @userinfobot on Telegram
allowed_user_ids = $AllowedIds

[plex]
# (Optional) Your Plex server URL and API token
plex_url = $PlexUrl
plex_token = $PlexToken

[host]
# Define absolute paths for your media. Use forward slashes for both OSes.
default_save_path = $DefaultSavePath
movies_save_path = $MoviesSavePath
tv_shows_save_path = $TvShowsSavePath

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
            "4k": 5,
            "2160p": 5,
            "1080p": 3,
            "720p": 1
        },
        "codecs": {
            "x265": 5,
            "hevc": 5,
            "x264": 1,
            "h264": 1
        },
        "uploaders": {
            "mazemaze16": 5,
            "QxR": 5
        }
    },
    "tv": {
        "resolutions": {
            "1080p": 5,
            "720p": 2
        },
        "codecs": {
            "x265": 5,
            "hevc": 5,
            "x264": 1
        },
        "uploaders": {
            "EZTV": 5,
            "MeGusta": 5
        }
    }
}
"@

# 5. Write File
$OutputPath = Join-Path (Get-Location) "config.ini"

if (Test-Path $OutputPath) {
    $Overwrite = Read-Host "'config.ini' already exists. Overwrite? (y/n) [n]"
    if ($Overwrite -ne "y") {
        Write-Host "Operation cancelled. No changes made." -ForegroundColor Yellow
        exit
    }
}

try {
    [System.IO.File]::WriteAllText($OutputPath, $ConfigContent)
    Write-Host "`nSuccess! Configuration written to: $OutputPath" -ForegroundColor Green
    Write-Host "You can now run the bot using: uv run __main__.py"
} catch {
    Write-Host "Error writing file: $_" -ForegroundColor Red
}
