' ----------------------------------------------------------------------
' Lanza el Teleprompter de forma 100% silenciosa:
'  - No abre ventana de CMD.
'  - No abre ventana de PowerShell.
'  - El proceso queda corriendo en background (pythonw.exe).
'
' Doble-click sobre este archivo es suficiente.
' ----------------------------------------------------------------------
Option Explicit

Dim shell, fso, scriptDir, projectRoot, pythonw, launcher, cmdLine

Set shell = CreateObject("WScript.Shell")
Set fso   = CreateObject("Scripting.FileSystemObject")

scriptDir   = fso.GetParentFolderName(WScript.ScriptFullName)
projectRoot = fso.GetParentFolderName(scriptDir)
pythonw     = projectRoot & "\.venv\Scripts\pythonw.exe"
launcher    = projectRoot & "\launch.py"

If Not fso.FileExists(pythonw) Then
    MsgBox "Entorno virtual no encontrado." & vbCrLf & _
           "Ejecuta scripts\install.bat primero.", _
           vbCritical, "Teleprompter"
    WScript.Quit 1
End If

If Not fso.FileExists(launcher) Then
    MsgBox "No se encontro launch.py en:" & vbCrLf & projectRoot, _
           vbCritical, "Teleprompter"
    WScript.Quit 1
End If

' Variables de entorno para el modelo STT.
shell.Environment("PROCESS").Item("TP_MODEL_SIZE") = "medium"
shell.Environment("PROCESS").Item("TP_DEVICE")     = "cuda"
shell.Environment("PROCESS").Item("TP_COMPUTE")    = "float16"
shell.Environment("PROCESS").Item("TP_LANGUAGE")   = "es"

' Fijar el cwd al raiz del proyecto.
shell.CurrentDirectory = projectRoot

' Run(command, windowStyle=0 (oculto), waitOnReturn=False (no bloquear)).
cmdLine = """" & pythonw & """ """ & launcher & """"
shell.Run cmdLine, 0, False
