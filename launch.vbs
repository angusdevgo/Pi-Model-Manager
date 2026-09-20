Dim fso, shell, scriptDir, appPy, pythonw, cmd

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
appPy = scriptDir & "\desktop_app.py"

Function FindPythonw()
    On Error Resume Next
    Dim whereExec, outLine
    Set whereExec = shell.Exec("where pythonw.exe")
    If Not whereExec Is Nothing Then
        outLine = Trim(whereExec.StdOut.ReadLine())
        If outLine <> "" And fso.FileExists(outLine) Then
            FindPythonw = outLine
            Exit Function
        End If
    End If
    On Error GoTo 0

    Dim localAppData, programFiles, programFilesX86, dirs(3), i, d, folder, subF, p
    localAppData = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%")
    programFiles = shell.ExpandEnvironmentStrings("%ProgramFiles%")
    programFilesX86 = shell.ExpandEnvironmentStrings("%ProgramFiles(x86)%")

    dirs(0) = localAppData & "\Programs\Python"
    dirs(1) = programFiles & "\Python"
    dirs(2) = programFilesX86 & "\Python"
    dirs(3) = "C:\Python"

    For i = 0 To 3
        d = dirs(i)
        If fso.FolderExists(d) Then
            Set folder = fso.GetFolder(d)
            For Each subF In folder.SubFolders
                p = subF.Path & "\pythonw.exe"
                If fso.FileExists(p) Then
                    FindPythonw = p
                    Exit Function
                End If
            Next
        End If
    Next

    FindPythonw = "pythonw.exe"
End Function

pythonw = FindPythonw()
cmd = """" & pythonw & """ """ & appPy & """"
shell.Run cmd, 0, False
