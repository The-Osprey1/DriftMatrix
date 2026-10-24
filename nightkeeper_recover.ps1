param(
    [string]$Panel = "http://192.168.1.90",
    [switch]$RemoveNightkeeper,
    [int]$Attempts = 24,
    [int]$DelaySeconds = 5
)

$ErrorActionPreference = "Stop"
$removed = -not $RemoveNightkeeper

for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
    try {
        if (-not $removed) {
            try {
                Invoke-RestMethod -Method Delete -Uri "$Panel/api/v1/apps/nightkeeper" -TimeoutSec 5 | Out-Null
            }
            catch {
                if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -ne 404) {
                    throw
                }
            }
            $removed = $true
        }

        Invoke-RestMethod -Method Patch -Uri "$Panel/api/v1/settings" `
            -ContentType "application/json" `
            -Body '{"autoBrightness":false,"brightness":45}' -TimeoutSec 5 | Out-Null
        Invoke-RestMethod -Method Patch -Uri "$Panel/api/v1/display" `
            -ContentType "application/json" `
            -Body '{"power":true}' -TimeoutSec 5 | Out-Null

        $display = Invoke-RestMethod -Uri "$Panel/api/v1/display" -TimeoutSec 5
        $settings = Invoke-RestMethod -Uri "$Panel/api/v1/settings" -TimeoutSec 5
        if ($display.power -eq $true -and
            [int]$settings.brightness -eq 45 -and
            $settings.autoBrightness -eq $false) {
            exit 0
        }
    }
    catch {
        if ($attempt -eq $Attempts) {
            Write-Error "AWTRIX recovery failed after $Attempts attempts: $($_.Exception.Message)"
            exit 1
        }
    }

    Start-Sleep -Seconds ([Math]::Max(1, $DelaySeconds))
}

exit 1
