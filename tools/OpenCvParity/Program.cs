// SPDX-License-Identifier: MIT
// OpenCvParity (CI job opencv-parity only; spec Appendix G and section 15 question 8).
//
// Runs Baballonia's VFT preprocessing through OpenCvSharp (the version Baballonia ships) on the
// oracle vectors written by tools/parity/gen_vectors.py and compares the bytes:
//   python tools/parity/gen_vectors.py --out parity
//   dotnet run --project tools/OpenCvParity -c Release -- parity
// Exit 0: every vector equal. 1: a difference (the first mismatch of each vector is printed with
// x, y and both values). 2: usage or input error. A difference is a finding: the oracle, the C
// helper and SWAR then follow OpenCvSharp.
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Text.Json;
using OpenCvSharp;

internal static class Program
{
    private const int Width = 400;
    private const int Height = 400;
    private const int Cols = 200;
    private const int FrameBytes = Width * Height * 2;
    private const int OutBytes = Width * Height;

    private static int Main(string[] args)
    {
        if (args.Length != 1)
        {
            Console.Error.WriteLine("usage: OpenCvParity <directory written by tools/parity/gen_vectors.py>");
            return 2;
        }
        try
        {
            return Run(args[0]);
        }
        catch (Exception e) when (e is IOException || e is JsonException || e is KeyNotFoundException
                                  || e is InvalidDataException || e is InvalidOperationException
                                  || e is FormatException || e is UnauthorizedAccessException)
        {
            Console.Error.WriteLine($"OpenCvParity: {e.GetType().Name}: {e.Message}");
            return 2;
        }
    }

    private static int Run(string dir)
    {
        using var manifest = JsonDocument.Parse(File.ReadAllText(Path.Combine(dir, "manifest.json")));
        var root = manifest.RootElement;
        if (root.GetProperty("width").GetInt32() != Width || root.GetProperty("height").GetInt32() != Height)
            throw new InvalidDataException("manifest is not for 400 x 400 frames");

        // Baballonia's LUT: (byte)(Math.Pow(i / 2048.0, 1.0 / 2.5) * 255.0); must equal Appendix F.
        var lut = new byte[256];
        for (var i = 0; i < 256; i++)
            lut[i] = (byte)(Math.Pow(i / 2048.0, 1.0 / 2.5) * 255.0);
        var failures = 0;
        var index = 0;
        foreach (var value in root.GetProperty("lut").EnumerateArray())
        {
            if (index > 255 || value.GetInt32() != lut[index])
            {
                Console.WriteLine($"FAIL lut[{index}]: manifest {value.GetInt32()} C# {(index > 255 ? -1 : lut[index])}");
                failures++;
            }
            index++;
        }
        if (index != 256)
            throw new InvalidDataException($"manifest lut has {index} entries");

        var frames = new Dictionary<int, byte[]>();
        foreach (var f in root.GetProperty("frames").EnumerateArray())
        {
            var data = File.ReadAllBytes(Path.Combine(dir, Name(f, "file")));
            if (data.Length != FrameBytes)
                throw new InvalidDataException($"{Name(f, "file")}: {data.Length} bytes, expected {FrameBytes}");
            frames[f.GetProperty("index").GetInt32()] = data;
        }

        using var lutMat = Mat.FromPixelData(1, 256, MatType.CV_8UC1, lut);
        var total = 0;
        foreach (var e in root.GetProperty("expected").EnumerateArray())
        {
            var frame = e.GetProperty("frame").GetInt32();
            var phase = e.GetProperty("phase").GetInt32();
            var col0 = e.GetProperty("col0").GetInt32();
            var expected = File.ReadAllBytes(Path.Combine(dir, Name(e, "file")));
            if (expected.Length != OutBytes)
                throw new InvalidDataException($"{Name(e, "file")}: {expected.Length} bytes, expected {OutBytes}");
            var actual = Preprocess(frames[frame], phase, col0, lutMat);
            total++;
            var first = -1;
            var count = 0;
            for (var i = 0; i < OutBytes; i++)
            {
                if (expected[i] == actual[i])
                    continue;
                if (first < 0)
                    first = i;
                count++;
            }
            if (first < 0)
            {
                Console.WriteLine($"PASS frame {frame} phase {phase} col0 {col0}");
                continue;
            }
            failures++;
            Console.WriteLine($"FAIL frame {frame} phase {phase} col0 {col0}: first mismatch at x={first % Width} " +
                              $"y={first / Width}: expected {expected[first]}, OpenCvSharp {actual[first]} " +
                              $"({count} bytes differ)");
        }
        Console.WriteLine(failures == 0
            ? $"OpenCvParity: all {total} vectors equal (OpenCV {Cv2.GetVersionString()})"
            : $"OpenCvParity: {failures} difference(s) in {total} vectors (OpenCV {Cv2.GetVersionString()})");
        return failures == 0 ? 0 : 1;
    }

    private static string Name(JsonElement element, string key) =>
        element.GetProperty(key).GetString() ?? throw new InvalidDataException($"missing {key}");

    // Baballonia's VFT backend: CvtColor(YUV2GRAY_Y422) (odd bytes; YUV2GRAY_YUY2 = even bytes for
    // phase 0), ColRange(col0, col0 + 200), Resize(400 x 400) with the default linear interpolation,
    // GaussianBlur(5 x 5, sigmaX 0), LUT; the result is 400 x 400 CV_8UC1.
    private static byte[] Preprocess(byte[] frame, int phase, int col0, Mat lut)
    {
        using var src = Mat.FromPixelData(Height, Width, MatType.CV_8UC2, frame);
        using var gray = new Mat();
        Cv2.CvtColor(src, gray, phase == 1 ? ColorConversionCodes.YUV2GRAY_Y422 : ColorConversionCodes.YUV2GRAY_YUY2);
        using var roi = gray.ColRange(col0, col0 + Cols);
        using var resized = new Mat();
        Cv2.Resize(roi, resized, new Size(Width, Height));
        using var blurred = new Mat();
        Cv2.GaussianBlur(resized, blurred, new Size(5, 5), 0);
        using var mapped = new Mat();
        Cv2.LUT(blurred, lut, mapped);
        if (mapped.Rows != Height || mapped.Cols != Width || mapped.Type() != MatType.CV_8UC1 || !mapped.IsContinuous())
            throw new InvalidDataException($"unexpected result {mapped.Cols}x{mapped.Rows} type {mapped.Type()}");
        var result = new byte[OutBytes];
        Marshal.Copy(mapped.Data, result, 0, OutBytes);
        return result;
    }
}
