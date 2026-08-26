' Sable silent launcher — runs start.bat with no visible window
' Used by autostart registry entry
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
WshShell.Run "cmd /c start.bat --background", 0, False
