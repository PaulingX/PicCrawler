param(
  [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

# 以脚本所在目录的上级（项目根）为工作目录。
# 防止从 scripts/ 目录启动时，相对路径 main.py / PicCrawler.spec 找不到。
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $root
$root = (Get-Location).Path

function Ensure-Success {
  if ($LASTEXITCODE -ne 0) {
    throw "上一步执行失败（退出码 $LASTEXITCODE）"
  }
}

Write-Host "[1/3] 安装打包依赖..."
& $Python -m pip install -r (Join-Path $root "requirements.txt")
Ensure-Success
& $Python -m pip install pyinstaller
Ensure-Success

Write-Host "[2/3] 清理历史产物（失败则忽略，由 PyInstaller --noconfirm 覆盖）..."
try { Remove-Item -Recurse -Force (Join-Path $root "build") -ErrorAction Stop } catch { }
try { Remove-Item -Recurse -Force (Join-Path $root "dist") -ErrorAction Stop } catch { }

Write-Host "[3/3] 打包 PicCrawler.exe ..."
& $Python -m PyInstaller --noconfirm (Join-Path $root "PicCrawler.spec")
Ensure-Success

Write-Host "完成: dist/PicCrawler.exe"