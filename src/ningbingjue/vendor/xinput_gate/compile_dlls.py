"""compile_dlls.py — 编译 fake XInput DLLs

用 MSVC Build Tools 18 编译三个 64 位 DLL。
把编译命令写到临时 .bat 文件再运行，避免 Python→cmd 引号转义问题。
"""
import subprocess
import sys
import os
import tempfile

VCVARS = r"C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvarsall.bat"
SRC_DIR = os.path.dirname(os.path.abspath(__file__))

BUILDS = [
    ("xinput1_3.dll",   "WANT_XINPUT13",  "xinput1_3.def"),
    ("xinput1_4.dll",   "WANT_XINPUT14",  "xinput1_4.def"),
    ("XInput9_1_0.dll", "WANT_XINPUT910", "XInput9_1_0.def"),
]

def run_bat(bat_content, cwd):
    """把 bat_content 写到临时 .bat 文件，运行它，返回 (stdout, stderr, rc)。"""
    # 临时 .bat 文件用纯 ASCII，避免编码问题
    tmpfd, tmppath = tempfile.mkstemp(suffix=".bat", dir=cwd)
    try:
        with os.fdopen(tmpfd, "w", encoding="ascii", newline="\r\n") as f:
            f.write("@echo off\r\n")
            f.write(bat_content)
        # cmd /c 运行临时 .bat（路径无空格无引号，不会出问题）
        r = subprocess.run(
            ["cmd.exe", "/c", tmppath],
            capture_output=True, cwd=cwd, timeout=120
        )
        out = r.stdout.decode("gbk", errors="replace")
        err = r.stderr.decode("gbk", errors="replace")
        return out, err, r.returncode
    finally:
        try:
            os.remove(tmppath)
        except OSError:
            pass

def main():
    if not os.path.exists(VCVARS):
        print(f"ERROR: vcvarsall.bat not found: {VCVARS}")
        sys.exit(1)

    all_ok = True
    for dll_name, define, def_file in BUILDS:
        print(f"\n=== Building {dll_name} ===")
        # 构建纯 ASCII 的 .bat 内容
        bat = (
            f'call "{VCVARS}" x64\r\n'
            f'cd /d "{SRC_DIR}"\r\n'
            f'cl /nologo /LD /O2 /D{define} '
            f'/Fe:{dll_name} xinput_gate.c /link /DEF:{def_file}\r\n'
        )
        out, err, rc = run_bat(bat, SRC_DIR)
        if out.strip():
            print(out.rstrip())
        if err.strip():
            print("STDERR:", err.rstrip())
        if rc != 0:
            print(f"FAILED: exit code {rc}")
            all_ok = False
        else:
            dll_path = os.path.join(SRC_DIR, dll_name)
            if os.path.exists(dll_path):
                size = os.path.getsize(dll_path)
                print(f"OK: {dll_name} ({size} bytes)")
            else:
                print(f"WARNING: exit code 0 but {dll_name} not found")
                all_ok = False

    if all_ok:
        print("\n=== All builds succeeded ===")
        # 验证导出表
        print("\nVerifying exports (xinput1_3.dll):")
        bat_verify = (
            f'call "{VCVARS}" x64\r\n'
            f'cd /d "{SRC_DIR}"\r\n'
            f'dumpbin /exports xinput1_3.dll\r\n'
        )
        out, _, _ = run_bat(bat_verify, SRC_DIR)
        for line in out.splitlines():
            if "XInput" in line:
                print(f"  {line.strip()}")
    else:
        print("\n=== Some builds FAILED ===")
        sys.exit(1)

    # 清理中间文件
    for f in os.listdir(SRC_DIR):
        if f.lower().endswith((".obj", ".exp", ".lib")):
            try:
                os.remove(os.path.join(SRC_DIR, f))
            except OSError:
                pass

if __name__ == "__main__":
    main()
