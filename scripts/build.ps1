# Pi Model Manager 一键打包脚本（PyInstaller onefile / 无控制台）
# 用法: powershell -ExecutionPolicy Bypass -File scripts\build.ps1 [-Clean]

param([switch]$Clean)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

Write-Host "== Pi Model Manager 打包 ==" -ForegroundColor Cyan
Write-Host "项目根目录: $root"

# 依赖检查
python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[!] 未检测到 PyInstaller，正在安装..." -ForegroundColor Yellow
    python -m pip install --disable-pip-version-check pyinstaller
}

if ($Clean) {
    Write-Host "[clean] 清理 build/ dist/ ..." -ForegroundColor Yellow
    Remove-Item -Recurse -Force (Join-Path $root 'build') -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force (Join-Path $root 'dist') -ErrorAction SilentlyContinue
}

$spec = Join-Path $PSScriptRoot 'PiModelManager.spec'
$distPath = Join-Path $root 'dist'
$workPath = Join-Path $root 'build\pyinstaller'

python -m PyInstaller --noconfirm --clean `
    --distpath $distPath `
    --workpath $workPath `
    $spec

if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败（退出码 $LASTEXITCODE）" }

$exe = Join-Path $distPath 'PiModelManager.exe'
if (-not (Test-Path $exe)) { throw "打包失败：未生成 $exe" }

$sizeMb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "[OK] 产物: $exe ($sizeMb MB)" -ForegroundColor Green

# 冒烟自检：无 GUI 启动，验证内嵌 JS / 配置读取 / 依赖完整
Write-Host "运行 exe 自检 (--selftest) ..." -ForegroundColor Cyan
$proc = Start-Process -FilePath $exe -ArgumentList '--selftest' -Wait -PassThru
if ($proc.ExitCode -ne 0) {
    throw "exe 自检失败（退出码 $($proc.ExitCode)）。日志: $env:USERPROFILE\.pi\agent\pi-model-manager.log"
}
Write-Host "自检通过 ✅" -ForegroundColor Green
Write-Host "双击 PiModelManager.bat 或 dist\PiModelManager.exe 即可启动。" -ForegroundColor Green
