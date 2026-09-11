' Sable - Silent launcher (Windows)
' Used by BOTH the Task Scheduler auto-start AND manual double-click.
' wscript.exe is a GUI-subsystem binary -> it NEVER allocates a console.
' Flag 0 = run the target fully hidden. Result: zero popup windows,
' at boot or on demand.

Set sh = CreateObject("WScript.Shell")
here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
ps1 = here & "\start.ps1"
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps1 & """"
sh.Run cmd, 0, False
