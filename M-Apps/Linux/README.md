# How to make elf

**Just convert bash to cpp and then cpp to elf**

**Disable search mode in Deepseek , tell to convert , then recheck for bugs**

**Type this command in linux to get elf:**

*C++ → ELF*
```bash
g++ boss.cpp -O2 -o boss
```

*if you make c , then*

*C → ELF*
```bash
gcc boss.c -O2 -o boss
```


## On both Linux and macOS, use `chmod +x` to give a Bash script executable permission.

```
chmod +x boss.sh
```

**Then run it with:**

```
./boss.sh
```

