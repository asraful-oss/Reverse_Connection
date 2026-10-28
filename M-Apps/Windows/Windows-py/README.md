## if you use online ps2exe converter 
## Or, if you try to convert c++ to exe

**Then windows defender flag the exe as threat**

**Only use this method:**

*Install ps2exe*
```bash
Install-Module ps2exe -Scope CurrentUser
```
*Bypass ontime window for next process*
```bash
Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process
```
*Turn ps1 to exe*
```bash
Invoke-PS2EXE .\compile.ps1 .\compile_tool.exe
```

