using System.IO;
using System.Security.Cryptography;

namespace Sts2Mod.Core.Developer;

public static class CheckpointFiles
{
    public static CheckpointFileProof Capture(string sourcePath, string outputDirectory)
    {
        if (Directory.Exists(outputDirectory) || File.Exists(outputDirectory))
            throw new IOException("The checkpoint output directory already exists.");

        Directory.CreateDirectory(outputDirectory);
        var path = Path.Combine(outputDirectory, "current_run.save");
        using (var source = File.OpenRead(sourcePath))
        using (var destination = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None))
        {
            source.CopyTo(destination);
        }

        return Verify(path);
    }

    public static CheckpointFileProof Verify(string path)
    {
        using var checkpoint = File.OpenRead(path);
        var sha256 = Convert.ToHexString(SHA256.HashData(checkpoint)).ToLowerInvariant();
        return new(path, sha256, checkpoint.Length);
    }
}
