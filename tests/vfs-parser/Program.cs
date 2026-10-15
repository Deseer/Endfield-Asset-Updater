using System.IO.Hashing;
using System.Buffers.Binary;
using System.Text;
using AnimeStudio.Endfield;

// Synthetic indexes contain no game assets. Run with dotnet run --project tests/vfs-parser.
static byte[] Index(int version, bool encrypted = false, bool legacyHeader = false)
{
    using var stream = new MemoryStream();
    using var w = new BinaryWriter(stream, Encoding.UTF8, true);
    void Name(string name) { var bytes = Encoding.UTF8.GetBytes(name); w.Write((ushort)bytes.Length); w.Write(bytes); }
    void Tag(byte value)
    {
        if (version >= 5) w.Write((long)value);
        else if (version >= 4) w.Write((int)value);
    }
    if (!legacyHeader) w.Write(version);
    w.Write(12345678); Name("Table"); w.Write(1L); w.Write(1); w.Write(10L);
    w.Write((byte)BlockType.Table); w.Write(1);
    w.Write(new byte[32]); w.Write(10L); w.Write((byte)BlockType.Table); Tag(1); w.Write(1);
    Name("test.json"); w.Write(2L); w.Write(new byte[32]); w.Write(0L); w.Write(10L);
    w.Write((byte)BlockType.Table); w.Write(encrypted);
    if (encrypted) w.Write(42L);
    Tag(1);
    if (version >= 5) { w.Write(0L); w.Write((ushort)0); }
    w.Flush(); return Crc(stream.ToArray());
}
static byte[] Crc(byte[] data)
{
    var result = new byte[data.Length + 4]; data.CopyTo(result, 0);
    BinaryPrimitives.WriteUInt32LittleEndian(result.AsSpan(data.Length), Crc32.HashToUInt32(data));
    return result;
}
static void Reject(byte[] data, string message)
{
    try { VfsParser.Parse(data); throw new Exception("Expected InvalidDataException: " + message); }
    catch (InvalidDataException ex) when (ex.Message.Contains(message)) { }
}
int passed = 0;
foreach (int version in new[] { 3, 4, 5 })
foreach (bool encrypted in new[] { false, true })
{
    var info = VfsParser.Parse(Index(version, encrypted));
    var chunk = info.Chunks.Single(); var file = chunk.Files.Single();
    if (info.CodeVersion != version || file.FileName != "test.json" || file.Length != 10 ||
        file.UseEncrypt != encrypted || file.IvSeed != (encrypted ? 42 : 0) ||
        file.FileTag != (version >= 4 ? FileTag.Audit : FileTag.None) ||
        chunk.MainTag != (version >= 4 ? FileTag.Audit : FileTag.None))
        throw new Exception("Version/encryption round trip failed");
    passed++;
}
if (VfsParser.Parse(Index(3, legacyHeader: true)).CodeVersion != 3) throw new Exception("Legacy header failed");
passed++;
var good = Index(5);
var badCrc = (byte[])good.Clone(); badCrc[10] ^= 1; Reject(badCrc, "CRC mismatch"); passed++;
var unsupported = good[..^4]; BinaryPrimitives.WriteInt32LittleEndian(unsupported, 6);
Reject(Crc(unsupported), "Unsupported VFS index code version 6"); passed++;
// Exhaustive truncation, with a valid CRC, must fail before allocating from garbage counts.
for (int length = 0; length < good.Length - 14; length++)
{
    Reject(Crc(good[..length]), ""); passed++;
}
// chunkCount begins after the variable length group name and fixed header fields.
int countOffset = 8 + 2 + Encoding.UTF8.GetByteCount("Table") + 8 + 4 + 8 + 1;
foreach (int value in new[] { -1, int.MaxValue })
{
    var data = good[..^4]; BinaryPrimitives.WriteInt32LittleEndian(data.AsSpan(countOffset), value);
    Reject(Crc(data), "Invalid VFS chunk count"); passed++;
    data = good[..^4]; BinaryPrimitives.WriteInt32LittleEndian(data.AsSpan(countOffset + 4 + 49), value);
    Reject(Crc(data), "Invalid VFS file count"); passed++;
}
Console.WriteLine($"VFS parser: {passed} checks passed");
