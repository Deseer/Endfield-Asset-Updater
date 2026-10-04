namespace AnimeStudio.Endfield.Processors;

public interface IAkpkPackage : IDisposable
{
    IReadOnlyList<AkpkPackage.WemEntry> Entries { get; }
    byte[] GetWemData(AkpkPackage.WemEntry entry);
}
