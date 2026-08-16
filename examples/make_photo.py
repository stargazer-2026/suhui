#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_photo.py — 生成含 EXIF 时间戳的示例照片（TIFF，纯标准库 struct 构造）

用于验证 parse.py 的照片解析路径（EXIF 时间线，§4.49）：
  python3 make_photo.py [--out photos/]

生成 3 张 TIFF：img_001.tif (2030-06-01) / img_002.tif (2030-06-10) /
img_003.tif (2030-07-20)，均为合成占位数据。
"""
import os
import struct
import sys

# TIFF 类型
SHORT, LONG, ASCII = 3, 4, 2


def make_tiff(datetime_str, make="SyntheticCam", width=64, height=64):
    """
    构造最小合法 TIFF + EXIF 标签（DateTime / DateTimeOriginal / DateTimeDigitized）。
    datetime_str 形如 "2030:06:01 10:00:00"（EXIF 标准用冒号分隔）。
    """
    endian = "II"          # 小端
    entries = [
        (0x0100, SHORT, 1, width),       # ImageWidth
        (0x0101, SHORT, 1, height),      # ImageLength
        (0x0102, SHORT, 1, 8),           # BitsPerSample
        (0x0103, SHORT, 1, 1),           # Compression = none
        (0x0112, SHORT, 1, 1),           # Orientation
        (0x010F, ASCII, len(make) + 1, make + "\x00"),  # Make
        (0x0132, ASCII, 20, datetime_str + "\x00"),     # DateTime
        (0x9003, ASCII, 20, datetime_str + "\x00"),     # DateTimeOriginal
        (0x9004, ASCII, 20, datetime_str + "\x00"),     # DateTimeDigitized
    ]
    # 每个 entry 12 字节：tag(2) type(2) count(4) value/offset(4)
    n = len(entries)
    ifd_offset = 8
    value_area_offset = ifd_offset + 2 + n * 12 + 4

    header = endian.encode() + struct.pack("<H", 42) + struct.pack("<I", ifd_offset)

    ifd = struct.pack("<H", n)
    extra = bytearray()
    value_offsets = {}
    for tag, typ, count, value in entries:
        if typ == ASCII:
            data = value.encode("latin-1") if isinstance(value, str) else value
            count = len(data)
            if count <= 4:
                val_field = data.ljust(4, b"\x00")
            else:
                off = value_area_offset + len(extra)
                value_offsets[tag] = off
                extra += data
                val_field = struct.pack("<I", off)
        elif typ == SHORT:
            val_field = struct.pack("<H", value).ljust(4, b"\x00")
        else:
            val_field = struct.pack("<I", value)
        ifd += struct.pack("<HHI", tag, typ, count) + val_field
    ifd += struct.pack("<I", 0)  # next IFD = none

    # 1x1 像素数据（64x64 灰度占位）
    pixels = bytes([128]) * (width * height)

    return header + ifd + bytes(extra) + pixels


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "photos")
    os.makedirs(out, exist_ok=True)
    samples = [
        ("img_001.tif", "2030:06:01 10:00:00"),
        ("img_002.tif", "2030:06:10 19:30:00"),
        ("img_003.tif", "2030:07:20 21:05:00"),
    ]
    for name, ts in samples:
        path = os.path.join(out, name)
        with open(path, "wb") as f:
            f.write(make_tiff(ts))
        print("已生成: %s（EXIF %s）" % (path, ts.replace(":", "-", 2)))
    print("完成——可运行: python3 scripts/parse.py examples/photos --out <dir>")


if __name__ == "__main__":
    main()
