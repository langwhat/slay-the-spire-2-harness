using System.Reflection;
using System.Runtime.Loader;

if (args.Length != 2)
    throw new ArgumentException("Usage: Settings <game-assembly-directory> <new-user-data-directory>");

if (!Path.IsPathFullyQualified(args[0]) || !Path.IsPathFullyQualified(args[1]))
    throw new ArgumentException("Both paths must be absolute.");

var gameDirectory = Path.GetFullPath(args[0]);
var outputDirectory = Path.GetFullPath(args[1]);
if (Directory.Exists(outputDirectory) || File.Exists(outputDirectory))
    throw new ArgumentException("The target user-data directory must not exist.");

AssemblyLoadContext.Default.Resolving += (_, assemblyName) =>
{
    if (string.IsNullOrWhiteSpace(assemblyName.Name))
        return null;
    var candidate = Path.Combine(gameDirectory, assemblyName.Name + ".dll");
    return File.Exists(candidate)
        ? AssemblyLoadContext.Default.LoadFromAssemblyPath(candidate)
        : null;
};

var gameAssembly = AssemblyLoadContext.Default.LoadFromAssemblyPath(Path.Combine(gameDirectory, "sts2.dll"));
var settingsType = gameAssembly.GetType("MegaCrit.Sts2.Core.Saves.SettingsSave", throwOnError: true)!;
var settings = Activator.CreateInstance(settingsType)
    ?? throw new InvalidOperationException("Failed to construct SettingsSave.");

var modSettingsType = gameAssembly.GetType("MegaCrit.Sts2.Core.Modding.ModSettings", throwOnError: true)!;
var modSettings = Activator.CreateInstance(modSettingsType)
    ?? throw new InvalidOperationException("Failed to construct ModSettings.");

modSettingsType.GetProperty("PlayerAgreedToModLoading")!.SetValue(modSettings, true);
settingsType.GetProperty("ModSettings")!.SetValue(settings, modSettings);

var serializerType = gameAssembly.GetType("MegaCrit.Sts2.Core.Saves.JsonSerializationUtility", throwOnError: true)!;
var serializeMethod = serializerType
    .GetMethods(BindingFlags.Public | BindingFlags.Static)
    .Single(method => method.Name == "SerializeAsync" && method.IsGenericMethodDefinition)
    .MakeGenericMethod(settingsType);

var json = await (Task<string>)serializeMethod.Invoke(null, [settings])!;
var settingsDirectory = Path.Combine(outputDirectory, "default", "1");
Directory.CreateDirectory(settingsDirectory);
await File.WriteAllTextAsync(Path.Combine(settingsDirectory, "settings.save"), json);
Console.WriteLine("Native settings created; launch only in this isolated user-data directory.");
