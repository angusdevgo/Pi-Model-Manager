# Repoint the desktop shortcut of Pi Model Manager to launch silently
# without any command prompt console window.
#
# Usage: double-click "Fix-Shortcut.bat", or run this file with PowerShell.

$projectDir = $PSScriptRoot
if (-not $projectDir) { $projectDir = (Get-Location).Path }
$vbsPath = Join-Path $projectDir 'launch.vbs'
$appPy = Join-Path $projectDir 'desktop_app.py'

Write-Host "Project dir : $projectDir"
Write-Host "VBS Target  : $vbsPath"
Write-Host ""

$shell = New-Object -ComObject WScript.Shell
$keywords = @('desktop_app.py', 'PiModelManager', 'pi-model-manager', 'launch.vbs')

$desktops = @()
$desktops += [Environment]::GetFolderPath('Desktop')
$desktops += [Environment]::GetFolderPath('CommonDesktopDirectory')
$desktops = $desktops | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique

# Find pythonw.exe for icon and target
$pythonw = ""
$pywCmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if ($pywCmd) { $pythonw = $pywCmd.Source }
if (-not $pythonw) {
    $searchPaths = @(
        "$env:LOCALAPPDATA\Programs\Python\Python*\pythonw.exe",
        "$env:ProgramFiles\Python*\pythonw.exe",
        "${env:ProgramFiles(x86)}\Python*\pythonw.exe",
        "C:\Python*\pythonw.exe"
    )
    foreach ($p in $searchPaths) {
        $found = Resolve-Path $p -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found) { $pythonw = $found.Path; break }
    }
}

$fixList = @()
foreach ($dir in $desktops) {
    $links = Get-ChildItem -LiteralPath $dir -Filter *.lnk -File -ErrorAction SilentlyContinue
    foreach ($file in $links) {
        $isTarget = $false
        try {
            $lnk = $shell.CreateShortcut($file.FullName)
            $combined = ([string]$lnk.TargetPath) + ' ' + ([string]$lnk.Arguments)
            foreach ($kw in $keywords) {
                if ($combined -like "*$kw*") { $isTarget = $true; break }
            }
            if (-not $isTarget -and $file.BaseName -match 'pi.*model|model.*manager') {
                $isTarget = $true
            }
        } catch {
            $isTarget = $false
        }
        if ($isTarget) { $fixList += $file.FullName }
    }
}

function Configure-Shortcut($lnkPath) {
    try {
        $lnk = $shell.CreateShortcut($lnkPath)
        if ($pythonw -and (Test-Path -LiteralPath $pythonw)) {
            $lnk.TargetPath = $pythonw
            $lnk.Arguments = "`"$appPy`""
            $lnk.IconLocation = "$pythonw,0"
        } else {
            $lnk.TargetPath = "wscript.exe"
            $lnk.Arguments = "`"$vbsPath`""
        }
        $lnk.WorkingDirectory = $projectDir
        $lnk.Description = "Pi Model Manager (Desktop GUI)"
        $lnk.WindowStyle = 1 # Normal - the GUI must not start minimized
        $lnk.Save()
        Write-Host "[OK] Updated shortcut: $lnkPath"
    } catch {
        Write-Host "[ERROR] Failed on $lnkPath : $($_.Exception.Message)"
    }
}

if ($fixList.Count -eq 0) {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $newPath = Join-Path $desktop 'Pi Model Manager.lnk'
    Configure-Shortcut $newPath
} else {
    foreach ($path in $fixList) {
        Configure-Shortcut $path
    }
}

Write-Host ""
Write-Host "Done! The desktop shortcut will now launch directly without any command console."
