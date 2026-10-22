using System.Runtime.InteropServices;

namespace Jarvis.Desktop;

public interface IVolume
{
    (int Level, bool Muted) Get();

    /// <summary>Set the master volume (0–100); returns what the device reports afterwards.</summary>
    (int Level, bool Muted) Set(int level);
}

/// <summary>Default output device master volume through Core Audio
/// (IAudioEndpointVolume), the same API the Python tool uses via pycaw.</summary>
public sealed class CoreAudioVolume : IVolume
{
    public (int Level, bool Muted) Get()
    {
        var ep = Endpoint();
        try
        {
            Check(ep.GetMasterVolumeLevelScalar(out var scalar));
            Check(ep.GetMute(out var muted));
            return ((int)Math.Round(scalar * 100), muted);
        }
        finally
        {
            Marshal.ReleaseComObject(ep);
        }
    }

    public (int Level, bool Muted) Set(int level)
    {
        level = Math.Clamp(level, 0, 100);
        var ep = Endpoint();
        try
        {
            var context = Guid.Empty;
            Check(ep.SetMasterVolumeLevelScalar(level / 100f, ref context));
            Check(ep.GetMasterVolumeLevelScalar(out var scalar));
            Check(ep.GetMute(out var muted));
            return ((int)Math.Round(scalar * 100), muted);
        }
        finally
        {
            Marshal.ReleaseComObject(ep);
        }
    }

    /// <summary>Level after applying a delta, clamped to 0–100.</summary>
    public static int Apply(int current, int delta) => Math.Clamp(current + delta, 0, 100);

    private static IAudioEndpointVolume Endpoint()
    {
        var enumerator = (IMMDeviceEnumerator)new MMDeviceEnumeratorCom();
        try
        {
            Check(enumerator.GetDefaultAudioEndpoint(0 /* eRender */, 1 /* eMultimedia */, out var device));
            try
            {
                var iid = typeof(IAudioEndpointVolume).GUID;
                Check(device.Activate(ref iid, 0x17 /* CLSCTX_ALL */, IntPtr.Zero, out var obj));
                return (IAudioEndpointVolume)obj;
            }
            finally
            {
                Marshal.ReleaseComObject(device);
            }
        }
        finally
        {
            Marshal.ReleaseComObject(enumerator);
        }
    }

    private static void Check(int hr)
    {
        if (hr < 0)
            Marshal.ThrowExceptionForHR(hr);
    }

    [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
    private class MMDeviceEnumeratorCom;

    [ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IMMDeviceEnumerator
    {
        [PreserveSig] int EnumAudioEndpoints(int dataFlow, int stateMask, out IntPtr devices);
        [PreserveSig] int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice device);
    }

    [ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IMMDevice
    {
        [PreserveSig] int Activate(ref Guid iid, int clsCtx, IntPtr activationParams,
                                   [MarshalAs(UnmanagedType.IUnknown)] out object iface);
    }

    // Method order is the COM vtable order — do not reorder.
    [ComImport, Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IAudioEndpointVolume
    {
        [PreserveSig] int RegisterControlChangeNotify(IntPtr notify);
        [PreserveSig] int UnregisterControlChangeNotify(IntPtr notify);
        [PreserveSig] int GetChannelCount(out uint count);
        [PreserveSig] int SetMasterVolumeLevel(float levelDb, ref Guid eventContext);
        [PreserveSig] int SetMasterVolumeLevelScalar(float level, ref Guid eventContext);
        [PreserveSig] int GetMasterVolumeLevel(out float levelDb);
        [PreserveSig] int GetMasterVolumeLevelScalar(out float level);
        [PreserveSig] int SetChannelVolumeLevel(uint channel, float levelDb, ref Guid eventContext);
        [PreserveSig] int SetChannelVolumeLevelScalar(uint channel, float level, ref Guid eventContext);
        [PreserveSig] int GetChannelVolumeLevel(uint channel, out float levelDb);
        [PreserveSig] int GetChannelVolumeLevelScalar(uint channel, out float level);
        [PreserveSig] int SetMute([MarshalAs(UnmanagedType.Bool)] bool mute, ref Guid eventContext);
        [PreserveSig] int GetMute([MarshalAs(UnmanagedType.Bool)] out bool mute);
    }
}
