param(
    [string]$Repository = "saienjoy0/nasdaq-cafe-news-collector"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function ConvertTo-Base64Url {
    param([byte[]]$Bytes)
    return [Convert]::ToBase64String($Bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function New-RandomBase64Url {
    param([int]$ByteCount)
    $bytes = New-Object byte[] $ByteCount
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }
    return ConvertTo-Base64Url $bytes
}

function Set-RepositorySecret {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Value
    )

    $temp = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText($temp, $Value, [System.Text.UTF8Encoding]::new($false))
        Get-Content -LiteralPath $temp -Raw | gh secret set $Name --repo $Repository
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to set GitHub Secret: $Name"
        }
    }
    finally {
        Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "GitHub CLI (gh) was not found. Install it before running this script."
}

gh auth status | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "GitHub CLI is not authenticated. Run: gh auth login"
}

$oauthBase = "https://openapi.longbridge.com/oauth2"
$redirectUri = "http://127.0.0.1:60355/callback/"

$registrationBody = @{
    client_name                = "NASDAQ Cafe GitHub Actions"
    redirect_uris              = @($redirectUri)
    token_endpoint_auth_method = "none"
    grant_types                = @("authorization_code", "refresh_token")
    response_types             = @("code")
} | ConvertTo-Json -Depth 5

Write-Host "Registering a Longbridge OAuth public client..."
$registration = Invoke-RestMethod `
    -Method Post `
    -Uri "$oauthBase/register" `
    -ContentType "application/json" `
    -Body $registrationBody

$clientId = [string]$registration.client_id
if ([string]::IsNullOrWhiteSpace($clientId)) {
    throw "Longbridge did not return client_id."
}

$codeVerifier = New-RandomBase64Url 64
$sha256 = [System.Security.Cryptography.SHA256]::Create()
try {
    $challengeBytes = $sha256.ComputeHash([System.Text.Encoding]::ASCII.GetBytes($codeVerifier))
}
finally {
    $sha256.Dispose()
}
$codeChallenge = ConvertTo-Base64Url $challengeBytes
$state = New-RandomBase64Url 32

$query = @(
    "response_type=code",
    "client_id=$([Uri]::EscapeDataString($clientId))",
    "redirect_uri=$([Uri]::EscapeDataString($redirectUri))",
    "scope=3",
    "state=$([Uri]::EscapeDataString($state))",
    "code_challenge=$([Uri]::EscapeDataString($codeChallenge))",
    "code_challenge_method=S256"
) -join "&"
$authorizeUrl = "$oauthBase/authorize?$query"

$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 60355)
$client = $null
try {
    $listener.Start()
    Write-Host "Opening Longbridge authorization in the browser..."
    Write-Host "Complete authorization within five minutes."
    Start-Process $authorizeUrl

    $acceptTask = $listener.AcceptTcpClientAsync()
    if (-not $acceptTask.Wait([TimeSpan]::FromMinutes(5))) {
        throw "Longbridge authorization timed out. Run the script again."
    }

    $client = $acceptTask.Result
    $stream = $client.GetStream()
    $reader = [System.IO.StreamReader]::new(
        $stream,
        [System.Text.Encoding]::ASCII,
        $false,
        1024,
        $true
    )

    $requestLine = $reader.ReadLine()
    while ($null -ne ($headerLine = $reader.ReadLine()) -and $headerLine -ne "") {
        # Consume HTTP request headers.
    }

    if ([string]::IsNullOrWhiteSpace($requestLine)) {
        throw "Longbridge callback request could not be read."
    }

    $parts = $requestLine.Split(' ')
    if ($parts.Count -lt 2) {
        throw "Longbridge callback request was malformed."
    }

    $requestTarget = $parts[1]
    $callbackUri = [Uri]("http://127.0.0.1:60355" + $requestTarget)
    Add-Type -AssemblyName System.Web
    $callbackQuery = [System.Web.HttpUtility]::ParseQueryString($callbackUri.Query)

    $returnedState = $callbackQuery["state"]
    $authorizationCode = $callbackQuery["code"]
    $oauthError = $callbackQuery["error"]

    $success = [string]::IsNullOrWhiteSpace($oauthError) -and
        -not [string]::IsNullOrWhiteSpace($authorizationCode) -and
        $returnedState -eq $state

    if ($success) {
        $html = "<html><body><h2>Longbridge authorization completed.</h2><p>You may close this tab.</p></body></html>"
        $status = "200 OK"
    }
    else {
        $html = "<html><body><h2>Longbridge authorization failed.</h2><p>Return to PowerShell for details.</p></body></html>"
        $status = "400 Bad Request"
    }

    $responseBytes = [System.Text.Encoding]::UTF8.GetBytes($html)
    $headers = "HTTP/1.1 $status`r`nContent-Type: text/html; charset=utf-8`r`nContent-Length: $($responseBytes.Length)`r`nConnection: close`r`n`r`n"
    $headerBytes = [System.Text.Encoding]::ASCII.GetBytes($headers)
    $stream.Write($headerBytes, 0, $headerBytes.Length)
    $stream.Write($responseBytes, 0, $responseBytes.Length)
    $stream.Flush()

    if (-not [string]::IsNullOrWhiteSpace($oauthError)) {
        throw "Longbridge authorization was rejected: $oauthError"
    }
    if ($returnedState -ne $state) {
        throw "Longbridge OAuth state mismatch."
    }
    if ([string]::IsNullOrWhiteSpace($authorizationCode)) {
        throw "Longbridge did not return an authorization code."
    }
}
finally {
    if ($null -ne $client) {
        $client.Dispose()
    }
    $listener.Stop()
}

Write-Host "Exchanging the authorization code for portable OAuth tokens..."
$token = Invoke-RestMethod `
    -Method Post `
    -Uri "$oauthBase/token" `
    -ContentType "application/x-www-form-urlencoded" `
    -Body @{
        grant_type    = "authorization_code"
        client_id     = $clientId
        redirect_uri  = $redirectUri
        code          = $authorizationCode
        code_verifier = $codeVerifier
    }

$refreshToken = [string]$token.refresh_token
$accessToken = [string]$token.access_token
if ([string]::IsNullOrWhiteSpace($refreshToken) -or [string]::IsNullOrWhiteSpace($accessToken)) {
    throw "Longbridge token response did not include access_token and refresh_token."
}

Write-Host "Saving portable OAuth credentials to GitHub Repository Secrets..."
Set-RepositorySecret -Name "LONGBRIDGE_OAUTH_CLIENT_ID" -Value $clientId
Set-RepositorySecret -Name "LONGBRIDGE_OAUTH_REFRESH_TOKEN" -Value $refreshToken

# The machine-bound CLI file is intentionally no longer used. Its absence is
# already the desired state, so a 404 must not fail an otherwise valid setup.
$previousErrorActionPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = "SilentlyContinue"
    gh secret delete LONGBRIDGE_CLI_AUTH_B64 --repo $Repository 2>$null | Out-Null
    $legacyDeleteExitCode = $LASTEXITCODE
}
finally {
    $ErrorActionPreference = $previousErrorActionPreference
}
if ($legacyDeleteExitCode -ne 0) {
    Write-Host "Legacy LONGBRIDGE_CLI_AUTH_B64 was already absent; continuing."
}

Write-Host "Portable Longbridge OAuth bootstrap completed."
Write-Host "Repository: $Repository"
Write-Host "Created: LONGBRIDGE_OAUTH_CLIENT_ID"
Write-Host "Created: LONGBRIDGE_OAUTH_REFRESH_TOKEN"
Write-Host "Existing LONGBRIDGE_SECRET_ROTATOR_TOKEN remains in use."
