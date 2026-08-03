# Create a shortcut to the headless Windows launcher.
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut("$root\CareerDesk.lnk")
$lnk.TargetPath = "wscript.exe"
$lnk.Arguments = "`"$root\start-hidden.vbs`""
$lnk.WorkingDirectory = $root
$lnk.IconLocation = "$root\careerdesk.ico"
$lnk.Description = "CareerDesk 求职助手"
$lnk.Save()
Write-Host "已生成 $root\CareerDesk.lnk —— 双击它即可静默启动（无黑框），图标为 CareerDesk logo。"
