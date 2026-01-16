#!/usr/bin/env python3

# Unsuper, fastest Android super.img dumper
# Made by fl0w
# https://github.com/codefl0w

import argparse
import mmap
import os
import struct
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, List, Optional, Tuple
import time
import numpy as np
import multiprocessing
from multiprocessing import Pool, cpu_count
import json

VERSION = "2.2.0"

# Constants
SPARSE_HEADER_MAGIC = 0xED26FF3A
SPARSE_HEADER_SIZE = 28
SPARSE_CHUNK_HEADER_SIZE = 12

LP_PARTITION_RESERVED_BYTES = 4096
LP_METADATA_GEOMETRY_MAGIC = 0x616c4467
LP_METADATA_GEOMETRY_SIZE = 4096
LP_METADATA_HEADER_MAGIC = 0x414C5030
LP_SECTOR_SIZE = 512

LP_TARGET_TYPE_LINEAR = 0

# Optimized buffer size for I/O operations (1MB)
BUFFER_SIZE = 1024 * 1024

class FastDumperError(Exception):
    # Custom exception for dumper errors
    pass

@dataclass
class ExtentInfo:
    # extent information
    offset: int
    size: int

@dataclass
class PartitionInfo:
    # partition information
    name: str
    extents: List[ExtentInfo]
    total_size: int

class SparseHeader:
    # Sparse image header parser
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
        ) = struct.unpack(fmt, buffer[:required_size])

class SparseChunkHeader:
    # Sparse chunk header parser
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
        ) = struct.unpack(fmt, buffer[:required_size])

class FastSparseConverter:
    # Optimized sparse image converter
    
    def __init__(self, input_file: str, temp_dir: Optional[str] = None):
        self.input_file = input_file
        self.temp_dir = temp_dir
        
    def convert(self) -> str:
        # Create output file in temp directory
        if self.temp_dir:
            temp_dir = Path(self.temp_dir)
            temp_dir.mkdir(parents=True, exist_ok=True)
            output_file = temp_dir / f"{Path(self.input_file).stem}.unsparse.img"
        else:
            # Use OS temp directory
            fd, output_file = tempfile.mkstemp(
                suffix='.unsparse.img',
                prefix=f"{Path(self.input_file).stem}_",
                dir=None
            )
            os.close(fd)  # Close the file descriptor, we'll open it normally
            output_file = str(output_file)
        
        try:
            with open(self.input_file, 'rb') as infile:
                # Read and validate header
                header_data = infile.read(SPARSE_HEADER_SIZE)
                if len(header_data) < SPARSE_HEADER_SIZE:
                    raise FastDumperError(f"File too short: cannot read sparse header ({len(header_data)} < {SPARSE_HEADER_SIZE})")
                
                header = SparseHeader(header_data)
                
                if header.magic != SPARSE_HEADER_MAGIC:
                    raise FastDumperError(f"Invalid sparse image magic: 0x{header.magic:08x} (expected 0x{SPARSE_HEADER_MAGIC:08x})")
                
                # Validate header values
                if header.file_hdr_sz < SPARSE_HEADER_SIZE:
                    raise FastDumperError(f"Invalid file header size: {header.file_hdr_sz}")
                
                if header.chunk_hdr_sz < SPARSE_CHUNK_HEADER_SIZE:
                    raise FastDumperError(f"Invalid chunk header size: {header.chunk_hdr_sz}")
                
                if header.blk_sz == 0:
                    raise FastDumperError("Invalid block size: 0")
                
                # Pre-allocate output file size for better performance
                estimated_size = header.total_blks * header.blk_sz
                
                with open(output_file, 'wb') as outfile:
                    try:
                        # Pre-allocate file
                        if estimated_size > 0:
                            outfile.seek(estimated_size - 1)
                            outfile.write(b'\0')
                            outfile.seek(0)
                    except:
                        # Fall back if pre-allocation fails
                        pass
                    
                    # Skip to chunk data
                    infile.seek(header.file_hdr_sz)
                    
                    # Pre-allocate buffer for better performance
                    buffer = bytearray(BUFFER_SIZE)
                    
                    # Process chunks efficiently
                    for chunk_idx in range(header.total_chunks):
                        try:
                            # Read chunk header with bounds checking
                            chunk_header_data = infile.read(SPARSE_CHUNK_HEADER_SIZE)
                            if len(chunk_header_data) < SPARSE_CHUNK_HEADER_SIZE:
                                raise FastDumperError(f"Unexpected EOF while reading chunk {chunk_idx + 1}/{header.total_chunks} header: got {len(chunk_header_data)} bytes")
                            
                            chunk_header = SparseChunkHeader(chunk_header_data)
                            
                            # Validate chunk header
                            if chunk_header.total_sz < header.chunk_hdr_sz:
                                raise FastDumperError(f"Invalid chunk {chunk_idx + 1} total size: {chunk_header.total_sz} < {header.chunk_hdr_sz}")
                            
                            # Skip extra header data if present
                            if header.chunk_hdr_sz > SPARSE_CHUNK_HEADER_SIZE:
                                extra_bytes = header.chunk_hdr_sz - SPARSE_CHUNK_HEADER_SIZE
                                infile.seek(infile.tell() + extra_bytes)  
                            
                            chunk_data_size = chunk_header.total_sz - header.chunk_hdr_sz
                            output_size = chunk_header.chunk_sz * header.blk_sz
                            
                            if chunk_header.chunk_type == 0xCAC1:  # Raw data
                                # Copy data using readinto for better performance
                                remaining = chunk_data_size
                                while remaining > 0:
                                    chunk_size = min(len(buffer), remaining)
                                    bytes_read = infile.readinto(memoryview(buffer)[:chunk_size])
                                    if not bytes_read:
                                        raise FastDumperError(f"Unexpected EOF while reading chunk {chunk_idx + 1} raw data")
                                    outfile.write(memoryview(buffer)[:bytes_read])
                                    remaining -= bytes_read
                                    
                            elif chunk_header.chunk_type == 0xCAC2:  # Fill data
                                if chunk_data_size < 4:
                                    raise FastDumperError(f"Incomplete fill chunk {chunk_idx + 1}: data size {chunk_data_size} < 4")
                                
                                fill_data = infile.read(4)
                                if len(fill_data) < 4:
                                    raise FastDumperError(f"Unexpected EOF while reading chunk {chunk_idx + 1} fill data")
                                
                                # Skip remaining fill chunk data if any
                                if chunk_data_size > 4:
                                    remaining_fill = chunk_data_size - 4
                                    infile.seek(infile.tell() + remaining_fill)  
                                
                                # Vectorized fill pattern generation using numpy
                                if output_size > 0:
                                    fill_value = struct.unpack('<I', fill_data)[0]
                                    # Create large chunks at once using numpy
                                    chunk_size = min(64 * 1024 * 1024, output_size)  # 64MB chunks
                                    remaining = output_size
                                    
                                    while remaining > 0:
                                        write_size = min(chunk_size, remaining)
                                        # Use numpy for fast pattern generation
                                        num_elements = write_size // 4
                                        if num_elements > 0:
                                            pattern_array = np.full(num_elements, fill_value, dtype=np.uint32)
                                            outfile.write(pattern_array.tobytes())
                                            remaining -= num_elements * 4
                                        
                                        # Handle remaining bytes
                                        if remaining > 0 and remaining < 4:
                                            outfile.write(fill_data[:remaining])
                                            remaining = 0
                                    
                            elif chunk_header.chunk_type == 0xCAC3:  # Don't care
                                # Skip chunk data and write zeros/seek
                                if chunk_data_size > 0:
                                    infile.seek(infile.tell() + chunk_data_size)  
                                
                                # Seek forward in output file (creates sparse file if supported)
                                current_pos = outfile.tell()
                                outfile.seek(current_pos + output_size)
                                
                            elif chunk_header.chunk_type == 0xCAC4:  # CRC32
                                # Skip CRC chunk data
                                if chunk_data_size > 0:
                                    infile.seek(infile.tell() + chunk_data_size)  
                                # CRC chunks don't contribute to output
                                
                            else:
                                # Unknown chunk type, skip data and output
                                print(f"Warning: Unknown chunk type 0x{chunk_header.chunk_type:04x} in chunk {chunk_idx + 1}, skipping...")
                                if chunk_data_size > 0:
                                    infile.seek(infile.tell() + chunk_data_size)  
                                
                                # Seek forward in output
                                current_pos = outfile.tell()
                                outfile.seek(current_pos + output_size)
                        
                        except FastDumperError:
                            raise
                        except Exception as e:
                            raise FastDumperError(f"Error processing chunk {chunk_idx + 1}: {e}")
        
        except Exception as e:
            # Clean up partial output file on error
            if os.path.exists(output_file):
                try:
                    os.remove(output_file)
                except:
                    pass
            raise
        
        return output_file

class FastMetadataParser:
    # Optimized metadata parser with batch processing
    
    def __init__(self, file_path: str):
        self.file_path = file_path
        self.file_size = os.path.getsize(file_path)
        
    def parse_partitions(self, partition_names: Optional[List[str]] = None) -> List[PartitionInfo]:
        # Parse partition metadata efficiently
        
        with open(self.file_path, 'rb') as f:
            # Use memory mapping for better performance on large files
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                return self._parse_with_mmap(mm, partition_names)
    
    def _parse_with_mmap(self, mm: mmap.mmap, partition_names: Optional[List[str]]) -> List[PartitionInfo]:
        
        # Parse metadata using memory mapping with batch processing
        
        # Check minimum file size
        if len(mm) < LP_PARTITION_RESERVED_BYTES + 40:
            raise FastDumperError("File too small to contain valid metadata")
        
        # Read geometry
        mm.seek(LP_PARTITION_RESERVED_BYTES)
        geometry_data = mm.read(44)  # Read enough for all geometry fields
        if len(geometry_data) < 44:
            raise FastDumperError("Cannot read geometry data")
        
        magic, struct_size = struct.unpack('<2I', geometry_data[:8])
        
        if magic != LP_METADATA_GEOMETRY_MAGIC:
            raise FastDumperError(f"Invalid geometry magic: 0x{magic:08x}")
        
        metadata_max_size, metadata_slot_count, logical_block_size = struct.unpack(
            '<3I', geometry_data[32:44]
        )
        
        # Validate geometry values
        if metadata_max_size == 0 or metadata_slot_count == 0:
            raise FastDumperError("Invalid metadata geometry")
        
        # Calculate header offset
        base_offset = LP_PARTITION_RESERVED_BYTES + (LP_METADATA_GEOMETRY_SIZE * 2)
        header_offset = base_offset
        
        # Check if we have enough space for metadata
        if header_offset + 128 > len(mm):
            raise FastDumperError("File too small for metadata header")
        
        # Read metadata header
        mm.seek(header_offset)
        header_data = mm.read(128)  # Read enough for header + table descriptors
        if len(header_data) < 128:
            raise FastDumperError("Cannot read metadata header")
        
        magic = struct.unpack('<I', header_data[:4])[0]
        if magic != LP_METADATA_HEADER_MAGIC:
            # Try backup header
            backup_offset = base_offset + metadata_max_size * metadata_slot_count
            if backup_offset + 128 > len(mm):
                raise FastDumperError("File too small for backup metadata header")
            
            mm.seek(backup_offset)
            header_data = mm.read(128)
            if len(header_data) < 128:
                raise FastDumperError("Cannot read backup metadata header")
            
            magic = struct.unpack('<I', header_data[:4])[0]
            if magic != LP_METADATA_HEADER_MAGIC:
                raise FastDumperError(f"Invalid metadata header magic: 0x{magic:08x}")
            header_offset = backup_offset
        
        # Parse header and table descriptors
        header_size, tables_size = struct.unpack('<2I', header_data[8:16])
        
        if header_size < 80 or tables_size == 0:
            raise FastDumperError("Invalid metadata header values")
        
        # Parse table descriptors (starting at offset 80 in header)
        tables_start = header_offset + header_size
        
        # Check bounds for table descriptors
        if len(header_data) < 104:
            raise FastDumperError("Header too small for table descriptors")
        
        # Partition table descriptor
        part_offset, part_count, part_entry_size = struct.unpack('<3I', header_data[80:92])
        
        # Extent table descriptor  
        extent_offset, extent_count, extent_entry_size = struct.unpack('<3I', header_data[92:104])
        
        # Validate table parameters
        if part_entry_size < 52 or extent_entry_size < 24:
            raise FastDumperError("Invalid table entry sizes")
        
        # Check bounds for partition table
        part_table_start = tables_start + part_offset
        part_table_size = part_count * part_entry_size
        if part_table_start + part_table_size > len(mm):
            raise FastDumperError("Partition table extends beyond file")
        
        # Check bounds for extent table
        extent_table_start = tables_start + extent_offset
        extent_table_size = extent_count * extent_entry_size
        if extent_table_start + extent_table_size > len(mm):
            raise FastDumperError("Extent table extends beyond file")
        
        # Read partition table
        mm.seek(part_table_start)
        partitions_data = mm.read(part_table_size)
        if len(partitions_data) < part_table_size:
            raise FastDumperError("Cannot read complete partition table")
        
        # Read extent table
        mm.seek(extent_table_start)
        extents_data = mm.read(extent_table_size)
        if len(extents_data) < extent_table_size:
            raise FastDumperError("Cannot read complete extent table")
        
        # Batch unpack all partition entries at once
        partitions = []
        partition_fmt = '<36s3I'  # name(36) + attributes(4) + first_extent(4) + num_extents(4)
        partition_struct_size = struct.calcsize(partition_fmt)
        
        # Calculate how many complete partition entries we can unpack
        max_partitions = min(part_count, len(partitions_data) // partition_struct_size)
        
        # Unpack all partitions in batches
        partition_structs = []
        for i in range(max_partitions):
            start_idx = i * part_entry_size
            # Only unpack the fields we need
            entry_data = partitions_data[start_idx:start_idx + partition_struct_size]
            if len(entry_data) >= partition_struct_size:
                partition_structs.append(struct.unpack(partition_fmt, entry_data))
        
        # Similarly for extents - batch unpack
        extent_fmt = '<QIQ'  # num_sectors(8) + target_type(4) + target_data(8)
        extent_struct_size = struct.calcsize(extent_fmt)
        max_extents = min(extent_count, len(extents_data) // extent_struct_size)
        
        extent_structs = []
        for i in range(max_extents):
            start_idx = i * extent_entry_size
            entry_data = extents_data[start_idx:start_idx + extent_struct_size]
            if len(entry_data) >= extent_struct_size:
                extent_structs.append(struct.unpack(extent_fmt, entry_data))
        
        # Process partitions with batch-loaded data
        for name_bytes, attributes, first_extent_idx, num_extents in partition_structs:
            name = name_bytes.decode('utf-8').strip('\x00')

                # Check for various empty partition conditions
           
            
            if not name or (partition_names and name not in partition_names):
                continue
                
            if num_extents == 0 or first_extent_idx >= len(extent_structs):
                print(f"Warning: Partition {name} has no extents, skipping...")
                continue
            
            # Process extents for this partition
            extents = []
            total_size = 0
            
            for j in range(num_extents):
                extent_idx = first_extent_idx + j
                if extent_idx >= len(extent_structs):
                    break
                    
                num_sectors, target_type, target_data = extent_structs[extent_idx]
                
                if target_type != LP_TARGET_TYPE_LINEAR:
                    continue
                
                offset = target_data * LP_SECTOR_SIZE
                size = num_sectors * LP_SECTOR_SIZE
                
                # Validate extent bounds
                if offset + size <= len(mm) and size > 0:
                    extents.append(ExtentInfo(offset, size))
                    total_size += size
            
            if extents:
                partitions.append(PartitionInfo(name, extents, total_size))
        
        return partitions

    def dump_metadata_json(self, output_dir: str, partition_names: Optional[List[str]] = None) -> None:
        # Dump metadata to JSON file in output directory
        partitions = self.parse_partitions_for_metadata(partition_names)
        
        # Extract block size from geometry
        with open(self.file_path, 'rb') as f:
            f.seek(LP_PARTITION_RESERVED_BYTES + 32)  # Skip to logical_block_size field
            geometry_data = f.read(12)
            if len(geometry_data) >= 12:
                logical_block_size = struct.unpack('<I', geometry_data[8:12])[0]
            else:
                logical_block_size = LP_SECTOR_SIZE  # Default fallback
        
        # Build JSON structure
        metadata = {
            "uses_unsuper": VERSION,
            "unsparse_image_size": self.file_size,
           
            "block_size": logical_block_size,
            "partitions": []
        }
        
        for partition in partitions:
            partition_data = {
                "name": partition.name,
                "size": partition.total_size,
                "extents": []
            }
            
            for extent in partition.extents:
                extent_data = {
                    "offset": extent.offset,
                    "length": extent.size
                }
                partition_data["extents"].append(extent_data)
            
            metadata["partitions"].append(partition_data)
        
        # Create output directory and write JSON file
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        json_file = output_path / "metadata.json"
        
        with open(json_file, 'w') as f:
            json.dump(metadata, f, indent=2)
    
    def parse_partitions_for_metadata(self, partition_names: Optional[List[str]] = None) -> List[PartitionInfo]:
        # Parse partition metadata for JSON dump - includes partitions with zero extents
        
        with open(self.file_path, 'rb') as f:
            # Use memory mapping for better performance on large files
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                return self._parse_with_mmap_for_metadata(mm, partition_names)
    
    def _parse_with_mmap_for_metadata(self, mm: mmap.mmap, partition_names: Optional[List[str]]) -> List[PartitionInfo]:
        # Parse metadata using memory mapping
        
        # Check minimum file size
        if len(mm) < LP_PARTITION_RESERVED_BYTES + 40:
            raise FastDumperError("File too small to contain valid metadata")
        
        # Read geometry
        mm.seek(LP_PARTITION_RESERVED_BYTES)
        geometry_data = mm.read(44)  # Read enough for all geometry fields
        if len(geometry_data) < 44:
            raise FastDumperError("Cannot read geometry data")
        
        magic, struct_size = struct.unpack('<2I', geometry_data[:8])
        
        if magic != LP_METADATA_GEOMETRY_MAGIC:
            raise FastDumperError(f"Invalid geometry magic: 0x{magic:08x}")
        
        metadata_max_size, metadata_slot_count, logical_block_size = struct.unpack(
            '<3I', geometry_data[32:44]
        )
        
        # Validate geometry values
        if metadata_max_size == 0 or metadata_slot_count == 0:
            raise FastDumperError("Invalid metadata geometry")
        
        # Calculate header offset
        base_offset = LP_PARTITION_RESERVED_BYTES + (LP_METADATA_GEOMETRY_SIZE * 2)
        header_offset = base_offset
        
        # Check if we have enough space for metadata
        if header_offset + 128 > len(mm):
            raise FastDumperError("File too small for metadata header")
        
        # Read metadata header
        mm.seek(header_offset)
        header_data = mm.read(128)  # Read enough for header + table descriptors
        if len(header_data) < 128:
            raise FastDumperError("Cannot read metadata header")
        
        magic = struct.unpack('<I', header_data[:4])[0]
        if magic != LP_METADATA_HEADER_MAGIC:
            # Try backup header
            backup_offset = base_offset + metadata_max_size * metadata_slot_count
            if backup_offset + 128 > len(mm):
                raise FastDumperError("File too small for backup metadata header")
            
            mm.seek(backup_offset)
            header_data = mm.read(128)
            if len(header_data) < 128:
                raise FastDumperError("Cannot read backup metadata header")
            
            magic = struct.unpack('<I', header_data[:4])[0]
            if magic != LP_METADATA_HEADER_MAGIC:
                raise FastDumperError(f"Invalid metadata header magic: 0x{magic:08x}")
            header_offset = backup_offset
        
        # Parse header and table descriptors
        header_size, tables_size = struct.unpack('<2I', header_data[8:16])
        
        if header_size < 80 or tables_size == 0:
            raise FastDumperError("Invalid metadata header values")
        
        # Parse table descriptors (starting at offset 80 in header)
        tables_start = header_offset + header_size
        
        # Check bounds for table descriptors
        if len(header_data) < 104:
            raise FastDumperError("Header too small for table descriptors")
        
        # Partition table descriptor
        part_offset, part_count, part_entry_size = struct.unpack('<3I', header_data[80:92])
        
        # Extent table descriptor  
        extent_offset, extent_count, extent_entry_size = struct.unpack('<3I', header_data[92:104])
        
        # Validate table parameters
        if part_entry_size < 52 or extent_entry_size < 24:
            raise FastDumperError("Invalid table entry sizes")
        
        # Check bounds for partition table
        part_table_start = tables_start + part_offset
        part_table_size = part_count * part_entry_size
        if part_table_start + part_table_size > len(mm):
            raise FastDumperError("Partition table extends beyond file")
        
        # Check bounds for extent table
        extent_table_start = tables_start + extent_offset
        extent_table_size = extent_count * extent_entry_size
        if extent_table_start + extent_table_size > len(mm):
            raise FastDumperError("Extent table extends beyond file")
        
        # Read partition table
        mm.seek(part_table_start)
        partitions_data = mm.read(part_table_size)
        if len(partitions_data) < part_table_size:
            raise FastDumperError("Cannot read complete partition table")
        
        # Read extent table
        mm.seek(extent_table_start)
        extents_data = mm.read(extent_table_size)
        if len(extents_data) < extent_table_size:
            raise FastDumperError("Cannot read complete extent table")
        
        # Batch unpack all partition entries at once
        partitions = []
        partition_fmt = '<36s3I'  # name(36) + attributes(4) + first_extent(4) + num_extents(4)
        partition_struct_size = struct.calcsize(partition_fmt)
        
        # Calculate how many complete partition entries we can unpack
        max_partitions = min(part_count, len(partitions_data) // partition_struct_size)
        
        # Unpack all partitions in batches
        partition_structs = []
        for i in range(max_partitions):
            start_idx = i * part_entry_size
            # Only unpack the fields we need
            entry_data = partitions_data[start_idx:start_idx + partition_struct_size]
            if len(entry_data) >= partition_struct_size:
                partition_structs.append(struct.unpack(partition_fmt, entry_data))
        
        # Similarly for extents - batch unpack
        extent_fmt = '<QIQ'  # num_sectors(8) + target_type(4) + target_data(8)
        extent_struct_size = struct.calcsize(extent_fmt)
        max_extents = min(extent_count, len(extents_data) // extent_struct_size)
        
        extent_structs = []
        for i in range(max_extents):
            start_idx = i * extent_entry_size
            entry_data = extents_data[start_idx:start_idx + extent_struct_size]
            if len(entry_data) >= extent_struct_size:
                extent_structs.append(struct.unpack(extent_fmt, entry_data))
        
        # Process partitions with batch-loaded data - INCLUDE zero-extent partitions for metadata dump
        for name_bytes, attributes, first_extent_idx, num_extents in partition_structs:
            name = name_bytes.decode('utf-8').strip('\x00')
            
            if not name or (partition_names and name not in partition_names):
                continue
            
            # Process extents for this partition (even if num_extents is 0)
            extents = []
            total_size = 0
            
            # Handle zero extents case - create partition with empty extents list
            if num_extents == 0:
                partitions.append(PartitionInfo(name, extents, total_size))
                continue
            
            # Handle case where first_extent_idx is out of bounds
            if first_extent_idx >= len(extent_structs):
                partitions.append(PartitionInfo(name, extents, total_size))
                continue
            
            for j in range(num_extents):
                extent_idx = first_extent_idx + j
                if extent_idx >= len(extent_structs):
                    break
                    
                num_sectors, target_type, target_data = extent_structs[extent_idx]
                
                if target_type != LP_TARGET_TYPE_LINEAR:
                    continue
                
                offset = target_data * LP_SECTOR_SIZE
                size = num_sectors * LP_SECTOR_SIZE
                
                # Validate extent bounds
                if offset + size <= len(mm) and size > 0:
                    extents.append(ExtentInfo(offset, size))
                    total_size += size
            
            # Always add partition to the list (even with no valid extents)
            partitions.append(PartitionInfo(name, extents, total_size))
        
        return partitions  

    def create_metadata_from_images(image_paths: List[str], output_path: str, 
                                   base_metadata: Optional[dict] = None) -> None:
        """Create metadata.json from individual partition images"""
        
        # Use base metadata if provided, otherwise create defaults
        if base_metadata:
            metadata = base_metadata.copy()
            # Update the version to current
            metadata["uses_unsuper"] = VERSION
        else:
            metadata = {
                "uses_unsuper": VERSION,
                "unsparse_image_size": 0,  # Will be calculated
                "block_size": LP_SECTOR_SIZE,  # Default block size
                "partitions": []
            }
        
        # Clear existing partitions if any
        metadata["partitions"] = []
        total_size = 0
        current_offset = LP_PARTITION_RESERVED_BYTES + (LP_METADATA_GEOMETRY_SIZE * 2)
        
        for image_path in image_paths:
            image_file = Path(image_path)
            if not image_file.exists():
                print(f"Warning: Image file {image_path} not found, skipping...")
                continue
            
            # Get partition name from filename (remove .img extension)
            partition_name = image_file.stem
            if partition_name.endswith('.img'):
                partition_name = partition_name[:-4]
            
            # Get file size
            file_size = image_file.stat().st_size
            total_size += file_size
            
            # Create partition entry
            partition_data = {
                "name": partition_name,
                "size": file_size,
                "extents": [{
                    "offset": current_offset,
                    "length": file_size
                }]
            }
            
            metadata["partitions"].append(partition_data)
            # Update offset for next partition (aligned to sector boundary)
            current_offset += ((file_size + LP_SECTOR_SIZE - 1) // LP_SECTOR_SIZE) * LP_SECTOR_SIZE
        
        # Update total size
        metadata["unsparse_image_size"] = total_size
        
        # Write metadata file
        with open(output_path, 'w') as f:
            json.dump(metadata, f, indent=2)
        
        print(f"Metadata created with {len(metadata['partitions'])} partitions")

def load_metadata_file(metadata_path: str) -> dict:
    """Load and validate metadata from JSON file"""
    metadata_file = Path(metadata_path)
    if not metadata_file.exists():
        raise FastDumperError(f"Metadata file not found: {metadata_path}")
    
    try:
        with open(metadata_file, 'r') as f:
            metadata = json.load(f)
    except json.JSONDecodeError as e:
        raise FastDumperError(f"Invalid JSON in metadata file: {e}")
    
    # Validate required fields
    required_fields = ["partitions", "block_size"]
    for field in required_fields:
        if field not in metadata:
            raise FastDumperError(f"Missing required field in metadata: {field}")
    
    if not isinstance(metadata["partitions"], list):
        raise FastDumperError("Metadata partitions field must be a list")
    
    return metadata

def create_partitions_from_metadata(metadata: dict, partition_names: Optional[List[str]] = None) -> List[PartitionInfo]:
    """Create PartitionInfo objects from metadata"""
    partitions = []
    
    for partition_data in metadata["partitions"]:
        if not isinstance(partition_data, dict):
            continue
            
        name = partition_data.get("name", "")
        if not name:
            continue
            
        # Filter by partition names if specified
        if partition_names and name not in partition_names:
            continue
        
        # Parse extents
        extents = []
        total_size = 0
        
        extent_list = partition_data.get("extents", [])
        if not isinstance(extent_list, list):
            extent_list = []
        
        for extent_data in extent_list:
            if not isinstance(extent_data, dict):
                continue
                
            offset = extent_data.get("offset", 0)
            length = extent_data.get("length", 0)
            
            if offset >= 0 and length > 0:
                extents.append(ExtentInfo(offset, length))
                total_size += length
        
        # Use size from metadata if available, otherwise calculate from extents
        partition_size = partition_data.get("size", total_size)
        
        partitions.append(PartitionInfo(name, extents, partition_size))
    
    return partitions            

class FastExtractor:
    
    def __init__(self, input_file: str, output_dir: str, max_workers: int = None):
        self.input_file = input_file
        self.output_dir = Path(output_dir)
        # Use more workers for I/O bound operations, but cap at reasonable limit
        self.max_workers = max_workers or min(cpu_count() * 2, 16)
        
        # Create output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def extract_partition(self, partition: PartitionInfo) -> Tuple[str, float]:
        # Extract a single partition with optimized I/O
        start_time = time.time()
        output_file = self.output_dir / f"{partition.name}.img"
        
        try:
            with open(self.input_file, 'rb') as infile, open(output_file, 'wb') as outfile:
                # Use memory mapping for input file if it's large
                if os.path.getsize(self.input_file) > 100 * 1024 * 1024:  # 100MB threshold
                    with mmap.mmap(infile.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                        self._extract_with_mmap_optimized(mm, outfile, partition)
                else:
                    self._extract_with_readinto(infile, outfile, partition)
        except Exception as e:
            # Clean up partial output file on error
            if output_file.exists():
                output_file.unlink()
            raise FastDumperError(f"Failed to extract {partition.name}: {e}")
        
        elapsed = time.time() - start_time
        return partition.name, elapsed
    
    def _extract_with_mmap_optimized(self, mm: mmap.mmap, outfile: BinaryIO, partition: PartitionInfo):
        # Optimized memory-mapped extraction with direct memory copying
        buffer = bytearray(BUFFER_SIZE)
        
        for extent in partition.extents:
            if extent.offset >= len(mm):
                continue
            
            remaining = min(extent.size, len(mm) - extent.offset)
            offset = extent.offset
            
            while remaining > 0:
                chunk_size = min(len(buffer), remaining)
                # Direct memory copy from mmap
                buffer[:chunk_size] = mm[offset:offset + chunk_size]
                outfile.write(memoryview(buffer)[:chunk_size])
                offset += chunk_size
                remaining -= chunk_size
    
    def _extract_with_readinto(self, infile: BinaryIO, outfile: BinaryIO, partition: PartitionInfo):
        # Extract using readinto for better performance
        file_size = os.path.getsize(self.input_file)
        buffer = bytearray(BUFFER_SIZE)
        
        for extent in partition.extents:
            # Validate extent bounds
            if extent.offset >= file_size:
                continue
            
            infile.seek(extent.offset)
            remaining = min(extent.size, file_size - extent.offset)
            
            while remaining > 0:
                chunk_size = min(len(buffer), remaining)
                bytes_read = infile.readinto(memoryview(buffer)[:chunk_size])
                if not bytes_read:
                    break
                outfile.write(memoryview(buffer)[:bytes_read])
                remaining -= bytes_read
    
    def extract_all(self, partitions: List[PartitionInfo], show_progress: bool = True) -> None:
        # Extract all partitions
        extract_all_partitions(self.input_file, self.output_dir, partitions, self.max_workers, show_progress)


def _extract_partition_worker(input_file: str, output_dir: Path, partition: PartitionInfo) -> Tuple[str, float]:
    # Worker function for multiprocessing 
    start_time = time.time()
    output_file = output_dir / f"{partition.name}.img"
    
    try:
        with open(input_file, 'rb') as infile, open(output_file, 'wb') as outfile:
            try:
                outfile.seek(partition.total_size - 1)
                outfile.write(b'\0')
                outfile.seek(0)
            except (OSError, IOError):
                pass
            # Use larger buffer and readinto for better performance
            buffer = bytearray(BUFFER_SIZE)
            file_size = os.path.getsize(input_file)
            
            for extent in partition.extents:
                if extent.offset >= file_size:
                    continue
                    
                infile.seek(extent.offset)
                remaining = min(extent.size, file_size - extent.offset)
                
                while remaining > 0:
                    chunk_size = min(len(buffer), remaining)
                    bytes_read = infile.readinto(memoryview(buffer)[:chunk_size])
                    if not bytes_read:
                        break
                    outfile.write(memoryview(buffer)[:bytes_read])
                    remaining -= bytes_read
                        
    except Exception as e:
        if output_file.exists():
            output_file.unlink()
        raise FastDumperError(f"Failed to extract {partition.name}: {e}")
    
    elapsed = time.time() - start_time
    return partition.name, elapsed


def extract_all_partitions(input_file: str, output_dir: Path, partitions: List[PartitionInfo], 
                          max_workers: int, show_progress: bool = True) -> None:
    
    if show_progress:
        print(f"Extracting {len(partitions)} partitions using {max_workers} threads...")
    
    # Use multiprocessing for CPU-intensive operations
    with Pool(processes=max_workers) as pool:
        # Create args for each partition
        args_list = [(input_file, output_dir, partition) for partition in partitions]
        
        # Use starmap for parallel processing
        results = pool.starmap(_extract_partition_worker, args_list)
        
        if show_progress:
            for i, (name, elapsed) in enumerate(results, 1):
                partition = next(p for p in partitions if p.name == name)
                size_mb = partition.total_size / (1024 * 1024)
                speed_mb = size_mb / elapsed if elapsed > 0 else 0
                print(f"[{i}/{len(partitions)}] {name}: "
                      f"{size_mb:.1f}MB in {elapsed:.2f}s ({speed_mb:.1f}MB/s)")
    
    if show_progress and len(partitions) > 1:
        total_time = sum(elapsed for _, elapsed in results)
        avg_time = total_time / len(partitions)
        print(f"\nCompleted! Average extraction time: {avg_time:.2f}s per partition")
        print(f"Total extraction time: {total_time:.2f}s")


def main():

    ascii = r"""
                                    
 /\ /\ _ __  ___ _   _ _ __   ___ _ __ 
/ / \ \ '_ \/ __| | | | '_ \ / _ \ '__|
\ \_/ / | | \__ \ |_| | |_) |  __/ |   
 \___/|_| |_|___/\__,_| .__/ \___|_|   
                      |_| 
                  
     """

    parser = argparse.ArgumentParser(
        description=ascii,
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
        nargs='?',  # Make this optional
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
        help='Directory for temporary unsparse file (default: system temp directory)'
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
    
    # Handle --create-metadata flag separately (doesn't need super_image)
    if args.create_metadata:
        try:
            if not args.quiet:
                print(f"Creating metadata from {len(args.create_metadata)} partition images...")
            
            # Create output directory
            output_dir = Path(args.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            
            # Use FastMetadataParser to create metadata
            metadata_path = output_dir / "metadata.json"
            FastMetadataParser.create_metadata_from_images(args.create_metadata, str(metadata_path))
            
            if not args.quiet:
                print(f"Metadata file created: {metadata_path}")
            return
            
        except FastDumperError as e:
            print(f"Error: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"Unexpected error: {e}")
            sys.exit(1)
    
    # For all other operations, super_image is required
    if not args.super_image:
        print("Error: super_image is required")
        parser.print_help()
        sys.exit(1)
    
    # Validate input file
    input_path = Path(args.super_image)
    if not input_path.exists():
        print(f"Error: {args.super_image} not found")
        sys.exit(1)
    
    if input_path.stat().st_size == 0:
        print(f"Error: {args.super_image} is empty")
        sys.exit(1)
    
    unsparse_file = None  # Track unsparse file for cleanup
    
    try:
        input_file = args.super_image
        unsparse_created_by_flag = False  # Track if unsparse file was created by --unsparse flag
        
        # Check if input is sparse and convert if needed
        with open(input_file, 'rb') as f:
            magic_data = f.read(4)
            if len(magic_data) < 4:
                raise FastDumperError("File too small to determine format")
            magic = struct.unpack('<I', magic_data)[0]
            
        if magic == SPARSE_HEADER_MAGIC:
            if not args.quiet:
                print("Sparse image detected, converting...")
            
            # If --unsparse flag is used, save to output directory and use for extraction
            if args.unsparse:
                output_path = Path(args.output_dir)
                output_path.mkdir(parents=True, exist_ok=True)
                unsparse_output = output_path / f"{Path(args.super_image).stem}.unsparse.img"
                
                converter = FastSparseConverter(input_file, str(output_path))
                converted_file = converter.convert()
                
                # Move/rename to desired location if needed
                if converted_file != str(unsparse_output):
                    shutil.move(converted_file, unsparse_output)
                
                # Use the unsparsed file for further processing
                input_file = str(unsparse_output)
                unsparse_file = str(unsparse_output)
                unsparse_created_by_flag = True
                
                if not args.quiet:
                    print(f"Unsparsed image saved to: {unsparse_output}")
            else:
                # Normal temp conversion for extraction
                converter = FastSparseConverter(input_file, args.temp_dir)
                unsparse_file = converter.convert()
                input_file = unsparse_file
                
                if not args.quiet:
                    print(f"Converted to temporary file: {input_file}")
        elif args.unsparse:
            # Image is already unsparsed, just copy it to output directory
            output_path = Path(args.output_dir)
            output_path.mkdir(parents=True, exist_ok=True)
            unsparse_output = output_path / f"{Path(args.super_image).stem}.unsparse.img"
            
            shutil.copy2(args.super_image, unsparse_output)
            
            # Use the copied file for further processing
            input_file = str(unsparse_output)
            unsparse_file = str(unsparse_output)
            unsparse_created_by_flag = True
            
            if not args.quiet:
                print(f"Image copied to: {unsparse_output} (was already unsparsed)")
        
        # If --unsparse is used without any extraction arguments, exit after unsparsing
        if args.unsparse and not args.partitions and not args.list:
            if not args.quiet:
                print("Unsparsing complete! Use --partitions to also extract partitions.")
            return
        
        # Parse metadata
        if not args.quiet:
            print("Parsing metadata...")
        
        parser_obj = FastMetadataParser(input_file)
        # Dump metadata if requested
        if args.dump_metadata:
            if not args.quiet:
                print(f"Dumping metadata to: {args.output_dir}/metadata.json")
            
            parser_obj.dump_metadata_json(args.output_dir, args.partitions)
            
            if not args.quiet:
                print("Metadata dump complete!")
            
            # If only dumping metadata (no extraction or listing), exit here
            if not args.list and not args.partitions and not args.unsparse:
                return        
        partitions = parser_obj.parse_partitions(args.partitions)
        
        if not partitions:
            if args.partitions:
                print(f"No matching partitions found for: {', '.join(args.partitions)}")
            else:
                print("No partitions found in image")
            sys.exit(1)
        
        # List partitions if requested
        if args.list:
            print(f"\nFound {len(partitions)} partitions:")
            total_size = 0
            for partition in partitions:
                size_mb = partition.total_size / (1024 * 1024)
                total_size += partition.total_size
                print(f"  {partition.name}: {size_mb:.1f}MB ({len(partition.extents)} extents)")
            
            total_mb = total_size / (1024 * 1024)
            print(f"\nTotal size: {total_mb:.1f}MB")
            return
        
        # Extract partitions
        extractor = FastExtractor(input_file, args.output_dir, args.jobs)
        extractor.extract_all(partitions, not args.quiet)
        
        if not args.quiet:
            print(f"\nExtraction complete! Files saved to: {args.output_dir}")
            if args.unsparse:
                print(f"Unsparsed image saved to: {input_file}")
        
    except FastDumperError as e:
        print(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)
    finally:
        # Clean up temporary unsparse file only if it wasn't created by --unsparse flag
        if unsparse_file and os.path.exists(unsparse_file) and not unsparse_created_by_flag:
            try:
                os.remove(unsparse_file)
                if not args.quiet:
                    print(f"Temporary sparse image cleaned successfully.")
            except Exception as e:
                print(f"Warning: Could not remove temporary file {unsparse_file}: {e}")

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()               