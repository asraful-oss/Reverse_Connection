## if you use online ps2exe converter 
## Or, if you try to convert c++ to exe

**Then windows defender flag the exe as threat**

<details>
  <summary><b>Only use this method</b></summary>
  <p>
  
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
</p>
</details>

## Or use ps2exe.bat , it's automated

## If you want: cpp to exe , auomated, use: c-cpp2exe.bat


# For icon in exe: 

**Use Resource Hacker**

**Convert your image to ico using image to ico online converter**

**Add image and click file and click save**

**Boom! You got exe with icon and previous exe saved as: {name}_original.exe

**Link >**  https://www.angusj.com/resourcehacker
