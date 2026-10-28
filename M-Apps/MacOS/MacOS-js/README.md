# On macOS, use Clang to create native Mach-O executables.

**Just convert bash to cpp and then cpp to elf**

## Upload both auto.js and auto.sh and then run :

```bash
bash auto.sh auto.js --hidden
```

*C++ → Mach-O*
```
clang++ boss.cpp -O2 -o boss
```

*C → Mach-O*
```
clang boss.c -O2 -o boss
```


### If Clang isn't installed

**Install Apple's Command Line Tools:**

```
xcode-select --install
```


## On both Linux and macOS, use `chmod +x` to give a Bash script executable permission.

```
chmod +x boss.sh
```

**Then run it with:**

```
./boss.sh
```
