@echo off
call "C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvarsall.bat" x64
cd /d "C:\Users\13567\WorkBuddy\2026-07-18-10-22-49\ningbingjue\src\ningbingjue\vendor\xinput_gate"
dumpbin /exports xinput1_3.dll
