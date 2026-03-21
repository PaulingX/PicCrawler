param(
  [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

Write-Host "[1/3] 安装打包依赖..."
& $Python -m pip install -r requirements.txt
& $Python -m pip install pyinstaller

Write-Host "[2/3] 清理历史产物..."
if (Test-Path build) { Remove-Item -Recurse -Force build }
if (Test-Path dist) { Remove-Item -Recurse -Force dist }
if (Test-Path PicCrawler.spec) { Remove-Item -Force PicCrawler.spec }

Write-Host "[3/3] 打包 PicCrawler.exe ..."
& $Python -m PyInstaller `
  --name PicCrawler `
  --onefile `
  --add-data "app/templates;app/templates" `
  --add-data "app/static;app/static" `
  main.py

Write-Host "完成: dist/PicCrawler.exe"
