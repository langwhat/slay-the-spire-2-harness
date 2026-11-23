using System.Numerics;
using static Sts2Mod.Observation.NativeRuleBody;

namespace Sts2Mod.Observation;

// Minimize the branch tests that lead to one outcome. Paths that reach another outcome
// exclude their test combinations; combinations that no recorded path takes are free.
internal static class NativeRuleGuards
{
    private const int MaxTests = 10;

    // Returns null when the tests are too many to enumerate or a combination leads to both outcomes.
    internal static List<Test[]>? Minimize(IEnumerable<Test[]> reaching, IEnumerable<Test[]> missing, Func<Test, string> key)
    {
        var on = Normalize(reaching, key);
        var off = Normalize(missing, key);
        if (on.Count == 0)
            return [];

        var names = on.Concat(off).SelectMany(cube => cube.Keys).Distinct().ToList();
        if (names.Count > MaxTests)
            return null;
        var samples = new Dictionary<string, Test>();
        foreach (var cube in on.Concat(off))
            foreach (var (name, test) in cube)
                samples.TryAdd(name, test);

        var onCubes = on.Select(cube => Encode(cube, names)).ToList();
        var offCubes = off.Select(cube => Encode(cube, names)).ToList();
        var minterms = new List<int>();
        var free = new List<int>();
        for (var assignment = 0; assignment < 1 << names.Count; assignment++)
        {
            var isOn = onCubes.Any(cube => (assignment & cube.Mask) == cube.Bits);
            var isOff = offCubes.Any(cube => (assignment & cube.Mask) == cube.Bits);
            if (isOn && isOff)
                return null;
            if (isOn)
                minterms.Add(assignment);
            else if (!isOff)
                free.Add(assignment);
        }

        var primes = PrimeImplicants(minterms.Concat(free));
        var uncovered = minterms.ToHashSet();
        var chosen = new List<(int Value, int Free)>();
        while (uncovered.Count > 0)
        {
            var best = primes
                .OrderByDescending(prime => uncovered.Count(term => Covers(prime, term)))
                .ThenByDescending(prime => BitOperations.PopCount((uint)prime.Free))
                .First();
            chosen.Add(best);
            uncovered.RemoveWhere(term => Covers(best, term));
        }

        return chosen.Select(prime => names
                .Select((name, bit) => (name, bit))
                .Where(entry => (prime.Free & (1 << entry.bit)) == 0)
                .Select(entry => samples[entry.name] with { Expected = (prime.Value & (1 << entry.bit)) != 0 })
                .ToArray())
            .ToList();
    }

    // Drop contradictory paths and repeated tests within a path.
    private static List<Dictionary<string, Test>> Normalize(IEnumerable<Test[]> paths, Func<Test, string> key)
    {
        var cubes = new List<Dictionary<string, Test>>();
        foreach (var path in paths)
        {
            var cube = new Dictionary<string, Test>();
            var feasible = true;
            foreach (var test in path)
            {
                var name = key(test);
                if (cube.TryGetValue(name, out var previous) && previous.Expected != test.Expected)
                {
                    feasible = false;
                    break;
                }
                cube[name] = test;
            }
            if (feasible)
                cubes.Add(cube);
        }
        return cubes;
    }

    private static (int Mask, int Bits) Encode(Dictionary<string, Test> cube, List<string> names)
    {
        var mask = 0;
        var bits = 0;
        foreach (var (name, test) in cube)
        {
            var bit = 1 << names.IndexOf(name);
            mask |= bit;
            if (test.Expected)
                bits |= bit;
        }
        return (mask, bits);
    }

    private static List<(int Value, int Free)> PrimeImplicants(IEnumerable<int> terms)
    {
        var current = terms.Select(term => (Value: term, Free: 0)).ToHashSet();
        var primes = new List<(int Value, int Free)>();
        while (current.Count > 0)
        {
            var next = new HashSet<(int Value, int Free)>();
            var merged = new HashSet<(int Value, int Free)>();
            var list = current.ToList();
            for (var left = 0; left < list.Count; left++)
            {
                for (var right = left + 1; right < list.Count; right++)
                {
                    var (a, b) = (list[left], list[right]);
                    var difference = a.Value ^ b.Value;
                    if (a.Free != b.Free || BitOperations.PopCount((uint)difference) != 1)
                        continue;
                    next.Add((a.Value & ~difference, a.Free | difference));
                    merged.Add(a);
                    merged.Add(b);
                }
            }
            primes.AddRange(list.Where(term => !merged.Contains(term)));
            current = next;
        }
        return primes;
    }

    private static bool Covers((int Value, int Free) prime, int term) => (term & ~prime.Free) == prime.Value;
}
