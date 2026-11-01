#!/usr/bin/env python3

# Unsuper - The fastest Android super.img dumper
# Made by fl0w
# https://github.com/codefl0w

import argparse
import bisect
import concurrent.futures
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import time
from typing import BinaryIO, Callable, Dict, List, Optional, Tuple, Union

VERSION = "3.0.0"

# Constants
SPARSE_HEADER_MAGIC = 0xED26FF3A
SPARSE_HEADER_SIZE = 28
SPARSE_CHUNK_HEADER_SIZE = 12

CHUNK_TYPE_RAW = 0xCAC1
CHUNK_TYPE_FILL = 0xCAC2
CHUNK_TYPE_DONT_CARE = 0xCAC3
CHUNK_TYPE_CRC32 = 0xCAC4

LP_PARTITION_RESERVED_BYTES = 4096
LP_METADATA_GEOMETRY_MAGIC = 0x616C4467
LP_METADATA_GEOMETRY_SIZE = 4096
LP_METADATA_HEADER_MAGIC = 0x414C5030
LP_SECTOR_SIZE = 512

LP_TARGET_TYPE_LINEAR = 0

# Optimized buffer size for I/O operations (4MB)
BUFFER_SIZE = 4 * 1024 * 1024


class FastDumperError(Exception):
    """Custom exception for dumper errors."""
    pass


@dataclass
class ExtentInfo:
    """Logical partition extent information."""
    offset: int
    size: int


@dataclass
class PartitionInfo:
    """Logical partition information."""
    name: str
    extents: List[ExtentInfo]
    total_size: int


@dataclass
class SparseChunk:
    """In-memory descriptor for a sparse image chunk."""
    unsparse_offset: int
    unsparse_size: int
    chunk_type: int
    file_offset: int
    fill_bytes: bytes


class SparseHeader:
    """Sparse image header parser."""
    def __init__(self, buffer: bytes):
        fmt = '<I4H4I'
        required_size = struct.calcsize(fmt)
        if len(buffer) < required_size:
            raise FastDumperError(f"Sparse header too short: got {len(buffer)} bytes, need {required_size}")

        (
            self.magic,
            self.major_version,
            self.minor_version,
            self.file_hdr_sz,
            self.chunk_hdr_sz,
            self.blk_sz,
            self.total_blks,
            self.total_chunks,
            self.image_checksum
        ) = struct.unpack_from(fmt, buffer)


class SparseChunkHeader:
    """Sparse chunk header parser."""
    def __init__(self, buffer: bytes):
        fmt = '<2H2I'
        required_size = struct.calcsize(fmt)
        if len(buffer) < required_size:
            raise FastDumperError(f"Chunk header too short: got {len(buffer)} bytes, need {required_size}")

        (
            self.chunk_type,
            self.reserved,
            self.chunk_sz,
            self.total_sz,
        ) = struct.unpack_from(fmt, buffer)


class SparseImage:
    """
    In-memory indexed sparse reader.
    Enables reading metadata and streaming partition extents directly from sparse images
    with zero intermediate unsparse disk writes.
    """
    def __init__(self, filepath: str):
        self.filepath = str(filepath)
        self.chunks: List[SparseChunk] = []
        self.chunk_starts: List[int] = []
        self.blk_sz: int = 4096
        self.unsparse_size: int = 0
        self._index_chunks()

    def _index_chunks(self) -> None:
        with open(self.filepath, 'rb') as f:
            header_data = f.read(SPARSE_HEADER_SIZE)
            if len(header_data) < SPARSE_HEADER_SIZE:
                raise FastDumperError(f"File too short to read sparse header: {len(header_data)} bytes")

            header = SparseHeader(header_data)
            if header.magic != SPARSE_HEADER_MAGIC:
                raise FastDumperError(f"Invalid sparse image magic: 0x{header.magic:08x}")
            if header.blk_sz == 0:
                raise FastDumperError("Invalid block size in sparse header: 0")

            self.blk_sz = header.blk_sz
            self.unsparse_size = header.total_blks * header.blk_sz

            # Seek to first chunk header
            f.seek(header.file_hdr_sz)

            curr_unsparse = 0
            for chunk_idx in range(header.total_chunks):
                chunk_header_data = f.read(SPARSE_CHUNK_HEADER_SIZE)
                if len(chunk_header_data) < SPARSE_CHUNK_HEADER_SIZE:
                    raise FastDumperError(f"Unexpected EOF while reading chunk {chunk_idx + 1}/{header.total_chunks} header")

                chunk_type, _, chunk_sz, total_sz = struct.unpack_from('<2H2I', chunk_header_data)

                # Skip extra header bytes if chunk header size > 12
                if header.chunk_hdr_sz > SPARSE_CHUNK_HEADER_SIZE:
                    f.seek(header.chunk_hdr_sz - SPARSE_CHUNK_HEADER_SIZE, os.SEEK_CUR)

                chunk_data_sz = total_sz - header.chunk_hdr_sz
                out_sz = chunk_sz * header.blk_sz

                if chunk_type == CHUNK_TYPE_RAW:
                    data_offset = f.tell()
                    self.chunks.append(SparseChunk(curr_unsparse, out_sz, chunk_type, data_offset, b''))
                    self.chunk_starts.append(curr_unsparse)
                    f.seek(chunk_data_sz, os.SEEK_CUR)
                    curr_unsparse += out_sz

                elif chunk_type == CHUNK_TYPE_FILL:
                    fill_bytes = f.read(4)
                    if len(fill_bytes) < 4:
                        raise FastDumperError(f"Unexpected EOF while reading fill data in chunk {chunk_idx + 1}")
                    if chunk_data_sz > 4:
                        f.seek(chunk_data_sz - 4, os.SEEK_CUR)
                    self.chunks.append(SparseChunk(curr_unsparse, out_sz, chunk_type, 0, fill_bytes))
                    self.chunk_starts.append(curr_unsparse)
                    curr_unsparse += out_sz

                elif chunk_type == CHUNK_TYPE_DONT_CARE:
                    if chunk_data_sz > 0:
                        f.seek(chunk_data_sz, os.SEEK_CUR)
                    self.chunks.append(SparseChunk(curr_unsparse, out_sz, chunk_type, 0, b''))
                    self.chunk_starts.append(curr_unsparse)
                    curr_unsparse += out_sz

                elif chunk_type == CHUNK_TYPE_CRC32:
                    if chunk_data_sz > 0:
                        f.seek(chunk_data_sz, os.SEEK_CUR)
                    # CRC chunks do not produce unsparse blocks

                else:
                    # Unknown chunk type; skip payload and advance logical offset
                    if chunk_data_sz > 0:
                        f.seek(chunk_data_sz, os.SEEK_CUR)
                    self.chunks.append(SparseChunk(curr_unsparse, out_sz, chunk_type, 0, b''))
                    self.chunk_starts.append(curr_unsparse)
                    curr_unsparse += out_sz

    def read(self, offset: int, length: int) -> bytes:
        """Read arbitrary logical byte range from sparse image directly."""
        if length <= 0 or offset >= self.unsparse_size:
            return b''

        actual_len = min(length, self.unsparse_size - offset)
        out = bytearray(actual_len)
        view = memoryview(out)
        written = 0
        curr_off = offset
        rem = actual_len

        with open(self.filepath, 'rb') as f:
            idx = bisect.bisect_right(self.chunk_starts, curr_off) - 1
            if idx < 0:
                idx = 0

            while rem > 0 and idx < len(self.chunks):
                c = self.chunks[idx]
                chunk_end = c.unsparse_offset + c.unsparse_size
                overlap_start = max(curr_off, c.unsparse_offset)
                overlap_end = min(curr_off + rem, chunk_end)
                overlap_len = overlap_end - overlap_start

                dest_slice = view[written: written + overlap_len]

                if c.chunk_type == CHUNK_TYPE_RAW:
                    f.seek(c.file_offset + (overlap_start - c.unsparse_offset))
                    f.readinto(dest_slice)
                elif c.chunk_type == CHUNK_TYPE_FILL:
                    fill_pattern = c.fill_bytes * ((overlap_len + 3) // 4)
                    dest_slice[:] = fill_pattern[:overlap_len]
                else:
                    dest_slice[:] = b'\x00' * overlap_len

                written += overlap_len
                curr_off += overlap_len
                rem -= overlap_len
                idx += 1

        return bytes(out)

    def copy_range(self, f: BinaryIO, offset: int, length: int, outfile: BinaryIO, buf: bytearray) -> None:
        """Stream an unsparse logical range from the open sparse file into outfile."""
        if length <= 0 or offset >= self.unsparse_size:
            return

        actual_len = min(length, self.unsparse_size - offset)
        curr_off = offset
        rem = actual_len
        idx = bisect.bisect_right(self.chunk_starts, curr_off) - 1
        if idx < 0:
            idx = 0

        fill_template = None
        last_fill = None

        while rem > 0 and idx < len(self.chunks):
            c = self.chunks[idx]
            chunk_end = c.unsparse_offset + c.unsparse_size
            overlap_start = max(curr_off, c.unsparse_offset)
            overlap_end = min(curr_off + rem, chunk_end)
            overlap_len = overlap_end - overlap_start

            if c.chunk_type == CHUNK_TYPE_RAW:
                f.seek(c.file_offset + (overlap_start - c.unsparse_offset))
                to_read = overlap_len
                while to_read > 0:
                    step = min(len(buf), to_read)
                    r = f.readinto(memoryview(buf)[:step])
                    if not r:
                        break
                    outfile.write(memoryview(buf)[:r])
                    to_read -= r

            elif c.chunk_type == CHUNK_TYPE_FILL:
                if c.fill_bytes == b'\x00\x00\x00\x00':
                    # Sparse seek hole: instantaneous on NTFS/ext4/APFS
                    outfile.seek(overlap_len, os.SEEK_CUR)
                else:
                    if fill_template is None or last_fill != c.fill_bytes:
                        fill_template = c.fill_bytes * (len(buf) // 4)
                        last_fill = c.fill_bytes
                    to_write = overlap_len
                    while to_write > 0:
                        step = min(len(fill_template), to_write)
                        outfile.write(fill_template[:step])
                        to_write -= step

            else:  # DONT_CARE
                outfile.seek(overlap_len, os.SEEK_CUR)

            curr_off += overlap_len
            rem -= overlap_len
            idx += 1


class RawImage:
    """Direct reader for non-sparse raw unsparse disk images."""
    def __init__(self, filepath: str):
        self.filepath = str(filepath)
        self.unsparse_size: int = os.path.getsize(self.filepath)

    def read(self, offset: int, length: int) -> bytes:
        if length <= 0 or offset >= self.unsparse_size:
            return b''
        actual_len = min(length, self.unsparse_size - offset)
        with open(self.filepath, 'rb') as f:
            f.seek(offset)
            return f.read(actual_len)

    def copy_range(self, f: BinaryIO, offset: int, length: int, outfile: BinaryIO, buf: bytearray) -> None:
        if length <= 0 or offset >= self.unsparse_size:
            return
        actual_len = min(length, self.unsparse_size - offset)
        f.seek(offset)
        remaining = actual_len
        while remaining > 0:
            step = min(len(buf), remaining)
            bytes_read = f.readinto(memoryview(buf)[:step])
            if not bytes_read:
                break
            outfile.write(memoryview(buf)[:bytes_read])
            remaining -= bytes_read


class FastSparseConverter:
    """
    High-performance sparse to unsparse converter.
    Uses pure Python multi-megabyte streaming, sparse filesystem holes, and zero dependencies.
    """
    def __init__(self, input_file: str, temp_dir: Optional[str] = None):
        self.input_file = str(input_file)
        self.temp_dir = temp_dir

    def convert(self) -> str:
        if self.temp_dir:
            temp_path = Path(self.temp_dir)
            temp_path.mkdir(parents=True, exist_ok=True)
            output_file = str(temp_path / f"{Path(self.input_file).stem}.unsparse.img")
        else:
            fd, output_file = tempfile.mkstemp(
                suffix='.unsparse.img',
                prefix=f"{Path(self.input_file).stem}_",
                dir=None
            )
            os.close(fd)
            output_file = str(output_file)

        try:
            with open(self.input_file, 'rb') as infile:
                header_data = infile.read(SPARSE_HEADER_SIZE)
                if len(header_data) < SPARSE_HEADER_SIZE:
                    raise FastDumperError(f"File too short to read sparse header: {len(header_data)} bytes")

                header = SparseHeader(header_data)
                if header.magic != SPARSE_HEADER_MAGIC:
                    raise FastDumperError(f"Invalid sparse image magic: 0x{header.magic:08x}")
                if header.blk_sz == 0:
                    raise FastDumperError("Invalid block size: 0")

                estimated_size = header.total_blks * header.blk_sz

                with open(output_file, 'wb') as outfile:
                    if estimated_size > 0:
                        try:
                            outfile.truncate(estimated_size)
                        except OSError:
                            pass

                    infile.seek(header.file_hdr_sz)
                    buffer = bytearray(BUFFER_SIZE)

                    # Template cache for fill patterns
                    fill_template = None
                    last_fill = None

                    for chunk_idx in range(header.total_chunks):
                        chunk_header_data = infile.read(SPARSE_CHUNK_HEADER_SIZE)
                        if len(chunk_header_data) < SPARSE_CHUNK_HEADER_SIZE:
                            raise FastDumperError(f"Unexpected EOF while reading chunk {chunk_idx + 1} header")

                        chunk_type, _, chunk_sz, total_sz = struct.unpack_from('<2H2I', chunk_header_data)

                        if header.chunk_hdr_sz > SPARSE_CHUNK_HEADER_SIZE:
                            infile.seek(header.chunk_hdr_sz - SPARSE_CHUNK_HEADER_SIZE, os.SEEK_CUR)

                        chunk_data_size = total_sz - header.chunk_hdr_sz
                        output_size = chunk_sz * header.blk_sz

                        if chunk_type == CHUNK_TYPE_RAW:
                            remaining = chunk_data_size
                            while remaining > 0:
                                step = min(len(buffer), remaining)
                                bytes_read = infile.readinto(memoryview(buffer)[:step])
                                if not bytes_read:
                                    raise FastDumperError(f"Unexpected EOF reading raw data in chunk {chunk_idx + 1}")
                                outfile.write(memoryview(buffer)[:bytes_read])
                                remaining -= bytes_read

                        elif chunk_type == CHUNK_TYPE_FILL:
                            fill_data = infile.read(4)
                            if len(fill_data) < 4:
                                raise FastDumperError(f"Unexpected EOF reading fill data in chunk {chunk_idx + 1}")
                            if chunk_data_size > 4:
                                infile.seek(chunk_data_size - 4, os.SEEK_CUR)

                            if output_size > 0:
                                if fill_data == b'\x00\x00\x00\x00':
                                    outfile.seek(output_size, os.SEEK_CUR)
                                else:
                                    if fill_template is None or last_fill != fill_data:
                                        fill_template = fill_data * (BUFFER_SIZE // 4)
                                        last_fill = fill_data
                                    remaining = output_size
                                    while remaining > 0:
                                        write_size = min(len(fill_template), remaining)
                                        outfile.write(fill_template[:write_size])
                                        remaining -= write_size

                        elif chunk_type == CHUNK_TYPE_DONT_CARE:
                            if chunk_data_size > 0:
                                infile.seek(chunk_data_size, os.SEEK_CUR)
                            outfile.seek(output_size, os.SEEK_CUR)

                        elif chunk_type == CHUNK_TYPE_CRC32:
                            if chunk_data_size > 0:
                                infile.seek(chunk_data_size, os.SEEK_CUR)

                        else:
                            if chunk_data_size > 0:
                                infile.seek(chunk_data_size, os.SEEK_CUR)
                            outfile.seek(output_size, os.SEEK_CUR)

                    if estimated_size > 0:
                        outfile.truncate(estimated_size)

        except Exception:
            if os.path.exists(output_file):
                try:
                    os.remove(output_file)
                except OSError:
                    pass
            raise

        return output_file


class FastMetadataParser:
    """
    Optimized parser for Android Logical Partition (LP) metadata.
    Accepts SparseImage, RawImage, or raw buffer without creating disk tempfiles.
    """
    def __init__(self, source: Union[SparseImage, RawImage, str]):
        if isinstance(source, (SparseImage, RawImage)):
            self.image = source
        else:
            self.image = load_image_source(str(source))
        self.file_size = self.image.unsparse_size

    def parse_partitions(self, partition_names: Optional[List[str]] = None) -> List[PartitionInfo]:
        return self._parse_metadata(partition_names, include_empty=False)

    def parse_partitions_for_metadata(self, partition_names: Optional[List[str]] = None) -> List[PartitionInfo]:
        return self._parse_metadata(partition_names, include_empty=True)

    def _parse_metadata(self, partition_names: Optional[List[str]] = None, include_empty: bool = False) -> List[PartitionInfo]:
        total_size = self.image.unsparse_size
        if total_size < LP_PARTITION_RESERVED_BYTES + 44:
            raise FastDumperError("File too small to contain valid LP metadata")

        # 1. Read Geometry
        geom_bytes = self.image.read(LP_PARTITION_RESERVED_BYTES, 44)
        if len(geom_bytes) < 44:
            raise FastDumperError("Cannot read LP geometry data")

        magic, _ = struct.unpack_from('<2I', geom_bytes, 0)
        if magic != LP_METADATA_GEOMETRY_MAGIC:
            raise FastDumperError(f"Invalid geometry magic: 0x{magic:08x} (expected 0x{LP_METADATA_GEOMETRY_MAGIC:08x})")

        metadata_max_size, metadata_slot_count, _ = struct.unpack_from('<3I', geom_bytes, 32)
        if metadata_max_size == 0 or metadata_slot_count == 0:
            raise FastDumperError("Invalid metadata geometry: slot count or max size is 0")

        base_offset = LP_PARTITION_RESERVED_BYTES + (LP_METADATA_GEOMETRY_SIZE * 2)
        header_offset = base_offset

        if header_offset + 128 > total_size:
            raise FastDumperError("File too small for metadata header")

        # 2. Read Metadata Header
        header_bytes = self.image.read(header_offset, 128)
        if len(header_bytes) < 128:
            raise FastDumperError("Cannot read metadata header")

        magic = struct.unpack_from('<I', header_bytes, 0)[0]
        if magic != LP_METADATA_HEADER_MAGIC:
            # Check backup slot
            backup_offset = base_offset + (metadata_max_size * metadata_slot_count)
            if backup_offset + 128 > total_size:
                raise FastDumperError("File too small for backup metadata header")
            header_bytes = self.image.read(backup_offset, 128)
            if len(header_bytes) < 128:
                raise FastDumperError("Cannot read backup metadata header")
            magic = struct.unpack_from('<I', header_bytes, 0)[0]
            if magic != LP_METADATA_HEADER_MAGIC:
                raise FastDumperError(f"Invalid metadata header magic: 0x{magic:08x}")
            header_offset = backup_offset

        header_size, tables_size = struct.unpack_from('<2I', header_bytes, 8)
        if header_size < 80 or tables_size == 0:
            raise FastDumperError("Invalid metadata header sizes")

        part_offset, part_count, part_entry_size = struct.unpack_from('<3I', header_bytes, 80)
        extent_offset, extent_count, extent_entry_size = struct.unpack_from('<3I', header_bytes, 92)

        if part_entry_size < 52 or extent_entry_size < 24:
            raise FastDumperError("Invalid table entry sizes in metadata header")

        tables_start = header_offset + header_size
        part_table_start = tables_start + part_offset
        part_table_size = part_count * part_entry_size

        extent_table_start = tables_start + extent_offset
        extent_table_size = extent_count * extent_entry_size

        if part_table_start + part_table_size > total_size or extent_table_start + extent_table_size > total_size:
            raise FastDumperError("Metadata tables extend beyond image size")

        # 3. Read Tables
        partitions_data = self.image.read(part_table_start, part_table_size)
        extents_data = self.image.read(extent_table_start, extent_table_size)

        if len(partitions_data) < part_table_size or len(extents_data) < extent_table_size:
            raise FastDumperError("Failed to read complete partition or extent tables")

        # 4. Parse Extents
        extent_structs = []
        for i in range(extent_count):
            start_idx = i * extent_entry_size
            if start_idx + 20 <= len(extents_data):
                num_sectors, target_type, target_data = struct.unpack_from('<QIQ', extents_data, start_idx)
                extent_structs.append((num_sectors, target_type, target_data))

        # 5. Parse Partitions
        partitions = []
        for i in range(part_count):
            start_idx = i * part_entry_size
            if start_idx + 48 > len(partitions_data):
                break

            name_bytes, _, first_extent_idx, num_extents = struct.unpack_from('<36s3I', partitions_data, start_idx)
            name = name_bytes.split(b'\x00', 1)[0].decode('utf-8', errors='replace').strip()

            if not name or (partition_names and name not in partition_names):
                continue

            extents: List[ExtentInfo] = []
            total_part_size = 0

            if num_extents == 0 or first_extent_idx >= len(extent_structs):
                if include_empty:
                    partitions.append(PartitionInfo(name, extents, 0))
                continue

            for j in range(num_extents):
                idx = first_extent_idx + j
                if idx >= len(extent_structs):
                    break

                num_sectors, target_type, target_data = extent_structs[idx]
                if target_type != LP_TARGET_TYPE_LINEAR:
                    continue

                offset = target_data * LP_SECTOR_SIZE
                size = num_sectors * LP_SECTOR_SIZE

                if size > 0 and offset + size <= total_size:
                    # Merge adjacent contiguous extents
                    if extents and (extents[-1].offset + extents[-1].size == offset):
                        extents[-1].size += size
                    else:
                        extents.append(ExtentInfo(offset, size))
                    total_part_size += size

            if extents or include_empty:
                partitions.append(PartitionInfo(name, extents, total_part_size))

        return partitions

    def dump_metadata_json(self, output_dir: str, partition_names: Optional[List[str]] = None) -> None:
        partitions = self.parse_partitions_for_metadata(partition_names)

        # Extract logical block size
        geom_bytes = self.image.read(LP_PARTITION_RESERVED_BYTES, 44)
        if len(geom_bytes) >= 44:
            logical_block_size = struct.unpack_from('<I', geom_bytes, 40)[0]
        else:
            logical_block_size = LP_SECTOR_SIZE

        metadata = {
            "uses_unsuper": VERSION,
            "unsparse_image_size": self.image.unsparse_size,
            "block_size": logical_block_size,
            "partitions": []
        }

        for p in partitions:
            p_data = {
                "name": p.name,
                "size": p.total_size,
                "extents": [{"offset": e.offset, "length": e.size} for e in p.extents]
            }
            metadata["partitions"].append(p_data)

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        json_file = output_path / "metadata.json"

        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2)

    @staticmethod
    def create_metadata_from_images(image_paths: List[str], output_path: str,
                                    base_metadata: Optional[dict] = None) -> None:
        """Create metadata.json from individual partition images."""
        if base_metadata:
            metadata = base_metadata.copy()
            metadata["uses_unsuper"] = VERSION
        else:
            metadata = {
                "uses_unsuper": VERSION,
                "unsparse_image_size": 0,
                "block_size": LP_SECTOR_SIZE,
                "partitions": []
            }

        metadata["partitions"] = []
        total_size = 0
        current_offset = LP_PARTITION_RESERVED_BYTES + (LP_METADATA_GEOMETRY_SIZE * 2)

        for img_path in image_paths:
            img_file = Path(img_path)
            if not img_file.exists():
                print(f"Warning: Image file {img_path} not found, skipping...")
                continue

            partition_name = img_file.stem
            if partition_name.endswith('.img'):
                partition_name = partition_name[:-4]

            file_size = img_file.stat().st_size
            total_size += file_size

            metadata["partitions"].append({
                "name": partition_name,
                "size": file_size,
                "extents": [{
                    "offset": current_offset,
                    "length": file_size
                }]
            })
            current_offset += ((file_size + LP_SECTOR_SIZE - 1) // LP_SECTOR_SIZE) * LP_SECTOR_SIZE

        metadata["unsparse_image_size"] = total_size

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2)


def load_image_source(filepath: str) -> Union[SparseImage, RawImage]:
    """Inspect header magic and return appropriate SparseImage or RawImage instance."""
    with open(filepath, 'rb') as f:
        magic_bytes = f.read(4)
        if len(magic_bytes) < 4:
            raise FastDumperError(f"File too small to determine image format: {filepath}")
        magic = struct.unpack_from('<I', magic_bytes, 0)[0]

    if magic == SPARSE_HEADER_MAGIC:
        return SparseImage(filepath)
    return RawImage(filepath)


def load_metadata_file(metadata_path: str) -> dict:
    """Load and validate metadata from JSON file."""
    metadata_file = Path(metadata_path)
    if not metadata_file.exists():
        raise FastDumperError(f"Metadata file not found: {metadata_path}")

    try:
        with open(metadata_file, 'r', encoding='utf-8') as f:
            metadata = json.load(f)
    except json.JSONDecodeError as e:
        raise FastDumperError(f"Invalid JSON in metadata file: {e}")

    for field in ["partitions", "block_size"]:
        if field not in metadata:
            raise FastDumperError(f"Missing required field in metadata: {field}")

    if not isinstance(metadata["partitions"], list):
        raise FastDumperError("Metadata partitions field must be a list")

    return metadata


def create_partitions_from_metadata(metadata: dict, partition_names: Optional[List[str]] = None) -> List[PartitionInfo]:
    """Create PartitionInfo objects from loaded metadata dict."""
    partitions = []
    for partition_data in metadata["partitions"]:
        if not isinstance(partition_data, dict):
            continue

        name = partition_data.get("name", "")
        if not name or (partition_names and name not in partition_names):
            continue

        extents = []
        total_size = 0
        extent_list = partition_data.get("extents", [])
        if isinstance(extent_list, list):
            for extent_data in extent_list:
                if not isinstance(extent_data, dict):
                    continue
                offset = extent_data.get("offset", 0)
                length = extent_data.get("length", 0)
                if offset >= 0 and length > 0:
                    if extents and (extents[-1].offset + extents[-1].size == offset):
                        extents[-1].size += length
                    else:
                        extents.append(ExtentInfo(offset, length))
                    total_size += length

        partition_size = partition_data.get("size", total_size)
        partitions.append(PartitionInfo(name, extents, partition_size))

    return partitions


def _extract_single_partition(
    image: Union[SparseImage, RawImage],
    partition: PartitionInfo,
    output_dir: Path
) -> Tuple[str, float]:
    """Worker function for concurrent thread extraction."""
    start_time = time.perf_counter()
    output_file = output_dir / f"{partition.name}.img"
    buf = bytearray(BUFFER_SIZE)

    try:
        with open(image.filepath, 'rb') as infile, open(output_file, 'wb') as outfile:
            for extent in partition.extents:
                image.copy_range(infile, extent.offset, extent.size, outfile, buf)
            # Guarantee exact file size down to the byte (creates sparse hole if trailing empty)
            outfile.truncate(partition.total_size)
    except Exception as e:
        if output_file.exists():
            try:
                output_file.unlink()
            except OSError:
                pass
        raise FastDumperError(f"Failed to extract {partition.name}: {e}")

    elapsed = time.perf_counter() - start_time
    return partition.name, elapsed


class FastExtractor:
    """Thread-pooled partition extractor releasing the GIL during disk I/O."""
    def __init__(self, source: Union[SparseImage, RawImage, str], output_dir: str, max_workers: Optional[int] = None):
        if isinstance(source, (SparseImage, RawImage)):
            self.image = source
        else:
            self.image = load_image_source(str(source))

        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_workers = max_workers or min(os.cpu_count() or 4, 16)

    def extract_partition(self, partition: PartitionInfo) -> Tuple[str, float]:
        return _extract_single_partition(self.image, partition, self.output_dir)

    def extract_all(self, partitions: List[PartitionInfo], show_progress: bool = True) -> List[Tuple[str, float]]:
        if show_progress:
            print(f"Extracting {len(partitions)} partitions using {self.max_workers} threads...")

        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_partition = {
                executor.submit(_extract_single_partition, self.image, p, self.output_dir): p
                for p in partitions
            }

            for i, future in enumerate(concurrent.futures.as_completed(future_to_partition), 1):
                partition = future_to_partition[future]
                name, elapsed = future.result()
                results.append((name, elapsed))

                if show_progress:
                    size_mb = partition.total_size / (1024 * 1024)
                    speed_mb = size_mb / elapsed if elapsed > 0 else 0
                    print(f"[{i}/{len(partitions)}] {name}: {size_mb:.1f}MB in {elapsed:.2f}s ({speed_mb:.1f}MB/s)")

        if show_progress and len(partitions) > 1:
            total_time = sum(elapsed for _, elapsed in results)
            avg_time = total_time / len(partitions)
            print(f"\nCompleted! Average extraction time: {avg_time:.2f}s per partition")
            print(f"Total cumulative extraction time: {total_time:.2f}s")

        return results


class OptimizedLpUnpack:
    """
    Unified, in-process super.img unpacker.
    Zero temporary disk space required: streams directly from sparse or raw images.
    Safe for PyInstaller frozen executables and direct Python invocation.
    """
    def __init__(
        self,
        super_image: str,
        output_dir: str = "extracted_partitions",
        partitions: Optional[List[str]] = None,
        jobs: int = 4,
        quiet: bool = False,
        temp_dir: Optional[str] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        list_only: bool = False,
        unsparse_only: bool = False,
        dump_metadata: bool = False,
        use_metadata: Optional[str] = None,
    ):
        self.super_image = str(super_image)
        self.output_dir = str(output_dir)
        self.partitions = partitions
        self.jobs = jobs
        self.quiet = quiet
        self.temp_dir = temp_dir
        self.log_callback = log_callback
        self.list_only = list_only
        self.unsparse_only = unsparse_only
        self.dump_metadata = dump_metadata
        self.use_metadata = use_metadata

    def log(self, message: str) -> None:
        if self.log_callback:
            self.log_callback(str(message))
        elif not self.quiet:
            print(message)

    def run(self) -> int:
        input_path = Path(self.super_image)
        if not input_path.exists():
            self.log(f"Error: {self.super_image} not found")
            return 1
        if input_path.stat().st_size == 0:
            self.log(f"Error: {self.super_image} is empty")
            return 1

        try:
            image = load_image_source(self.super_image)
            is_sparse = isinstance(image, SparseImage)

            # 1. Handle --unsparse flag if requested
            if self.unsparse_only:
                output_path = Path(self.output_dir)
                output_path.mkdir(parents=True, exist_ok=True)
                unsparse_output = output_path / f"{input_path.stem}.unsparse.img"

                if is_sparse:
                    self.log("Sparse image detected, converting to unsparse image...")
                    converter = FastSparseConverter(self.super_image, str(output_path))
                    converted_file = converter.convert()
                    if converted_file != str(unsparse_output):
                        shutil.move(converted_file, unsparse_output)
                    self.log(f"Unsparsed image saved to: {unsparse_output}")
                    # Switch to raw reader for extracted unsparse image
                    image = RawImage(str(unsparse_output))
                else:
                    self.log(f"Image is already unsparsed, copying to: {unsparse_output}")
                    shutil.copy2(self.super_image, unsparse_output)
                    image = RawImage(str(unsparse_output))

                # If only unsparsing (no partitions, no listing), exit
                if not self.partitions and not self.list_only:
                    self.log("Unsparsing complete! Use --partitions to also extract partitions.")
                    return 0

            # 2. Obtain Partitions
            if self.use_metadata:
                self.log(f"Loading partitions from metadata file: {self.use_metadata}")
                metadata = load_metadata_file(self.use_metadata)
                partitions = create_partitions_from_metadata(metadata, self.partitions)
            else:
                self.log("Parsing partition metadata...")
                parser_obj = FastMetadataParser(image)

                if self.dump_metadata:
                    self.log(f"Dumping metadata to: {self.output_dir}/metadata.json")
                    parser_obj.dump_metadata_json(self.output_dir, self.partitions)
                    self.log("Metadata dump complete!")
                    if not self.list_only and not self.partitions and not self.unsparse_only:
                        return 0

                partitions = parser_obj.parse_partitions(self.partitions)

            if not partitions:
                if self.partitions:
                    self.log(f"No matching partitions found for: {', '.join(self.partitions)}")
                else:
                    self.log("No partitions found in image")
                return 1

            # 3. Handle --list flag
            if self.list_only:
                self.log(f"\nFound {len(partitions)} partitions:")
                total_size = 0
                for partition in partitions:
                    size_mb = partition.total_size / (1024 * 1024)
                    total_size += partition.total_size
                    self.log(f"  {partition.name}: {size_mb:.1f}MB ({len(partition.extents)} extents)")
                self.log(f"\nTotal size: {total_size / (1024 * 1024):.1f}MB")
                return 0

            # 4. Extract Partitions Concurrently
            self.log(f"Extracting {len(partitions)} partitions using {self.jobs} threads...")
            output_path = Path(self.output_dir)
            output_path.mkdir(parents=True, exist_ok=True)

            results = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.jobs) as executor:
                future_to_partition = {
                    executor.submit(_extract_single_partition, image, p, output_path): p
                    for p in partitions
                }

                for i, future in enumerate(concurrent.futures.as_completed(future_to_partition), 1):
                    partition = future_to_partition[future]
                    name, elapsed = future.result()
                    results.append((name, elapsed))
                    size_mb = partition.total_size / (1024 * 1024)
                    speed_mb = size_mb / elapsed if elapsed > 0 else 0
                    self.log(f"[{i}/{len(partitions)}] {name}: {size_mb:.1f}MB in {elapsed:.2f}s ({speed_mb:.1f}MB/s)")

            if len(partitions) > 1:
                total_time = sum(elapsed for _, elapsed in results)
                avg_time = total_time / len(partitions)
                self.log(f"\nCompleted! Average extraction time: {avg_time:.2f}s per partition")
                self.log(f"Total cumulative extraction time: {total_time:.2f}s")

            self.log(f"\nExtraction complete! Files saved to: {self.output_dir}")
            return 0

        except FastDumperError as e:
            self.log(f"Error: {e}")
            return 1
        except Exception as e:
            self.log(f"Unexpected error: {e}")
            return 1


def unpack_super(
    super_image: str,
    output_dir: str = "extracted_partitions",
    partitions: Optional[List[str]] = None,
    jobs: int = 4,
    quiet: bool = False,
    temp_dir: Optional[str] = None,
    log_callback: Optional[Callable[[str], None]] = None
) -> int:
    """Convenience function for in-process super.img extraction."""
    unpacker = OptimizedLpUnpack(
        super_image=super_image,
        output_dir=output_dir,
        partitions=partitions,
        jobs=jobs,
        quiet=quiet,
        temp_dir=temp_dir,
        log_callback=log_callback
    )
    return unpacker.run()


def main():
    ascii_art = r"""
                                    
 /\ /\ _ __  ___ _   _ _ __   ___ _ __ 
/ / \ \ '_ \/ __| | | | '_ \ / _ \ '__|
\ \_/ / | | \__ \ |_| | |_) |  __/ |   
 \___/|_| |_|___/\__,_| .__/ \___|_|   
                      |_| 
    """

    parser = argparse.ArgumentParser(
        description=ascii_art,
        usage="%(prog)s [super_image] [output_dir] [-h] [-p PARTITIONS [PARTITIONS ...]] [-j JOBS] [-q] [--list] [--temp-dir TEMP_DIR] [--version] [--unsparse] [--dump-metadata] [--create-metadata IMAGE [IMAGE ...]] [--use-metadata METADATA_FILE]",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        '--version',
        action='version',
        version=f'%(prog)s V{VERSION}'
    )

    parser.add_argument(
        'super_image',
        nargs='?',
        help='Path to super.img file'
    )

    parser.add_argument(
        'output_dir',
        nargs='?',
        default='extracted_partitions',
        help='Output directory for extracted partitions (default: extracted_partitions)'
    )

    parser.add_argument(
        '-p', '--partitions',
        nargs='+',
        help='Specific partition names to extract (vendor, product etc.) (default: extract all)'
    )

    parser.add_argument(
        '-j', '--jobs',
        type=int,
        default=4,
        help='Number of parallel extraction threads (default: 4)'
    )

    parser.add_argument(
        '-q', '--quiet',
        action='store_true',
        help='Suppress progress output'
    )

    parser.add_argument(
        '--list',
        action='store_true',
        help='List available partitions and exit'
    )

    parser.add_argument(
        '--temp-dir',
        help='Directory for temporary unsparse file if --unsparse is used (default: system temp directory)'
    )

    parser.add_argument(
        '--unsparse',
        action='store_true',
        help='Unsparse image and save to output directory'
    )

    parser.add_argument(
        '--dump-metadata',
        action='store_true',
        help='Dump partition metadata to metadata.json in output directory'
    )

    parser.add_argument(
        '--create-metadata',
        nargs='+',
        metavar='IMAGE',
        help='Create metadata.json from individual partition images'
    )

    parser.add_argument(
        '--use-metadata',
        metavar='METADATA_FILE',
        help='Use existing metadata.json file for partition information'
    )

    args = parser.parse_args()

    # Handle --create-metadata flag separately
    if args.create_metadata:
        try:
            if not args.quiet:
                print(f"Creating metadata from {len(args.create_metadata)} partition images...")

            output_dir = Path(args.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            metadata_path = output_dir / "metadata.json"

            FastMetadataParser.create_metadata_from_images(args.create_metadata, str(metadata_path))

            if not args.quiet:
                print(f"Metadata file created: {metadata_path}")
            sys.exit(0)
        except FastDumperError as e:
            print(f"Error: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"Unexpected error: {e}")
            sys.exit(1)

    if not args.super_image:
        print("Error: super_image is required")
        parser.print_help()
        sys.exit(1)

    unpacker = OptimizedLpUnpack(
        super_image=args.super_image,
        output_dir=args.output_dir,
        partitions=args.partitions,
        jobs=args.jobs,
        quiet=args.quiet,
        temp_dir=args.temp_dir,
        list_only=args.list,
        unsparse_only=args.unsparse,
        dump_metadata=args.dump_metadata,
        use_metadata=args.use_metadata,
    )

    try:
        exit_code = unpacker.run()
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        sys.exit(1)


if __name__ == "__main__":
    main()
