param([switch]$Demo)

$interpreter = 'C:\Users\devyn\anaconda3\pythonw.exe'
$entryPoint = Join-Path $PSScriptRoot 'desk_controls.py'
if (-not (Test-Path -LiteralPath $interpreter)) {
    throw 'The configured Anaconda Python interpreter was not found.'
}
$launchArguments = @(('"{0}"' -f $entryPoint))
if ($Demo) { $launchArguments += '--demo' }
# This is the requested interactive control window; hide only its console.
Start-Process -FilePath $interpreter -ArgumentList $launchArguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
