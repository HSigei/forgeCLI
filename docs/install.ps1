# Install Forge on Windows:
#   irm https://forgecli.pages.dev/install.ps1 | iex
# Downloads ForgeSetup.exe (the standalone build, no Python needed) and runs it: it installs to
# %LOCALAPPDATA%\Programs\Forge, adds itself to PATH and the Start menu. Run again to update.
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$url = 'https://github.com/HSigei/forgeCLI/releases/latest/download/ForgeSetup.exe'
$f = Join-Path $env:TEMP 'ForgeSetup.exe'
Write-Host ''
Write-Host '  Downloading Forge ...' -ForegroundColor Magenta
Invoke-WebRequest -UseBasicParsing $url -OutFile $f
$p = Start-Process -FilePath $f -ArgumentList '--install', '--quiet' -Wait -PassThru -NoNewWindow
if ($p.ExitCode -ne 0) { throw "ForgeSetup.exe failed (exit $($p.ExitCode))" }
$dir = Join-Path $env:LOCALAPPDATA 'Programs\Forge'
if (($env:Path -split ';') -notcontains $dir) { $env:Path = "$env:Path;$dir" }
Write-Host ''
Write-Host "  Forge is installed: $(& "$dir\forge.exe" --version)" -ForegroundColor Green
Write-Host '  Type  forge  to start.'
Write-Host ''
