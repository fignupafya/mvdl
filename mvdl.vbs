' mvdl - no-console launcher for Windows (recommended).
' Opens the app window with pyw (Python without a console). No terminal stays open.
' If nothing happens, run baslat.bat instead to see the error.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
appDir  = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = appDir
sh.Run "pyw """ & appDir & "\app.py""", 0, False
