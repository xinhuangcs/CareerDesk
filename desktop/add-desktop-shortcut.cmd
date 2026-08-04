@echo off
setlocal
set "APP_DIR=%~dp0"
chcp 65001 >nul
echo Creating a desktop shortcut for CareerDesk...
echo 正在为 CareerDesk 创建桌面快捷方式……
powershell -NoProfile -ExecutionPolicy Bypass -Command "$dir = $env:APP_DIR.TrimEnd('\'); $desktop = [Environment]::GetFolderPath('Desktop'); $ws = New-Object -ComObject WScript.Shell; $lnk = $ws.CreateShortcut((Join-Path $desktop 'CareerDesk.lnk')); $lnk.TargetPath = (Join-Path $dir 'CareerDesk.exe'); $lnk.WorkingDirectory = $dir; $lnk.IconLocation = (Join-Path $dir 'CareerDesk.exe'); $lnk.Description = 'CareerDesk'; $lnk.Save(); Write-Output ('SHORTCUT_CREATED ' + (Join-Path $desktop 'CareerDesk.lnk'))"
if errorlevel 1 (
  echo Failed to create the shortcut. You can right-click CareerDesk.exe and use Send to Desktop instead.
  echo 创建失败。也可以右键 CareerDesk.exe，选择“发送到 - 桌面快捷方式”。
) else (
  echo Done. Launch CareerDesk from the desktop icon from now on.
  echo 完成。以后从桌面图标启动 CareerDesk 即可。
)
echo If you move this folder later, double-click this file again to refresh the shortcut.
echo 以后如果移动了这个文件夹，再双击本文件刷新快捷方式。
pause
