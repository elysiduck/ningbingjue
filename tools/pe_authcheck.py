#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""pe_authcheck.py — 离线核验 Windows PE 的数字签名 + 版本资源（纯 stdlib）。

为什么要有这个工具
==================
本机安全策略禁用了 reg / signtool 一类系统级工具，PowerShell 的
Get-AuthenticodeSignature 也常被 Core Isolation / 执行策略挡住。而判断一个
第三方 exe（ryzenadj、HWiNFO、inpoutx64 之类）是不是被改过，只需要两件事：

  1. 自己按 Authenticode 规范算一遍文件哈希；
  2. 去签名 DER 里翻出官方当时签的 messageDigest，两者比对。

**对得上 = 字节零改动**；对不上 = 签名之后被人动过（加壳/破解/注入）。
这一步不需要联网、不需要管理员、不需要任何三方库。

用法::

    python tools/pe_authcheck.py <file.exe> [<file2.dll> ...]

退出码：0 = 全部签名有效；1 = 至少一个无效/无签名；2 = 用法或解析错误。

Authenticode 哈希算法（SHA-256）
================================
按 "Windows Authenticode Portable Executable Signature Format" 顺序拼接：

  1. 文件开头 → Optional Header 的 Checksum 字段之前
  2. 跳过 Checksum（4 字节）
  3. Checksum 之后 → 数据目录第 4 项（SECURITY）之前
  4. 跳过 SECURITY 目录项（8 字节）
  5. SECURITY 之后 → SizeOfHeaders 结束
  6. 各节按 PointerToRawData 升序，逐节喂 SizeOfRawData 字节
  7. 最后一节之后、证书之前的多余字节（通常没有）
  —— 证书 blob 本身全程排除。

坑（本工具已处理）
==================
* 资源目录的 NumberOfNamedEntries / NumberOfIdEntries 在 **偏移 +12**，不是 +0。
  +0 读到的是 Characteristics/TimeDateStamp，恒为 0 → 会误判成"无版本资源"。
* 命名资源的名字偏移是相对**资源目录基址**（rb），不是相对当前层目录。
  写成 rb+off 会在多层目录时算出天文数字偏移直接崩。
* PE32 与 PE32+ 的 Checksum 字段都在 Optional Header 偏移 64，可以统一处理。
* SECURITY 数据目录里存的"RVA"其实是**文件偏移**，直接当偏移用，别做 RVA 换算。
* UPX 加壳**不等于**被篡改：只要哈希对得上，说明是"先加壳后签名"（官方行为）。
  HWiNFO64.exe 就是 UPX 5.11 加壳且签名有效。
"""

from __future__ import annotations

import hashlib
import struct
import sys

# AlgorithmIdentifier 前缀：06 09 60 86 48 01 65 03 04 02 01 = OID 2.16.840.1.101.3.4.2.1 (SHA-256)
# 后面紧跟 05 00 (NULL) + 04 20 (OCTET STRING, 32 字节) + 32 字节摘要
_SHA256_PREFIX = bytes.fromhex("0609608648016503040201" + "0500" + "0420")
_SHA1_PREFIX = bytes.fromhex("06092a864886f70d010105" + "0500" + "0414")


class PEError(Exception):
    pass


class PEFile:
    def __init__(self, path: str) -> None:
        self.path = path
        with open(path, "rb") as fh:
            self.data = fh.read()
        d = self.data
        if d[:2] != b"MZ":
            raise PEError("不是 PE 文件（缺少 MZ 头）")
        self.e_lfanew = struct.unpack_from("<I", d, 0x3C)[0]
        if d[self.e_lfanew:self.e_lfanew + 4] != b"PE\0\0":
            raise PEError("PE 签名损坏")
        self.nsec = struct.unpack_from("<H", d, self.e_lfanew + 6)[0]
        self.optsz = struct.unpack_from("<H", d, self.e_lfanew + 20)[0]
        self.optoff = self.e_lfanew + 24
        self.is64 = struct.unpack_from("<H", d, self.optoff)[0] == 0x20B
        self.dd_off = self.optoff + (112 if self.is64 else 96)
        self.size_of_headers = struct.unpack_from("<I", d, self.optoff + 60)[0]
        self.csum_off = self.optoff + 64          # PE32 / PE32+ 一致
        self.sec_off = self.optoff + self.optsz
        self.sections = []
        for i in range(self.nsec):
            o = self.sec_off + i * 40
            name = d[o:o + 8].rstrip(b"\0").decode("ascii", "replace")
            vsize, vaddr, rsize, raddr = struct.unpack_from("<IIII", d, o + 8)
            self.sections.append({"name": name, "vaddr": vaddr, "vsize": vsize,
                                  "raddr": raddr, "rsize": rsize})

    def data_dir(self, index: int) -> tuple[int, int]:
        return struct.unpack_from("<II", self.data, self.dd_off + index * 8)

    def rva2off(self, rva: int) -> int | None:
        for s in self.sections:
            if s["vaddr"] <= rva < s["vaddr"] + max(s["vsize"], s["rsize"]):
                return s["raddr"] + (rva - s["vaddr"])
        return None

    # -- 签名 ------------------------------------------------------------
    def cert_blob(self) -> tuple[int, bytes] | None:
        """返回 (文件偏移, WIN_CERTIFICATE 之后的 DER 字节)。无签名返回 None。"""
        rva, size = self.data_dir(4)     # SECURITY 目录：这里的 "RVA" 就是文件偏移
        if size == 0:
            return None
        off = rva
        if off + 8 > len(self.data):
            return None
        length = struct.unpack_from("<I", self.data, off)[0]
        rev, ctype = struct.unpack_from("<HH", self.data, off + 4)
        if length < 8 or off + length > len(self.data):
            return None
        return off, (self.data[off + 8:off + length], rev, ctype)

    def authenticode_hashes(self) -> dict[str, bytes]:
        """同时算 SHA-256 与 SHA-1（老签名可能只有 SHA-1）。"""
        d = self.data
        cert_off, _ = self.data_dir(4)
        cert = self.cert_blob()
        cert_start = cert[0] if cert else len(d)
        dd_sec = self.dd_off + 4 * 8
        out = {}
        for algo in ("sha256", "sha1"):
            h = hashlib.new(algo)

            def feed(a: int, b: int) -> None:
                if b > a:
                    h.update(d[a:b])

            feed(0, self.csum_off)                        # 1
            feed(self.csum_off + 4, dd_sec)               # 2 + 3（跳过 Checksum）
            feed(dd_sec + 8, self.size_of_headers)        # 4 + 5（跳过 SECURITY 项）
            end = self.size_of_headers
            for s in sorted(self.sections, key=lambda x: x["raddr"]):
                if s["rsize"] == 0 or s["raddr"] == 0:
                    continue
                h.update(d[s["raddr"]:s["raddr"] + s["rsize"]])
                end = max(end, s["raddr"] + s["rsize"])
            if cert_start > end:                          # 7
                h.update(d[end:cert_start])
            out[algo] = h.digest()
        return out

    def verify_signature(self) -> tuple[str, str]:
        """返回 (判定, 说明)。判定 ∈ valid / invalid / unsigned。"""
        cert = self.cert_blob()
        if cert is None:
            return ("unsigned", "无 Authenticode 签名（未签名或签名被剥离）")
        blob, rev, ctype = cert[1]
        hashes = self.authenticode_hashes()
        for algo, prefix in (("sha256", _SHA256_PREFIX), ("sha1", _SHA1_PREFIX)):
            i = blob.find(prefix + hashes[algo])
            if i >= 0:
                return ("valid",
                        f"{algo.upper()} 摘要命中（DER 偏移 0x{i:X}）→ 字节零改动，签名有效")
        cands = []
        for m in range(0, max(0, len(blob) - 34)):
            if blob[m:m + 2] == b"\x04\x20":
                cands.append(blob[m + 2:m + 34].hex())
        hint = ""
        if cands:
            hint = f"；签名内记录的摘要前 3 个候选: {', '.join(cands[:3])}"
        return ("invalid", "当前字节的摘要在签名里找不到 → 签名后被改动过" + hint)

    # -- 版本资源 --------------------------------------------------------
    def _read_dir(self, off: int, rb: int) -> list[tuple]:
        d = self.data
        named, idd = struct.unpack_from("<HH", d, off + 12)   # 坑：+12 不是 +0
        out = []
        for i in range(named + idd):
            o = off + 16 + i * 8
            if i < named:
                noff = struct.unpack_from("<I", d, o)[0] & 0x7FFFFFFF
                try:
                    ln = struct.unpack_from("<H", d, rb + noff)[0]   # 坑：相对 rb
                    ident = d[rb + noff + 2:rb + noff + 2 + ln * 2].decode("utf-16-le", "replace")
                except Exception:
                    ident = f"<bad:{noff}>"
            else:
                ident = struct.unpack_from("<I", d, o)[0]
            sub = struct.unpack_from("<I", d, o + 4)[0]
            out.append((ident, bool(sub & 0x80000000), sub & 0x7FFFFFFF))
        return out

    def _parse_ver(self, base: int) -> tuple[str, object]:
        d = self.data

        def align4(x: int) -> int:
            return (x + 3) & ~3

        ln, vlen, vtype = struct.unpack_from("<HHH", d, base)
        # szKey 是 NUL 结尾的 UTF-16 串，长度**不能**用 wValueLength 算——
        # wValueLength 是后面 Value 的字节数。这里写错会让后续所有偏移错位。
        p = base + 6
        chars = []
        while p + 2 <= len(d):
            ch = d[p:p + 2]
            p += 2
            if ch == b"\x00\x00":
                break
            chars.append(ch)
        key = b"".join(chars).decode("utf-16-le", "replace")
        # 注意：不能在这里按 wType 判断提前返回——StringFileInfo 是 wType=1
        # 但 wValueLength=0 的**容器**节点，提前返回会把整棵子树丢掉。
        # vlen==0 的情况由下面的 `if vlen:` 自然跳过即可。
        pos = align4(p)
        fixed = None
        if vlen:
            # 长度单位随 wType 变：0=二进制按字节；1=文本按 WCHAR（word）。
            nbytes = vlen * 2 if vtype == 1 else vlen
            blob = d[pos:pos + nbytes]
            if blob[:4] == b"\xbd\x04\xef\xfe" and vlen >= 52:
                fixed = struct.unpack_from("<13I", d, pos)
            elif vtype == 1:
                fixed = blob.decode("utf-16-le", "replace").rstrip("\x00")
            pos = align4(pos + nbytes)
        children: dict[str, tuple] = {}
        guard = 0
        while pos < base + ln and guard < 300:
            guard += 1
            cln = struct.unpack_from("<H", d, pos)[0]
            if cln == 0:
                break
            k, f, c = self._parse_ver(pos)
            children[k] = (k, f, c)
            pos += cln
            pos = align4(pos)
        # (键名, 固定值/文本值, 子字典)
        return key, fixed, children
    def version_info(self) -> dict[str, str]:
        """返回 StringFileInfo 里的键值对 + 固定版本号。读不到返回空 dict。"""
        rva, _ = self.data_dir(2)
        rb = self.rva2off(rva)
        if rb is None:
            return {}
        out: dict[str, str] = {}
        for t, isdir, off in self._read_dir(rb, rb):
            if not isdir or t != 16:       # RT_VERSION = 16
                continue
            for _nm, isdir2, off2 in self._read_dir(rb + off, rb):
                if not isdir2:
                    continue
                for _lang, isdir3, off3 in self._read_dir(rb + off2, rb):
                    drva, _dsize, _dcp = struct.unpack_from("<III", self.data, rb + off3)
                    o = self.rva2off(drva)
                    if o is None:
                        continue
                    try:
                        _k, fx, ch = self._parse_ver(o)
                    except Exception:
                        continue
                    if isinstance(fx, tuple) and len(fx) >= 6:
                        _s, _sv, fv_ms, fv_ls, pv_ms, pv_ls = fx[:6]
                        # 版本号字段顺序：MS_hi.MS_lo.LS_hi.LS_lo（major.minor.build.rev）
                        out["_FileVersion"] = "%d.%d.%d.%d" % (
                            fv_ms >> 16, fv_ms & 0xFFFF, fv_ls >> 16, fv_ls & 0xFFFF)
                        out["_ProductVersion"] = "%d.%d.%d.%d" % (
                            pv_ms >> 16, pv_ms & 0xFFFF, pv_ls >> 16, pv_ls & 0xFFFF)

                    def walk(node: tuple) -> None:
                        _key, fixed, kids = node
                        if kids:
                            for child in kids.values():
                                walk(child)
                        elif isinstance(fixed, str) and fixed:
                            out.setdefault(_key, fixed)
                    walk((_k, fx, ch))
        return out


def check(path: str) -> int:
    print("=" * 72)
    print("文件: %s" % path)
    try:
        pe = PEFile(path)
    except PEError as e:
        print("  解析失败: %s" % e)
        return 2
    except OSError as e:
        print("  读取失败: %s" % e)
        return 2

    print("  大小      : %d 字节" % len(pe.data))
    print("  节表      : %s" % ", ".join(s["name"] for s in pe.sections))
    packed = [s["name"] for s in pe.sections if s["name"].lower().startswith("upx")]
    if packed:
        print("  加壳      : 检测到 %s（加壳本身不等于被篡改，看下面签名判定）" % "/".join(packed))

    verdict, detail = pe.verify_signature()
    print("  签名      : %s —— %s" % (verdict.upper(), detail))

    vi = pe.version_info()
    if vi:
        for key in ("_FileVersion", "_ProductVersion", "CompanyName", "ProductName",
                    "FileDescription", "LegalCopyright", "InternalName", "OriginalFilename"):
            if key in vi:
                print("  %-10s: %s" % (key.lstrip("_"), vi[key]))
    else:
        print("  版本资源  : 无（UPX 连资源一起压了，或本就没有）")
    return 0 if verdict == "valid" else 1


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    rc = 0
    for p in argv[1:]:
        rc |= check(p)
    print("=" * 72)
    print("汇总: %s" % ("全部签名有效" if rc == 0 else "存在无效/无签名的文件"))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
