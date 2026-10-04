using System.IO.MemoryMappedFiles;

namespace AnimeStudio.Endfield.Processors;

/// <summary>
/// Disk-backed AKPK parser for large PCKs. Only headers and the current WEM
/// enter managed memory; encrypted header/BNK ranges are modified in the
/// scratch file through a writable memory map.
/// </summary>
public sealed class FileAkpkPackage : IAkpkPackage
{
    private static readonly byte[] EncryptedMagic = { (byte)':', (byte)')', (byte)'x', (byte)'D' };
    private static readonly byte[] AkpkMagic = { (byte)'A', (byte)'K', (byte)'P', (byte)'K' };
    private static readonly byte[] BkhdMagic = { (byte)'B', (byte)'K', (byte)'H', (byte)'D' };
    private static readonly byte[] DidxMagic = { (byte)'D', (byte)'I', (byte)'D', (byte)'X' };
    private static readonly byte[] DataMagic = { (byte)'D', (byte)'A', (byte)'T', (byte)'A' };

    private readonly MemoryMappedFile _mapping;
    private readonly MemoryMappedViewAccessor _view;
    private readonly long _length;
    private readonly List<AkpkPackage.WemEntry> _entries = new();
    private readonly Dictionary<uint, string> _languages = new();

    public IReadOnlyList<AkpkPackage.WemEntry> Entries => _entries;

    private FileAkpkPackage(string path)
    {
        _length = new System.IO.FileInfo(path).Length;
        _mapping = MemoryMappedFile.CreateFromFile(
            path,
            FileMode.Open,
            mapName: null,
            capacity: 0,
            MemoryMappedFileAccess.ReadWrite);
        _view = _mapping.CreateViewAccessor(0, 0, MemoryMappedFileAccess.ReadWrite);
    }

    public static FileAkpkPackage Parse(string path)
    {
        var package = new FileAkpkPackage(path);
        try
        {
            package.ParseInternal();
            return package;
        }
        catch
        {
            package.Dispose();
            throw;
        }
    }

    private void ParseInternal()
    {
        if (BytesEqual(0, EncryptedMagic))
        {
            uint headerSize = ReadU32LE(4);
            DecryptRange(12, checked((int)headerSize - 4), headerSize, 0);
            WriteBytes(0, AkpkMagic);
            WriteU32LE(8, 1);
        }

        if (!BytesEqual(0, AkpkMagic))
            throw new InvalidDataException("invalid AKPK magic");

        long pos = 4;
        uint headerSizeValue = ReadU32LE(pos); pos += 4;
        _ = ReadU32LE(pos); pos += 4;
        uint languagesSectorSize = ReadU32LE(pos); pos += 4;
        uint banksSectorSize = ReadU32LE(pos); pos += 4;
        uint soundsSectorSize = ReadU32LE(pos); pos += 4;

        uint externalsSectorSize = 0;
        if (languagesSectorSize + banksSectorSize + soundsSectorSize + 0x10 < headerSizeValue)
        {
            externalsSectorSize = ReadU32LE(pos);
            pos += 4;
        }

        ParseLanguages(pos, languagesSectorSize);
        pos += languagesSectorSize;
        ParseSector(pos, banksSectorSize, isSounds: false, isExternals: false);
        pos += banksSectorSize;
        ParseSector(pos, soundsSectorSize, isSounds: true, isExternals: false);
        pos += soundsSectorSize;
        ParseSector(pos, externalsSectorSize, isSounds: true, isExternals: true);
    }

    private void ParseLanguages(long sectorStart, uint sectorSize)
    {
        if (sectorSize == 0) return;
        long pos = sectorStart;
        uint count = ReadU32LE(pos); pos += 4;
        for (int i = 0; i < count; i++)
        {
            uint langOffset = ReadU32LE(pos); pos += 4;
            uint langId = ReadU32LE(pos); pos += 4;
            long stringPos = sectorStart + langOffset;
            string name;
            if (stringPos + 2 <= _length && (ReadByte(stringPos) == 0 || ReadByte(stringPos + 1) == 0))
            {
                var chars = new List<char>();
                for (long p = stringPos; p + 1 < _length; p += 2)
                {
                    char value = (char)(ReadByte(p) | (ReadByte(p + 1) << 8));
                    if (value == '\0') break;
                    chars.Add(value);
                }
                name = new string(chars.ToArray());
            }
            else
            {
                var bytes = new List<byte>();
                for (long p = stringPos; p < Math.Min(stringPos + 16, _length); p++)
                {
                    byte value = ReadByte(p);
                    if (value == 0) break;
                    bytes.Add(value);
                }
                name = System.Text.Encoding.UTF8.GetString(bytes.ToArray());
            }
            _languages[langId] = name;
        }
    }

    private void ParseSector(long sectorStart, uint sectorSize, bool isSounds, bool isExternals)
    {
        if (sectorSize == 0) return;
        long pos = sectorStart;
        uint fileCount = ReadU32LE(pos); pos += 4;
        if (fileCount == 0) return;
        uint entrySize = (sectorSize - 4) / fileCount;
        bool altMode = entrySize == 0x18;

        for (int i = 0; i < fileCount; i++)
        {
            ulong fileIdLow = ReadU32LE(pos);
            ulong? fileIdHigh = null;
            long p = pos + 4;
            if (altMode && isExternals)
            {
                fileIdHigh = ReadU32LE(p);
                p += 4;
            }
            uint blockSize = ReadU32LE(p); p += 4;
            ulong size;
            if (altMode && isExternals)
            {
                size = ReadU32LE(p); p += 4;
            }
            else if (altMode)
            {
                size = ReadU64LE(p); p += 8;
            }
            else
            {
                size = ReadU32LE(p); p += 4;
            }
            ulong offset = ReadU32LE(p); p += 4;
            uint langId = ReadU32LE(p);
            if (blockSize != 0) offset *= blockSize;
            string? language = _languages.TryGetValue(langId, out string? lang) ? lang : null;
            ulong finalId = fileIdHigh.HasValue ? (fileIdHigh.Value << 32) | fileIdLow : fileIdLow;

            if (!isSounds)
            {
                long bnkStart = checked((long)offset);
                int bnkLength = checked((int)size);
                long bnkEnd = bnkStart + bnkLength;
                DecryptRange(bnkStart, bnkLength, (uint)fileIdLow, 0);
                foreach (var (wemId, wemOffset, wemSize) in ParseBnk(bnkStart, bnkEnd))
                {
                    _entries.Add(new AkpkPackage.WemEntry
                    {
                        Id = wemId,
                        Offset = offset + wemOffset,
                        Size = wemSize,
                        Language = language,
                    });
                }
            }
            else
            {
                _entries.Add(new AkpkPackage.WemEntry
                {
                    Id = finalId,
                    Offset = offset,
                    Size = size,
                    Language = language,
                });
            }
            pos += entrySize;
        }
    }

    private List<(uint Id, uint Offset, uint Size)> ParseBnk(long start, long end)
    {
        var result = new List<(uint, uint, uint)>();
        if (end - start < 8 || !BytesEqual(start, BkhdMagic)) return result;
        uint bkhdSize = ReadU32LE(start + 4);
        long pos = start + 8 + bkhdSize;
        if (pos + 8 > end || !BytesEqual(pos, DidxMagic)) return result;
        uint didxSize = ReadU32LE(pos + 4);
        int count = checked((int)(didxSize / 12));
        long entriesStart = pos + 8;
        long dataSectionStart = entriesStart + didxSize;
        if (dataSectionStart + 8 > end || !BytesEqual(dataSectionStart, DataMagic)) return result;
        long dataOffset = dataSectionStart + 8;
        for (int i = 0; i < count; i++)
        {
            long p = entriesStart + i * 12L;
            result.Add((ReadU32LE(p), checked((uint)(dataOffset + ReadU32LE(p + 4))), ReadU32LE(p + 8)));
        }
        return result;
    }

    public byte[] GetWemData(AkpkPackage.WemEntry entry)
    {
        if (entry.Offset + entry.Size > (ulong)_length || entry.Size > int.MaxValue)
            throw new InvalidDataException($"WEM out of bounds: offset={entry.Offset}, size={entry.Size}, data length={_length}");
        var data = new byte[checked((int)entry.Size)];
        int read = _view.ReadArray(checked((long)entry.Offset), data, 0, data.Length);
        if (read != data.Length) throw new EndOfStreamException("WEM read was truncated");
        if (data.Length >= 4 && !(data[0] == 'R' && data[1] == 'I' && data[2] == 'F' && (data[3] == 'F' || data[3] == 'X')))
            AkpkCrypto.DecryptWem(data, (uint)entry.Id);
        return data;
    }

    private void DecryptRange(long start, int length, uint seed, uint dataOffset)
    {
        const int BufferSize = 1024 * 1024;
        var buffer = new byte[Math.Min(BufferSize, length)];
        int processed = 0;
        while (processed < length)
        {
            int count = Math.Min(buffer.Length, length - processed);
            int read = _view.ReadArray(start + processed, buffer, 0, count);
            if (read != count) throw new EndOfStreamException("AKPK encrypted range was truncated");
            AkpkCrypto.DecryptVfs(buffer, 0, count, seed, unchecked(dataOffset + (uint)processed));
            _view.WriteArray(start + processed, buffer, 0, count);
            processed += count;
        }
        _view.Flush();
    }

    private byte ReadByte(long offset)
    {
        if (offset < 0 || offset >= _length) throw new InvalidDataException($"read out of bounds: {offset}");
        return _view.ReadByte(offset);
    }

    private uint ReadU32LE(long offset)
    {
        if (offset < 0 || offset + 4 > _length) throw new InvalidDataException($"read out of bounds: {offset}");
        return _view.ReadUInt32(offset);
    }

    private ulong ReadU64LE(long offset)
    {
        if (offset < 0 || offset + 8 > _length) throw new InvalidDataException($"read out of bounds: {offset}");
        return _view.ReadUInt64(offset);
    }

    private void WriteU32LE(long offset, uint value) => _view.Write(offset, value);

    private void WriteBytes(long offset, byte[] value)
    {
        _view.WriteArray(offset, value, 0, value.Length);
    }

    private bool BytesEqual(long offset, byte[] expected)
    {
        if (offset < 0 || offset + expected.Length > _length) return false;
        for (int i = 0; i < expected.Length; i++)
            if (_view.ReadByte(offset + i) != expected[i]) return false;
        return true;
    }

    public void Dispose()
    {
        _view.Dispose();
        _mapping.Dispose();
    }
}
