using System.Buffers.Binary;
using System.IO.Hashing;
using System.Text;

namespace AnimeStudio.Endfield;

public static class VfsParser
{
    public static BlockMainInfo Parse(byte[] data, bool verifyCrc = true)
    {
        if (data is null) throw new ArgumentNullException(nameof(data));
        if (data.Length < 4) throw new InvalidDataException("data too short");

        ReadOnlySpan<byte> span = data;

        if (verifyCrc)
        {
            int dataLength = data.Length - 4;
            int expectedCrc = BinaryPrimitives.ReadInt32LittleEndian(span.Slice(dataLength, 4));
            int actualCrc = unchecked((int)Crc32.HashToUInt32(span.Slice(0, dataLength)));

            if (expectedCrc != actualCrc)
            {
                throw new InvalidDataException(
                    $"CRC mismatch: expected 0x{expectedCrc:X8}, got 0x{actualCrc:X8}");
            }
        }

        // The CRC is not part of the serialized records, even when verification
        // is disabled by a caller.
        span = span.Slice(0, data.Length - 4);
        int cursor = 0;

        int rawVersion = ReadI32Le(span, ref cursor);
        int codeVersion;
        int version;
        if (rawVersion < 11)
        {
            codeVersion = rawVersion;
            version = ReadI32Le(span, ref cursor);
        }
        else
        {
            codeVersion = 3;
            version = rawVersion;
        }

        if (codeVersion is < 3 or > 5)
            throw new InvalidDataException($"Unsupported VFS index code version {codeVersion}; supported versions are 3, 4 and 5");

        int groupCfgNameLen = ReadU16Le(span, ref cursor);
        string groupCfgName = ReadString(span, ref cursor, groupCfgNameLen);
        long groupCfgHashName = ReadI64Le(span, ref cursor);
        int groupFileInfoNum = ReadI32Le(span, ref cursor);
        long groupChunksLength = ReadI64Le(span, ref cursor);
        BlockType blockType = BlockTypes.FromByte(ReadU8(span, ref cursor));
        int chunkCount = ReadI32Le(span, ref cursor);

        ValidateCount(span, cursor, chunkCount, codeVersion >= 5 ? 53 : codeVersion == 4 ? 49 : 45, "chunk");
        var chunks = new List<ChunkInfo>(chunkCount);
        for (int ci = 0; ci < chunkCount; ci++)
        {
            UInt128 md5Name = ReadU128Le(span, ref cursor);
            UInt128 contentMd5 = ReadU128Le(span, ref cursor);
            long chunkLength = ReadI64Le(span, ref cursor);
            BlockType chunkBlockType = BlockTypes.FromByte(ReadU8(span, ref cursor));

            FileTag mainTag;
            if (codeVersion > 3)
            {
                long tagRaw = codeVersion >= 5 ? ReadI64Le(span, ref cursor) : ReadI32Le(span, ref cursor);
                mainTag = BlockTypes.FileTagFromByte(unchecked((byte)tagRaw));
            }
            else
            {
                mainTag = FileTag.None;
            }

            int fileCount = ReadI32Le(span, ref cursor);
            ValidateCount(span, cursor, fileCount, codeVersion >= 5 ? 68 : codeVersion == 4 ? 64 : 60, "file");
            var files = new List<FileInfo>(fileCount);

            for (int fi = 0; fi < fileCount; fi++)
            {
                int fileNameLen = ReadU16Le(span, ref cursor);
                string fileName = ReadString(span, ref cursor, fileNameLen);
                long fileNameHash = ReadI64Le(span, ref cursor);
                UInt128 fileChunkMd5 = ReadU128Le(span, ref cursor);
                UInt128 fileDataMd5 = ReadU128Le(span, ref cursor);
                long offset = ReadI64Le(span, ref cursor);
                long len = ReadI64Le(span, ref cursor);
                BlockType fileBlockType = BlockTypes.FromByte(ReadU8(span, ref cursor));
                bool useEncrypt = ReadU8(span, ref cursor) != 0;
                long ivSeed = useEncrypt ? ReadI64Le(span, ref cursor) : 0;

                FileTag fileTag;
                if (codeVersion > 3)
                {
                    long tagRaw = codeVersion >= 5 ? ReadI64Le(span, ref cursor) : ReadI32Le(span, ref cursor);
                    fileTag = BlockTypes.FileTagFromByte(unchecked((byte)tagRaw));
                }
                else
                {
                    fileTag = FileTag.None;
                }

                files.Add(new FileInfo
                {
                    FileName = fileName,
                    FileNameHash = fileNameHash,
                    FileChunkMd5 = fileChunkMd5,
                    FileDataMd5 = fileDataMd5,
                    Offset = offset,
                    Length = len,
                    BlockType = fileBlockType,
                    UseEncrypt = useEncrypt,
                    IvSeed = ivSeed,
                    FileTag = fileTag,
                });
            }

            chunks.Add(new ChunkInfo
            {
                Md5Name = md5Name,
                ContentMd5 = contentMd5,
                Length = chunkLength,
                BlockType = chunkBlockType,
                MainTag = mainTag,
                Files = files,
            });
        }

        return new BlockMainInfo
        {
            Version = version,
            GroupCfgName = groupCfgName,
            GroupCfgHashName = groupCfgHashName,
            GroupFileInfoNum = groupFileInfoNum,
            GroupChunksLength = groupChunksLength,
            BlockType = blockType,
            Chunks = chunks,
            CodeVersion = codeVersion,
        };
    }

    private static void EnsureAvailable(ReadOnlySpan<byte> span, int cursor, int length)
    {
        if (length < 0 || cursor < 0 || cursor > span.Length || length > span.Length - cursor)
            throw new InvalidDataException($"Truncated VFS index at offset {cursor}: need {length} bytes (index length {span.Length})");
    }

    private static void ValidateCount(ReadOnlySpan<byte> span, int cursor, int count, int minimumSize, string kind)
    {
        if (count < 0 || count > (span.Length - cursor) / minimumSize)
            throw new InvalidDataException($"Invalid VFS {kind} count {count} at offset {cursor}; index is truncated or its format is unsupported");
    }

    private static int ReadI32Le(ReadOnlySpan<byte> span, ref int cursor)
    {
        EnsureAvailable(span, cursor, 4);
        var v = BinaryPrimitives.ReadInt32LittleEndian(span.Slice(cursor, 4));
        cursor += 4;
        return v;
    }

    private static long ReadI64Le(ReadOnlySpan<byte> span, ref int cursor)
    {
        EnsureAvailable(span, cursor, 8);
        var v = BinaryPrimitives.ReadInt64LittleEndian(span.Slice(cursor, 8));
        cursor += 8;
        return v;
    }

    private static ushort ReadU16Le(ReadOnlySpan<byte> span, ref int cursor)
    {
        EnsureAvailable(span, cursor, 2);
        var v = BinaryPrimitives.ReadUInt16LittleEndian(span.Slice(cursor, 2));
        cursor += 2;
        return v;
    }

    private static byte ReadU8(ReadOnlySpan<byte> span, ref int cursor)
    {
        EnsureAvailable(span, cursor, 1);
        var v = span[cursor];
        cursor += 1;
        return v;
    }

    private static UInt128 ReadU128Le(ReadOnlySpan<byte> span, ref int cursor)
    {
        EnsureAvailable(span, cursor, 16);
        var v = BinaryPrimitives.ReadUInt128LittleEndian(span.Slice(cursor, 16));
        cursor += 16;
        return v;
    }

    private static string ReadString(ReadOnlySpan<byte> span, ref int cursor, int len)
    {
        EnsureAvailable(span, cursor, len);
        if (len == 0) return "";
        var slice = span.Slice(cursor, len);
        cursor += len;
        // Match Rust's String::from_utf8_lossy: invalid sequences become U+FFFD.
        return Encoding.UTF8.GetString(slice);
    }
}
