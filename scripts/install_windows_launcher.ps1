[CmdletBinding()]
param(
    [switch]$NoTaskbarPin
)

$ErrorActionPreference = "Stop"

# Keep the user-facing launcher outside the source checkout: it survives normal
# repository updates and is safe to run from either a terminal or Explorer.
$appName = "Toledo Workflow Relay"
$projectRoot = Split-Path -Parent $PSScriptRoot
$installRoot = Join-Path $env:LOCALAPPDATA "ToledoOrchestrator\Launcher"
$startMenuRoot = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$taskbarRoot = Join-Path $env:APPDATA "Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar"
$desktopRoot = [Environment]::GetFolderPath("Desktop")
$launcherPath = Join-Path $installRoot "Toledo Workflow Relay.ps1"
$iconPath = Join-Path $installRoot "Toledo Workflow Relay.ico"
$startMenuShortcut = Join-Path $startMenuRoot "$appName.lnk"
$taskbarShortcut = Join-Path $taskbarRoot "$appName.lnk"
$desktopShortcut = Join-Path $desktopRoot "$appName.lnk"

New-Item -ItemType Directory -Force -Path $installRoot, $startMenuRoot, $taskbarRoot | Out-Null

# A small, self-contained icon: deep navy square, Toledo coral relay path, and
# a clean "T" monogram.  Windows accepts a PNG payload inside an .ico wrapper.
Add-Type -AssemblyName System.Drawing
$bitmap = [System.Drawing.Bitmap]::new(256, 256)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$graphics.Clear([System.Drawing.Color]::FromArgb(12, 20, 39))
$coral = [System.Drawing.Color]::FromArgb(255, 112, 95)
$mint = [System.Drawing.Color]::FromArgb(87, 224, 190)
$pen = [System.Drawing.Pen]::new($coral, 13)
$pen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
$pen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
$graphics.DrawLine($pen, 42, 195, 82, 155)
$graphics.DrawLine($pen, 82, 155, 133, 206)
$graphics.DrawLine($pen, 133, 206, 213, 126)
$graphics.FillEllipse([System.Drawing.SolidBrush]::new($mint), 28, 181, 28, 28)
$graphics.FillEllipse([System.Drawing.SolidBrush]::new($mint), 119, 192, 28, 28)
$graphics.FillEllipse([System.Drawing.SolidBrush]::new($mint), 199, 112, 28, 28)
$font = [System.Drawing.Font]::new("Segoe UI Semibold", 90, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
$format = [System.Drawing.StringFormat]::new()
$format.Alignment = [System.Drawing.StringAlignment]::Center
$format.LineAlignment = [System.Drawing.StringAlignment]::Center
$graphics.DrawString("T", $font, [System.Drawing.Brushes]::White, [System.Drawing.RectangleF]::new(0, 26, 256, 105), $format)
$stream = [System.IO.MemoryStream]::new()
$bitmap.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
$png = $stream.ToArray()
$writer = [System.IO.BinaryWriter]::new([System.IO.File]::Open($iconPath, [System.IO.FileMode]::Create))
$writer.Write([UInt16]0); $writer.Write([UInt16]1); $writer.Write([UInt16]1)
$writer.Write([Byte]0); $writer.Write([Byte]0); $writer.Write([Byte]0); $writer.Write([Byte]0)
$writer.Write([UInt16]1); $writer.Write([UInt16]32); $writer.Write([UInt32]$png.Length); $writer.Write([UInt32]22)
$writer.Write($png); $writer.Close()
$stream.Close(); $font.Dispose(); $pen.Dispose(); $graphics.Dispose(); $bitmap.Dispose()

$launcher = @'
$ErrorActionPreference = "Stop"
$port = 8765
$url = "http://127.0.0.1:$port/"
$projectRoot = "__PROJECT_ROOT__"

function Get-AppBrowser {
    # Prefer stable Chrome for the dedicated app window.  Brave is an equivalent
    # Chromium fallback on this machine if Chrome is ever removed.
    $candidates = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
        "$env:ProgramFiles\BraveSoftware\Brave-Browser\Application\brave.exe",
        "${env:ProgramFiles(x86)}\BraveSoftware\Brave-Browser\Application\brave.exe",
        "$env:LOCALAPPDATA\BraveSoftware\Brave-Browser\Application\brave.exe"
    )
    return $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}

function Open-RelayApp {
    $browser = Get-AppBrowser
    if ($browser) {
        # --app opens the relay in a standalone, tabless application window,
        # rather than in an existing browser tab.
        Start-Process -FilePath $browser -ArgumentList "--app=$url", "--window-size=1440,960"
        return
    }
    Start-Process $url
}

function Test-RelayReady {
    try {
        $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 1
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    } catch { return $false }
}

if (Test-RelayReady) {
    Open-RelayApp
    exit 0
}

$python = Get-Command python.exe -ErrorAction Stop
Start-Process -FilePath $python.Source -ArgumentList "-m toledo_orchestrator ui --port $port --no-open" -WorkingDirectory $projectRoot -WindowStyle Hidden

$deadline = (Get-Date).AddSeconds(25)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 400
    if (Test-RelayReady) {
        Open-RelayApp
        exit 0
    }
}

Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.MessageBox]::Show("Toledo Workflow Relay did not become ready on port $port. Run its command from a terminal to see the startup error.", "Toledo Workflow Relay", "OK", "Error") | Out-Null
'@.Replace("__PROJECT_ROOT__", $projectRoot.Replace("'", "''"))

Set-Content -LiteralPath $launcherPath -Value $launcher -Encoding UTF8

$shell = New-Object -ComObject WScript.Shell
foreach ($shortcutPath in @($startMenuShortcut, $taskbarShortcut, $desktopShortcut)) {
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcherPath`""
    $shortcut.WorkingDirectory = $projectRoot
    $shortcut.IconLocation = "$iconPath,0"
    $shortcut.Description = "Open the Toledo Workflow Relay"
    $shortcut.Save()
}

$pinResult = "not attempted"
if (-not $NoTaskbarPin) {
    $folder = (New-Object -ComObject Shell.Application).Namespace((Split-Path -Parent $startMenuShortcut))
    $item = $folder.ParseName((Split-Path -Leaf $startMenuShortcut))
    $verb = @($item.Verbs() | Where-Object { $_.Name.Replace("&", "").Trim() -match "Pin to taskbar" }) | Select-Object -First 1
    if ($verb) {
        $verb.DoIt()
        $pinResult = "requested"
    } else {
        $pinResult = "Windows did not expose a taskbar-pin verb"
    }
}

[pscustomobject]@{
    launcher = $launcherPath
    icon = $iconPath
    start_menu_shortcut = $startMenuShortcut
    taskbar_shortcut = $taskbarShortcut
    desktop_shortcut = $desktopShortcut
    taskbar_pin = $pinResult
} | Format-List
