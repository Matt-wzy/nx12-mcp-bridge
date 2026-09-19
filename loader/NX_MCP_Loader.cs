// NX_MCP_Loader — NXOpen .NET resident plugin (V2)
// Loaded automatically by NX via a "startup" folder (ufsta/Startup user exit).
// NamedPipe command channel -> thread-safe queue -> SendMessage scheduler on
// the NX main thread executes NXOpen operations. No journal, no Alt+F8, no
// keyboard simulation.
//
// Scheduler: NX's MFC main thread does not pump WinForms messages, so a
// System.Windows.Forms.Timer never fires here. A plain Win32 hidden window
// (NativeWindow) receives our custom message via SendMessage (verified: the NX
// main thread dispatches it synchronously) and runs the NXOpen ops on the NX
// main thread. The pipe/background thread NEVER calls NXOpen directly.
//
// Command protocol (UTF-8 line over named pipe "nx_mcp_loader"):
//   ping
//   nx_status
//   nx_create_part <prt-path>
//   nx_save_part
//   nx_release
//   nx_create_sketch <XY|XZ|YZ>
//   nx_sketch_line <sketch_id> <x1> <y1> <x2> <y2>
//   nx_sketch_rectangle <sketch_id> <cx> <cy> <w> <h>
//   nx_sketch_circle <sketch_id> <cx> <cy> <diameter>
//   nx_sketch_arc <sketch_id> <cx> <cy> <radius> <start_deg> <end_deg>
//   nx_finish_sketch <sketch_id>
//   nx_extrude <sketch_id> <distance> [reverse=0|1] [start_offset] [create|subtract] [body_id]
//   nx_hole <body_id> <cx> <cy> <diameter> <depth> [start_offset]
//   nx_edge_blend <body_id> <radius> [edge_indices csv]
//   nx_chamfer <body_id> <offset> [edge_indices csv]
//   nx_unite <target_body_id> <tool_body_ids csv>
//   nx_revolve <sketch_id> <ax> <ay> <bx> <by> <angle> [reverse=0|1]
//   nx_mirror <body_id> <XY|XZ|YZ> [offset]
//   nx_linear_pattern <body_id> <X|Y|Z> <count> <spacing> [reverse=0|1]
//   nx_fit_view
//   nx_export_step <step-path>
//   nx_build_plate_test           (single-command acceptance part)
//   create_block <w> <h> <d>      (kept for backward compatibility)
//
// Responses are one line of JSON: {"ok":true,...} or {"ok":false,"error":"..."}.
//
// The loader stays resident for the whole NX session. nx_release only clears
// the current task state and restores normal NX interaction; it never stops
// the pipe server or unloads the plugin.
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using NXOpen;
using NXOpen.Features;
using NXOpen.GeometricUtilities;

public static class NX_MCP_Loader
{
    private const string PipeName = "nx_mcp_loader";
    private const int WmSelfTest = 0x8000 + 1; // WM_APP+1

    private static readonly ConcurrentQueue<string> CmdQueue = new ConcurrentQueue<string>();
    private static readonly ConcurrentQueue<string> RespQueue = new ConcurrentQueue<string>();
    private static NamedPipeServerStream _pipe;
    private static System.Threading.Thread _pipeThread;
    private static TimerWindow _timerWindow;
    private static volatile bool _started;

    // ---- task state ----------------------------------------------------
    private static Session _session;
    private static NXOpen.Part _part;
    private static readonly Dictionary<string, Sketch> _sketches = new Dictionary<string, Sketch>();
    private static readonly Dictionary<string, string> _sketchPlanes = new Dictionary<string, string>();
    private static readonly Dictionary<string, Body> _bodies = new Dictionary<string, Body>();
    private static int _sketchCounter;
    private static int _bodyCounter;
    private static string _partPath;

    private static string LogPath
    {
        get
        {
            try
            {
                string ws = Environment.GetEnvironmentVariable("NX_MCP_WORKSPACE");
                if (!string.IsNullOrEmpty(ws)) return Path.Combine(ws, "nx_mcp_loader.log");
            }
            catch { }
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "nx_mcp_loader.log");
        }
    }

    private static void Log(string msg)
    {
        try
        {
            File.AppendAllText(LogPath,
                DateTime.Now.ToString("HH:mm:ss") + " " + msg + Environment.NewLine,
                Encoding.UTF8);
        }
        catch { }
    }

    // ---- JSON helpers --------------------------------------------------
    private static string Esc(string s)
    {
        if (s == null) return "";
        return s.Replace("\\", "\\\\").Replace("\"", "\\\"");
    }

    private static string OkJson(string result)
    {
        return "{\"ok\":true,\"result\":\"" + Esc(result) + "\"}";
    }

    private static string OkJson(string key, string value, string result)
    {
        return "{\"ok\":true,\"" + Esc(key) + "\":\"" + Esc(value) + "\",\"result\":\"" + Esc(result) + "\"}";
    }

    private static string OkJsonList(string key, List<string> values, string result)
    {
        var sb = new StringBuilder();
        sb.Append("{\"ok\":true,\"").Append(Esc(key)).Append("\":[");
        for (int i = 0; i < values.Count; i++)
        {
            if (i > 0) sb.Append(",");
            sb.Append("\"").Append(Esc(values[i])).Append("\"");
        }
        sb.Append("],\"result\":\"").Append(Esc(result)).Append("\"}");
        return sb.ToString();
    }

    private static string ErrJson(string msg)
    {
        return "{\"ok\":false,\"error\":\"" + Esc(msg) + "\"}";
    }

    // ---- NX user exits -------------------------------------------------
    public static int GetUnloadOption(string dummy)
    {
        // AtTermination (UF_UNLOAD_UG_TERMINATE = 3): keep resident for the whole NX session
        return 3;
    }

    public static int Startup()
    {
        if (_started) return 0;
        _started = true;
        try
        {
            Log("Startup() called; thread=" + System.Threading.Thread.CurrentThread.ManagedThreadId +
                " messageLoop=" + Application.MessageLoop);
            _pipeThread = new System.Threading.Thread(PipeLoop) { IsBackground = true };
            _pipeThread.Start();
            _timerWindow = new TimerWindow();
            _timerWindow.Start();
            Log("pipe thread + scheduler window started");
        }
        catch (Exception e)
        {
            Log("Startup ERR: " + e);
        }
        return 0;
    }

    // ---- main-thread scheduler ----------------------------------------
    private sealed class TimerWindow : NativeWindow
    {
        public void Start()
        {
            CreateHandle(new CreateParams());
        }

        protected override void WndProc(ref Message m)
        {
            if (m.Msg == WmSelfTest)
            {
                DrainQueue();
            }
            base.WndProc(ref m);
        }
    }

    private static void DrainQueue()
    {
        string cmd;
        while (CmdQueue.TryDequeue(out cmd))
        {
            try
            {
                string resp = Execute(cmd);
                RespQueue.Enqueue(resp);
                Log("exec ok: " + cmd + " -> " + resp);
            }
            catch (Exception ex)
            {
                RespQueue.Enqueue(ErrJson(ex.Message));
                Log("exec ERR: " + cmd + " -> " + ex);
            }
        }
    }

    [DllImport("user32.dll")]
    private static extern IntPtr SendMessage(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam);

    // ---- pipe server loop (background, never calls NXOpen) -------------
    private static void PipeLoop()
    {
        while (true)
        {
            NamedPipeServerStream inst = null;
            try
            {
                inst = new NamedPipeServerStream(
                    PipeName, PipeDirection.InOut, 1,
                    PipeTransmissionMode.Byte, PipeOptions.Asynchronous);
                _pipe = inst;
                inst.WaitForConnection();
                Log("pipe client connected");
                byte[] lineBytes = ReadLineBytes(inst);
                if (lineBytes == null || lineBytes.Length == 0) continue;
                string line = Encoding.UTF8.GetString(lineBytes).Trim();
                if (line.Length == 0) continue;
                CmdQueue.Enqueue(line);
                Log("cmd queued: " + line);
                SendMessage(_timerWindow.Handle, WmSelfTest, IntPtr.Zero, IntPtr.Zero);
                string resp = null;
                DateTime deadline = DateTime.UtcNow.AddSeconds(300);
                while (DateTime.UtcNow < deadline)
                {
                    if (RespQueue.TryDequeue(out resp)) break;
                    System.Threading.Thread.Sleep(50);
                }
                if (resp == null) resp = "{\"ok\":false,\"error\":\"timeout\"}";
                byte[] outBytes = Encoding.UTF8.GetBytes(resp + "\n");
                inst.Write(outBytes, 0, outBytes.Length);
                inst.Flush();
                Log("resp sent: " + resp);
            }
            catch (Exception e)
            {
                Log("pipe ERR: " + e.Message);
                System.Threading.Thread.Sleep(300);
            }
            finally
            {
                try { if (inst != null) inst.Dispose(); } catch { }
                _pipe = null;
            }
        }
    }

    private static byte[] ReadLineBytes(System.IO.Stream s)
    {
        using (var ms = new MemoryStream())
        {
            int b;
            while ((b = s.ReadByte()) >= 0)
            {
                if (b == (byte)'\n') break;
                ms.WriteByte((byte)b);
            }
            return ms.ToArray();
        }
    }

    // ---- command dispatch ----------------------------------------------
    private static string Execute(string cmd)
    {
        string[] parts = cmd.Split(new char[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
        if (parts.Length == 0) return ErrJson("empty command");
        switch (parts[0])
        {
            case "ping":
                return OkJson("pong");
            case "nx_status":
                return Status();
            case "nx_create_part":
                return CreatePart(parts);
            case "nx_save_part":
                return SavePart();
            case "nx_release":
                return ReleaseTask();
            case "nx_create_sketch":
                return CreateSketch(parts);
            case "nx_sketch_line":
                return SketchLine(parts);
            case "nx_sketch_rectangle":
                return SketchRectangle(parts);
            case "nx_sketch_circle":
                return SketchCircle(parts);
            case "nx_sketch_arc":
                return SketchArc(parts);
            case "nx_finish_sketch":
                return FinishSketch(parts);
            case "nx_extrude":
                return Extrude(parts);
            case "nx_hole":
                return Hole(parts);
            case "nx_counterbore_hole":
                return CounterboreHole(parts);
            case "nx_countersink_hole":
                return CountersinkHole(parts);
            case "nx_shell":
                return Shell(parts);
            case "nx_edge_blend":
                return EdgeBlend(parts);
            case "nx_chamfer":
                return Chamfer(parts);
            case "nx_unite":
                return Unite(parts);
            case "nx_revolve":
                return Revolve(parts);
            case "nx_mirror":
                return Mirror(parts);
            case "nx_linear_pattern":
                return LinearPattern(parts);
            case "nx_circular_pattern":
                return CircularPattern(parts);
            case "nx_fit_view":
                return FitView();
            case "nx_export_step":
                return ExportStep(parts);
            case "nx_open_part":
                return OpenPart(parts);
            case "nx_close_part":
                return ClosePart(parts);
            case "nx_list_sketches":
                return ListObjects("Sketches", "sketch");
            case "nx_list_bodies":
                return ListObjects("Bodies", "body");
            case "nx_list_features":
                return ListObjects("Features", "feature");
            case "nx_list_edges":
                return ListEdges(parts);
            case "nx_list_faces":
                return ListFaces(parts);
            case "nx_undo":
                return Undo();
            case "nx_build_plate_test":
                return BuildPlateTest();
            case "create_block":
                return CreateBlock(parts);
            default:
                return ErrJson("unknown command: " + cmd);
        }
    }

    // ---- commands ------------------------------------------------------
    private static string Status()
    {
        if (_session == null) _session = Session.GetSession();
        try
        {
            // The NX work part is authoritative. _part may become an inactive
            // wrapper when the user closes/switches parts outside the Loader.
            NXOpen.Part work = _session.Parts.Work;
            if (work == null)
            {
                if (_part != null || _partPath != null) ResetTaskState();
                _part = null;
                _partPath = null;
                return "{\"ok\":true,\"ready\":true,\"loader\":\"nx_mcp_loader\",\"nx\":\"2506\"," +
                       "\"part\":\"\",\"sketches\":0,\"bodies\":0}";
            }

            string fullPath = "";
            try { fullPath = work.FullPath; } catch { }
            _part = work;
            _partPath = fullPath;

            // Keep an unsaved active part visible to Runner preflight so it is
            // never mistaken for "no active part".
            string partName = string.IsNullOrEmpty(fullPath)
                ? "<unsaved-active-part>"
                : fullPath;

            return "{\"ok\":true,\"ready\":true,\"loader\":\"nx_mcp_loader\",\"nx\":\"2506\"," +
                   "\"part\":\"" + Esc(partName) + "\",\"sketches\":" + _sketches.Count +
                   ",\"bodies\":" + _bodies.Count + "}";
        }
        catch (Exception e)
        {
            Log("status ERR: " + e.Message);
            return ErrJson("status failed: " + e.Message);
        }
    }

    private static string CreatePart(string[] parts)
    {
        if (parts.Length < 2) return ErrJson("usage: nx_create_part <path>");
        string path = parts[1];
        _session = Session.GetSession();
        var lw = _session.ListingWindow;
        try { lw.Open(); } catch { }

        if (File.Exists(path)) { try { File.Delete(path); } catch { } }

        // Use NewDisplay: FileNewBuilder crashes with access violation in this
        // resident-loader context (unmanaged exception cannot be caught).
        NXOpen.Part part = _session.Parts.NewDisplay(path, Part.Units.Millimeters);
        try { _session.ApplicationSwitchImmediate("UG_APP_MODELING"); } catch { }
        _part = part;
        _partPath = path;
        ResetTaskState();
        return OkJson("part", path, "created");
    }

    private static void ResetTaskState()
    {
        _sketches.Clear();
        _sketchPlanes.Clear();
        _bodies.Clear();
        _sketchCounter = 0;
        _bodyCounter = 0;
    }

    private static string SavePart()
    {
        if (_part == null) return ErrJson("no active part");
        _part.Save(NXOpen.BasePart.SaveComponents.False, NXOpen.BasePart.CloseAfterSave.False);
        return OkJson("saved", _part.FullPath, "saved");
    }

    private static string ReleaseTask()
    {
        // Loader stays resident. Only clear task state and restore normal
        // manual editing (NX is already interactive after each command).
        ResetTaskState();
        _part = null;
        _partPath = null;
        try { FitView(); } catch { }
        return OkJson("task state cleared; loader stays ready");
    }

    private static Plane PlaneZ(double z)
    {
        return _part.Planes.CreatePlane(
            new Point3d(0.0, 0.0, z),
            new Vector3d(0.0, 0.0, 1.0),
            SmartObject.UpdateOption.WithinModeling);
    }

    private static string CreateSketch(string[] parts)
    {
        if (_part == null) return ErrJson("no active part");
        string plane = (parts.Length > 1 ? parts[1] : "XY").ToUpperInvariant();
        if (plane != "XY" && plane != "XZ" && plane != "YZ")
            return ErrJson("plane must be XY, XZ or YZ");

        _sketchCounter++;
        string id = "SKETCH_" + _sketchCounter;
        Plane planeRef = _part.Planes.CreatePlane(
            new Point3d(0.0, 0.0, 0.0),
            SketchPlaneNormal(plane),
            SmartObject.UpdateOption.WithinModeling);

        var b = _part.Sketches.CreateSketchInPlaceBuilder2(null);
        Sketch sk;
        try
        {
            // Attach the sketch to the requested principal plane at the work-part origin.
            // Local sketch coordinates are mapped explicitly by SketchPoint/SketchVector.
            b.PlaneReference = planeRef;
            b.OriginOption = OriginMethod.WorkPartOrigin;
            sk = (Sketch)b.Commit();
        }
        finally
        {
            b.Destroy();
        }
        try { sk.SetName(id); } catch { }
        try { sk.Activate(Sketch.ViewReorient.True); } catch { }
        _sketches[id] = sk;
        _sketchPlanes[id] = plane;
        return OkJson("sketch_id", id, "created plane=" + plane);
    }

    private static string SketchLine(string[] parts)
    {
        if (parts.Length < 6) return ErrJson("usage: nx_sketch_line <sketch_id> x1 y1 x2 y2");
        var sk = GetSketch(parts[1]);
        double x1 = D(parts[2]), y1 = D(parts[3]), x2 = D(parts[4]), y2 = D(parts[5]);
        var curve = _part.Curves.CreateLine(
            SketchPoint(parts[1], x1, y1),
            SketchPoint(parts[1], x2, y2));
        sk.AddGeometry(curve, Sketch.InferConstraintsOption.InferNoConstraints);
        return OkJson("added line " + parts[2] + "," + parts[3] + " -> " + parts[4] + "," + parts[5]);
    }

    private static string SketchRectangle(string[] parts)
    {
        if (parts.Length < 6) return ErrJson("usage: nx_sketch_rectangle <sketch_id> cx cy w h");
        var sk = GetSketch(parts[1]);
        double cx = D(parts[2]), cy = D(parts[3]), w = D(parts[4]), h = D(parts[5]);
        double x1 = cx - w / 2.0, y1 = cy - h / 2.0;
        double x2 = cx + w / 2.0, y2 = cy + h / 2.0;
        AddLine(sk, parts[1], x1, y1, x2, y1);
        AddLine(sk, parts[1], x2, y1, x2, y2);
        AddLine(sk, parts[1], x2, y2, x1, y2);
        AddLine(sk, parts[1], x1, y2, x1, y1);
        return OkJson("added rectangle " + w + "x" + h + " at " + cx + "," + cy);
    }

    private static string SketchCircle(string[] parts)
    {
        if (parts.Length < 5) return ErrJson("usage: nx_sketch_circle <sketch_id> cx cy diameter");
        var sk = GetSketch(parts[1]);
        double cx = D(parts[2]), cy = D(parts[3]), dia = D(parts[4]);
        double r = dia / 2.0;
        string plane = GetSketchPlaneName(parts[1]);
        var ell = _part.Curves.CreateEllipse(
            SketchPoint(parts[1], cx, cy),
            SketchVectorForPlane(plane, 1.0, 0.0),
            SketchVectorForPlane(plane, 0.0, 1.0),
            r, r, 0.0, 2.0 * Math.PI);
        sk.AddGeometry(ell, Sketch.InferConstraintsOption.InferNoConstraints);
        return OkJson("added circle d=" + dia + " at " + cx + "," + cy);
    }

    private static string SketchArc(string[] parts)
    {
        if (parts.Length < 7) return ErrJson("usage: nx_sketch_arc <sketch_id> cx cy radius start_deg end_deg");
        var sk = GetSketch(parts[1]);
        double cx = D(parts[2]), cy = D(parts[3]), r = D(parts[4]);
        double a1 = D(parts[5]), a2 = D(parts[6]);
        double rad1 = a1 * Math.PI / 180.0;
        double rad2 = a2 * Math.PI / 180.0;
        string plane = GetSketchPlaneName(parts[1]);
        var arc = _part.Curves.CreateArc(
            SketchPoint(parts[1], cx, cy),
            SketchVectorForPlane(plane, 1.0, 0.0),
            SketchVectorForPlane(plane, 0.0, 1.0),
            r, rad1, rad2);
        sk.AddGeometry(arc, Sketch.InferConstraintsOption.InferNoConstraints);
        return OkJson("added arc r=" + r + " " + a1 + ".." + a2 + " deg at " + cx + "," + cy);
    }

    private static string FinishSketch(string[] parts)
    {
        if (parts.Length < 2) return ErrJson("usage: nx_finish_sketch <sketch_id>");
        var sk = GetSketch(parts[1]);
        try { sk.Deactivate(Sketch.ViewReorient.True, Sketch.UpdateLevel.Model); } catch { }
        return OkJson("finished " + parts[1]);
    }

    private static string Extrude(string[] parts)
    {
        if (parts.Length < 3) return ErrJson("usage: nx_extrude <sketch_id> <distance> [reverse] [start_offset] [create|subtract] [body_id]");
        var sk = GetSketch(parts[1]);
        double dist = D(parts[2]);
        bool reverse = parts.Length > 3 && parts[3] == "1";
        double startOff = parts.Length > 4 ? D(parts[4]) : 0.0;
        string op = parts.Length > 5 ? parts[5] : "create";
        Body target = null;
        if (parts.Length > 6 && _bodies.ContainsKey(parts[6])) target = _bodies[parts[6]];

        var bt = op == "subtract"
            ? BooleanOperation.BooleanType.Subtract
            : (target != null
                ? BooleanOperation.BooleanType.Unite
                : BooleanOperation.BooleanType.Create);

        var section = _part.Sections.CreateSection();
        var rule = _part.ScRuleFactory.CreateRuleCurveFeature(new Feature[] { sk.Feature });
        section.AddToSection(
            new SelectionIntentRule[] { rule }, null, null, null,
            new Point3d(0.0, 0.0, 0.0), Section.Mode.Create, false);

        Vector3d axis = SketchExtrudeAxis(GetSketchPlaneName(parts[1]));
        if (reverse) axis = new Vector3d(-axis.X, -axis.Y, -axis.Z);
        var dir = _part.Directions.CreateDirection(
            new Point3d(0.0, 0.0, 0.0), axis,
            SmartObject.UpdateOption.WithinModeling);

        var b = _part.Features.CreateExtrudeBuilder(null);
        Feature feature;
        try
        {
            b.Section = section;
            b.Direction = dir;
            b.Limits.StartExtend.Value.RightHandSide = startOff.ToString("0.###", CultureInfo.InvariantCulture);
            b.Limits.EndExtend.Value.RightHandSide = (startOff + dist).ToString("0.###", CultureInfo.InvariantCulture);
            b.BooleanOperation.Type = bt;
            if (target != null)
            {
                b.BooleanOperation.SetTargetBodies(new Body[] { target });
            }
            b.AllowSelfIntersectingSection(true);
            feature = b.CommitFeature();
        }
        finally
        {
            b.Destroy();
        }

        // identify body: last body of the part
        Body[] bodies = _part.Bodies.ToArray();
        Body newBody = null;
        if (bodies.Length > 0) newBody = bodies[bodies.Length - 1];
        if (bt != BooleanOperation.BooleanType.Subtract && newBody != null)
        {
            _bodyCounter++;
            string bid = "BODY_" + _bodyCounter;
            _bodies[bid] = newBody;
            return OkJson("body_id", bid, "extruded " + dist + (reverse ? " (rev)" : ""));
        }
        return OkJson("extruded subtract ok");
    }

    private static string Revolve(string[] parts)
    {
        if (parts.Length < 7) return ErrJson("usage: nx_revolve <sketch_id> <ax> <ay> <bx> <by> <angle> [reverse]");
        var sk = GetSketch(parts[1]);
        double ax = D(parts[2]), ay = D(parts[3]);
        double bx = D(parts[4]), by = D(parts[5]);
        double angle = D(parts[6]);
        bool reverse = parts.Length > 7 && parts[7] == "1";

        double vx = bx - ax, vy = by - ay;
        double len = Math.Sqrt(vx * vx + vy * vy);
        if (len < 1e-9) return ErrJson("revolve axis has zero length");
        vx /= len; vy /= len;
        if (reverse) { vx = -vx; vy = -vy; }

        var section = _part.Sections.CreateSection();
        var rule = _part.ScRuleFactory.CreateRuleCurveFeature(new Feature[] { sk.Feature });
        section.AddToSection(
            new SelectionIntentRule[] { rule }, null, null, null,
            new Point3d(0.0, 0.0, 0.0), Section.Mode.Create, false);

        NXOpen.Axis axis;
        try
        {
            NXOpen.Point axisPoint = _part.Points.CreatePoint(SketchPoint(parts[1], ax, ay));
            NXOpen.Direction axisDir = _part.Directions.CreateDirection(
                axisPoint, SketchVectorForPlane(GetSketchPlaneName(parts[1]), vx, vy));
            axis = _part.Axes.CreateAxis(axisPoint, axisDir, SmartObject.UpdateOption.WithinModeling);
        }
        catch (Exception e) { return ErrJson("revolve[axis] failed: " + e.Message); }

        var b = _part.Features.CreateRevolveBuilder(null);
        Feature feature;
        try
        {
            b.Section = section;
            b.Axis = axis;
            b.Limits.StartExtend.Value.RightHandSide = "0";
            b.Limits.EndExtend.Value.RightHandSide = angle.ToString("0.###", CultureInfo.InvariantCulture);
            b.BooleanOperation.Type = BooleanOperation.BooleanType.Create;
            feature = b.CommitFeature();
        }
        catch (Exception e)
        {
            try { b.Destroy(); } catch { }
            return ErrJson("revolve[commit] failed: " + e.Message);
        }
        b.Destroy();

        Body[] bodies = _part.Bodies.ToArray();
        Body newBody = null;
        if (bodies.Length > 0) newBody = bodies[bodies.Length - 1];
        if (newBody != null)
        {
            _bodyCounter++;
            string bid = "BODY_" + _bodyCounter;
            _bodies[bid] = newBody;
            return OkJson("body_id", bid, "revolved " + angle + " deg");
        }
        return ErrJson("revolve produced no body");
    }

    private static string Mirror(string[] parts)
    {
        if (parts.Length < 3) return ErrJson("usage: nx_mirror <body_id> <XY|XZ|YZ> [offset]");
        Body body = GetBody(parts[1]);
        string planeName = parts[2].ToUpperInvariant();
        double offset = parts.Length > 3 ? D(parts[3]) : 0.0;

        Point3d origin;
        Vector3d mx, my, mz;
        switch (planeName)
        {
            case "XY":
                origin = new Point3d(0.0, 0.0, offset);
                mx = new Vector3d(1, 0, 0); my = new Vector3d(0, 1, 0); mz = new Vector3d(0, 0, 1);
                break;
            case "XZ":
                origin = new Point3d(0.0, offset, 0.0);
                mx = new Vector3d(0, 0, 1); my = new Vector3d(1, 0, 0); mz = new Vector3d(0, 1, 0);
                break;
            case "YZ":
                origin = new Point3d(offset, 0.0, 0.0);
                mx = new Vector3d(0, 1, 0); my = new Vector3d(0, 0, 1); mz = new Vector3d(1, 0, 0);
                break;
            default:
                return ErrJson("plane must be XY, XZ or YZ");
        }
        var m = new Matrix3x3();
        m.Xx = mx.X; m.Xy = mx.Y; m.Xz = mx.Z;
        m.Yx = my.X; m.Yy = my.Y; m.Yz = my.Z;
        m.Zx = mz.X; m.Zy = mz.Y; m.Zz = mz.Z;

        var before = new HashSet<Tag>();
        foreach (Body b in _part.Bodies) before.Add(b.Tag);

        DatumPlane datumPlane;
        var mb = _part.Features.CreateMirrorBodyBuilder(null);
        try
        {
            datumPlane = _part.Datums.CreateFixedDatumPlane(origin, m);
            mb.MirrorBodyList.SetArray(new Body[] { body });
            mb.Plane.SetValue(datumPlane, null, new Point3d(0.0, 0.0, 0.0));
            mb.DeleteSourceBody = false;
            mb.CommitFeature();
        }
        catch (Exception e)
        {
            try { mb.Destroy(); } catch { }
            return ErrJson("mirror failed: " + e.Message);
        }
        mb.Destroy();

        Body newBody = null;
        foreach (Body b in _part.Bodies)
        {
            if (!before.Contains(b.Tag)) { newBody = b; break; }
        }
        if (newBody != null)
        {
            _bodyCounter++;
            string bid = "BODY_" + _bodyCounter;
            _bodies[bid] = newBody;
            return OkJson("body_id", bid, "mirrored about " + planeName);
        }
        return ErrJson("mirror produced no new body");
    }

    private static string LinearPattern(string[] parts)
    {
        if (parts.Length < 5)
            return ErrJson("usage: nx_linear_pattern <body_id> <X|Y|Z> <count> <spacing> [reverse]");
        Body body = GetBody(parts[1]);
        string dirName = parts[2].ToUpperInvariant();
        int count;
        double spacing;
        if (!int.TryParse(parts[3], NumberStyles.Integer, CultureInfo.InvariantCulture, out count))
            return ErrJson("count must be an integer >= 2");
        if (!double.TryParse(parts[4], NumberStyles.Float, CultureInfo.InvariantCulture, out spacing))
            return ErrJson("bad spacing");
        bool reverse = parts.Length > 5 && parts[5] == "1";
        if (count < 2) return ErrJson("count must be >= 2");
        if (double.IsNaN(spacing) || double.IsInfinity(spacing) || spacing <= 0.0)
            return ErrJson("spacing must be > 0");

        Vector3d vec;
        switch (dirName)
        {
            case "X": vec = new Vector3d(1, 0, 0); break;
            case "Y": vec = new Vector3d(0, 1, 0); break;
            case "Z": vec = new Vector3d(0, 0, 1); break;
            default: return ErrJson("direction must be X, Y or Z");
        }

        var before = new HashSet<Tag>();
        foreach (Body b in _part.Bodies) before.Add(b.Tag);

        // Linear pattern via Move-Object copy: NumberOfCopies is the number of
        // NEW instances (excluding the seed), each translated by `spacing` along
        // the direction vector, so count/spacing/reverse semantics are exact and
        // every copy is an independent non-associative body.
        var mb = _part.BaseFeatures.CreateMoveObjectBuilder(null);
        try
        {
            try { mb.ObjectToMoveObject.SetArray(new NXObject[] { body }); }
            catch (Exception e) { throw new Exception("step Objects: " + e.Message, e); }
            try
            {
                mb.Associative = false;
                mb.MoveObjectResult = MoveObjectBuilder.MoveObjectResultOptions.CopyOriginal;
                mb.NumberOfCopies = count - 1;
            }
            catch (Exception e) { throw new Exception("step Options: " + e.Message, e); }

            try
            {
                Vector3d dirVec = reverse
                    ? new Vector3d(-vec.X, -vec.Y, -vec.Z)
                    : vec;
                Direction dirObj = _part.Directions.CreateDirection(
                    new Point3d(0.0, 0.0, 0.0), dirVec,
                    SmartObject.UpdateOption.WithinModeling);
                ModlMotion motion = mb.TransformMotion;
                motion.Option = ModlMotion.Options.Distance;
                motion.DistanceVector = dirObj;
                motion.DistanceValue.RightHandSide =
                    spacing.ToString("0.###", CultureInfo.InvariantCulture);
            }
            catch (Exception e) { throw new Exception("step Motion: " + e.Message, e); }

            try { mb.Commit(); }
            catch (Exception e) { throw new Exception("step Commit: " + e.Message, e); }
        }
        catch (Exception e)
        {
            try { mb.Destroy(); } catch { }
            return ErrJson("linear pattern failed: " + e.Message);
        }
        mb.Destroy();

        var newIds = new List<string>();
        foreach (Body b in _part.Bodies)
        {
            if (!before.Contains(b.Tag))
            {
                _bodyCounter++;
                string bid = "BODY_" + _bodyCounter;
                _bodies[bid] = b;
                newIds.Add(bid);
            }
        }
        if (newIds.Count == 0) return ErrJson("linear pattern produced no new bodies");
        return OkJsonList("body_ids", newIds,
            "linear pattern " + count + " along " + dirName + (reverse ? " (reverse)" : ""));
    }

    private static string CircularPattern(string[] parts)
    {
        // nx_circular_pattern <body_id> <X|Y|Z> <cx> <cy> <cz> <count> <angle> [reverse]
        if (parts.Length < 8)
            return ErrJson("usage: nx_circular_pattern <body_id> <X|Y|Z> <cx> <cy> <cz> <count> <angle> [reverse]");
        Body body = GetBody(parts[1]);
        string axisName = parts[2].ToUpperInvariant();
        double cx = D(parts[3]), cy = D(parts[4]), cz = D(parts[5]);
        int count;
        double totalAngle;
        if (!int.TryParse(parts[6], NumberStyles.Integer, CultureInfo.InvariantCulture, out count))
            return ErrJson("count must be an integer >= 2");
        if (!double.TryParse(parts[7], NumberStyles.Float, CultureInfo.InvariantCulture, out totalAngle))
            return ErrJson("bad angle");
        bool reverse = parts.Length > 8 && parts[8] == "1";
        if (count < 2) return ErrJson("count must be >= 2");
        if (double.IsNaN(totalAngle) || double.IsInfinity(totalAngle)
            || totalAngle <= 0.0 || totalAngle > 360.0)
            return ErrJson("angle must be in (0, 360]");

        Vector3d axisVec;
        switch (axisName)
        {
            case "X": axisVec = new Vector3d(1, 0, 0); break;
            case "Y": axisVec = new Vector3d(0, 1, 0); break;
            case "Z": axisVec = new Vector3d(0, 0, 1); break;
            default: return ErrJson("axis must be X, Y or Z");
        }
        // Angle step semantics:
        //  - angle == 360: step = 360/count (full circle, instances at 0..360-step)
        //  - angle <  360: step = angle/(count-1) (seed at 0, last instance at `angle`)
        double stepAngle = (Math.Abs(totalAngle - 360.0) < 1e-9)
            ? totalAngle / count
            : totalAngle / (count - 1);

        var before = new HashSet<Tag>();
        foreach (Body b in _part.Bodies) before.Add(b.Tag);

        // Circular pattern by rigid rotation copy. Each copy is an independent
        // rigid rotation of the seed about the axis through `center`, by the
        // absolute angle theta_i = sign * i * stepAngle. We set the 3x3 rotation
        // matrix on the Move-Object manipulator so the solid's position AND
        // own orientation both rotate (point-to-point translation kept position
        // but never rotated the body). Matrix convention: local axes rotate by
        // theta, i.e. point P -> R * P about the pivot (WCS origin == center).
        double sign = reverse ? -1.0 : 1.0;

        for (int i = 1; i < count; i++)
        {
            double th = sign * i * stepAngle * Math.PI / 180.0;
            double c = Math.Cos(th), s = Math.Sin(th);
            Matrix3x3 mat = new Matrix3x3();
            mat.Xx = 1.0; mat.Xy = 0.0; mat.Xz = 0.0;
            mat.Yx = 0.0; mat.Yy = 1.0; mat.Yz = 0.0;
            mat.Zx = 0.0; mat.Zy = 0.0; mat.Zz = 1.0;
            switch (axisName)
            {
                case "Z": mat.Xx = c; mat.Xy = -s; mat.Yx = s; mat.Yy = c; mat.Zz = 1.0; break;
                case "X": mat.Xx = 1.0; mat.Yy = c; mat.Yz = -s; mat.Zy = s; mat.Zz = c; break;
                case "Y": mat.Xx = c; mat.Xz = s; mat.Yy = 1.0; mat.Zx = -s; mat.Zz = c; break;
            }

            var mb = _part.BaseFeatures.CreateMoveObjectBuilder(null);
            try
            {
                mb.ObjectToMoveObject.SetArray(new NXObject[] { body });
                mb.Associative = false;
                mb.MoveObjectResult = MoveObjectBuilder.MoveObjectResultOptions.CopyOriginal;
                mb.NumberOfCopies = 1;
                ModlMotion motion = mb.TransformMotion;
                motion.Option = ModlMotion.Options.Dynamic;
                motion.ManipulatorMatrix = mat;
                mb.Commit();
            }
            catch (Exception e)
            {
                try { mb.Destroy(); } catch { }
                return ErrJson("circular pattern failed at instance " + i + ": " + e.Message);
            }
            mb.Destroy();
        }

        var newIds = new List<string>();
        foreach (Body b in _part.Bodies)
        {
            if (!before.Contains(b.Tag))
            {
                _bodyCounter++;
                string bid = "BODY_" + _bodyCounter;
                _bodies[bid] = b;
                newIds.Add(bid);
            }
        }
        if (newIds.Count == 0) return ErrJson("circular pattern produced no new bodies");
        return OkJsonList("body_ids", newIds,
            "circular pattern " + count + " about " + axisName
            + " sweep " + totalAngle + (reverse ? " (reverse)" : ""));
    }

    private static string Hole(string[] parts)
    {
        if (parts.Length < 6) return ErrJson("usage: nx_hole <body_id> cx cy diameter depth [start_offset]");
        Body body = GetBody(parts[1]);
        double cx = D(parts[2]), cy = D(parts[3]), dia = D(parts[4]), depth = D(parts[5]);
        double startOff = parts.Length > 6 ? D(parts[6]) : 0.0;

        _sketchCounter++;
        string id = "SKETCH_HOLE_" + _sketchCounter;
        var b = _part.Sketches.CreateSketchInPlaceBuilder2(null);
        Sketch sk;
        try
        {
            sk = (Sketch)b.Commit();
        }
        finally
        {
            b.Destroy();
        }
        try { sk.SetName(id); } catch { }
        try { sk.Activate(Sketch.ViewReorient.True); } catch { }
        _sketches[id] = sk;

        double r = dia / 2.0;
        var ell = _part.Curves.CreateEllipse(
            new Point3d(cx, cy, 0.0),
            new Vector3d(1.0, 0.0, 0.0), new Vector3d(0.0, 1.0, 0.0),
            r, r, 0.0, 2.0 * Math.PI);
        sk.AddGeometry(ell, Sketch.InferConstraintsOption.InferNoConstraints);
        try { sk.Deactivate(Sketch.ViewReorient.True, Sketch.UpdateLevel.Model); } catch { }

        ExtrudeSketchCore(sk, startOff, startOff + depth,
            BooleanOperation.BooleanType.Subtract, body);
        return OkJson("hole d=" + dia + " depth=" + depth + " at " + cx + "," + cy);
    }

    private static string CounterboreHole(string[] parts)
    {
        if (parts.Length < 8)
            return ErrJson("usage: nx_counterbore_hole <body_id> cx cy hole_dia hole_depth cb_dia cb_depth [start_offset]");
        Body body = GetBody(parts[1]);
        double cx = D(parts[2]), cy = D(parts[3]);
        double holeDia = D(parts[4]), holeDepth = D(parts[5]);
        double cbDia = D(parts[6]), cbDepth = D(parts[7]);
        double startOff = parts.Length > 8 ? D(parts[8]) : 0.0;

        if (double.IsNaN(holeDia) || double.IsInfinity(holeDia) || holeDia <= 0)
            return ErrJson("hole_diameter must be a positive finite number");
        if (double.IsNaN(holeDepth) || double.IsInfinity(holeDepth) || holeDepth <= 0)
            return ErrJson("hole_depth must be a positive finite number");
        if (double.IsNaN(cbDia) || double.IsInfinity(cbDia) || cbDia <= 0)
            return ErrJson("counterbore_diameter must be a positive finite number");
        if (double.IsNaN(cbDepth) || double.IsInfinity(cbDepth) || cbDepth <= 0)
            return ErrJson("counterbore_depth must be > 0");
        if (cbDia <= holeDia)
            return ErrJson("counterbore_diameter must be > hole_diameter");
        if (cbDepth >= holeDepth)
            return ErrJson("counterbore_depth must be < hole_depth");

        // 1) main through hole (smaller dia, deeper)
        double rHole = holeDia / 2.0;
        _sketchCounter++;
        var b1 = _part.Sketches.CreateSketchInPlaceBuilder2(null);
        Sketch sk1;
        try { sk1 = (Sketch)b1.Commit(); }
        finally { b1.Destroy(); }
        try { sk1.SetName("SKETCH_CB_HOLE_" + _sketchCounter); } catch { }
        try { sk1.Activate(Sketch.ViewReorient.True); } catch { }
        _sketches["SKETCH_CB_HOLE_" + _sketchCounter] = sk1;
        var ellHole = _part.Curves.CreateEllipse(
            new Point3d(cx, cy, 0.0),
            new Vector3d(1.0, 0.0, 0.0), new Vector3d(0.0, 1.0, 0.0),
            rHole, rHole, 0.0, 2.0 * Math.PI);
        sk1.AddGeometry(ellHole, Sketch.InferConstraintsOption.InferNoConstraints);
        try { sk1.Deactivate(Sketch.ViewReorient.True, Sketch.UpdateLevel.Model); } catch { }
        ExtrudeSketchCore(sk1, startOff, startOff + holeDepth,
            BooleanOperation.BooleanType.Subtract, body);

        // 2) counterbore (larger dia, shallower, coaxial, from top surface down cbDepth)
        double rCb = cbDia / 2.0;
        _sketchCounter++;
        var b2 = _part.Sketches.CreateSketchInPlaceBuilder2(null);
        Sketch sk2;
        try { sk2 = (Sketch)b2.Commit(); }
        finally { b2.Destroy(); }
        try { sk2.SetName("SKETCH_CB_" + _sketchCounter); } catch { }
        try { sk2.Activate(Sketch.ViewReorient.True); } catch { }
        _sketches["SKETCH_CB_" + _sketchCounter] = sk2;
        var ellCb = _part.Curves.CreateEllipse(
            new Point3d(cx, cy, 0.0),
            new Vector3d(1.0, 0.0, 0.0), new Vector3d(0.0, 1.0, 0.0),
            rCb, rCb, 0.0, 2.0 * Math.PI);
        sk2.AddGeometry(ellCb, Sketch.InferConstraintsOption.InferNoConstraints);
        try { sk2.Deactivate(Sketch.ViewReorient.True, Sketch.UpdateLevel.Model); } catch { }
        ExtrudeSketchCore(sk2, startOff, startOff + cbDepth,
            BooleanOperation.BooleanType.Subtract, body);

        return OkJson("counterbore hole d=" + holeDia + " depth=" + holeDepth
            + " cb_d=" + cbDia + " cb_depth=" + cbDepth + " at " + cx + "," + cy);
    }

    private static string CountersinkHole(string[] parts)
    {
        if (parts.Length < 8)
            return ErrJson("usage: nx_countersink_hole <body_id> cx cy hole_dia hole_depth cs_dia cs_angle [start_offset]");
        Body body = GetBody(parts[1]);
        double cx = D(parts[2]), cy = D(parts[3]);
        double holeDia = D(parts[4]), holeDepth = D(parts[5]);
        double csDia = D(parts[6]), csAngle = D(parts[7]);
        double startOff = parts.Length > 8 ? D(parts[8]) : 0.0;

        if (double.IsNaN(holeDia) || double.IsInfinity(holeDia) || holeDia <= 0)
            return ErrJson("hole_diameter must be a positive finite number");
        if (double.IsNaN(holeDepth) || double.IsInfinity(holeDepth) || holeDepth <= 0)
            return ErrJson("hole_depth must be a positive finite number");
        if (double.IsNaN(csDia) || double.IsInfinity(csDia) || csDia <= 0)
            return ErrJson("countersink_diameter must be a positive finite number");
        if (double.IsNaN(csAngle) || double.IsInfinity(csAngle) || csAngle <= 0.0 || csAngle >= 180.0)
            return ErrJson("countersink_angle must be in (0,180)");
        if (csDia <= holeDia)
            return ErrJson("countersink_diameter must be > hole_diameter");

        // countersink depth from included angle:
        // depth = ((cs_dia - hole_dia)/2) / tan(cs_angle/2)
        double csDepth = ((csDia - holeDia) / 2.0) / Math.Tan(csAngle / 2.0 * Math.PI / 180.0);
        if (csDepth >= holeDepth)
            return ErrJson("computed countersink depth must be < hole_depth");

        // 1) main through hole (smaller dia, deeper cylinder subtract)
        double rHole = holeDia / 2.0;
        _sketchCounter++;
        var b1 = _part.Sketches.CreateSketchInPlaceBuilder2(null);
        Sketch sk1;
        try { sk1 = (Sketch)b1.Commit(); }
        finally { b1.Destroy(); }
        try { sk1.SetName("SKETCH_CS_HOLE_" + _sketchCounter); } catch { }
        try { sk1.Activate(Sketch.ViewReorient.True); } catch { }
        _sketches["SKETCH_CS_HOLE_" + _sketchCounter] = sk1;
        var ellHole = _part.Curves.CreateEllipse(
            new Point3d(cx, cy, 0.0),
            new Vector3d(1.0, 0.0, 0.0), new Vector3d(0.0, 1.0, 0.0),
            rHole, rHole, 0.0, 2.0 * Math.PI);
        sk1.AddGeometry(ellHole, Sketch.InferConstraintsOption.InferNoConstraints);
        try { sk1.Deactivate(Sketch.ViewReorient.True, Sketch.UpdateLevel.Model); } catch { }
        ExtrudeSketchCore(sk1, startOff, startOff + holeDepth,
            BooleanOperation.BooleanType.Subtract, body);

        // 2) conical countersink (cone, large top dia -> small bottom dia, subtract)
        NXOpen.Point axisPt = _part.Points.CreatePoint(new Point3d(cx, cy, startOff));
        NXOpen.Direction axisDir = _part.Directions.CreateDirection(
            new Point3d(cx, cy, startOff), new Vector3d(0.0, 0.0, 1.0),
            SmartObject.UpdateOption.WithinModeling);
        Axis coneAxis = _part.Axes.CreateAxis(axisPt, axisDir, SmartObject.UpdateOption.WithinModeling);

        var cb = _part.Features.CreateConeBuilder(null);
        try
        {
            cb.Type = NXOpen.Features.ConeBuilder.Types.DiametersAndHeight;
            cb.Axis = coneAxis;
            cb.BaseDiameter.RightHandSide = csDia.ToString("0.###", CultureInfo.InvariantCulture);
            cb.TopDiameter.RightHandSide = holeDia.ToString("0.###", CultureInfo.InvariantCulture);
            cb.Height.RightHandSide = csDepth.ToString("0.###", CultureInfo.InvariantCulture);
            cb.BooleanOption.Type = BooleanOperation.BooleanType.Subtract;
            cb.BooleanOption.SetTargetBodies(new Body[] { body });
            cb.CommitFeature();
        }
        catch (Exception e)
        {
            try { cb.Destroy(); } catch { }
            return ErrJson("countersink cone failed: " + e.Message);
        }
        cb.Destroy();

        return OkJson("countersink hole d=" + holeDia + " depth=" + holeDepth
            + " cs_d=" + csDia + " angle=" + csAngle + " depth=" + csDepth.ToString("0.###")
            + " at " + cx + "," + cy);
    }

    private static string Shell(string[] parts)
    {
        if (parts.Length < 4)
            return ErrJson("usage: nx_shell <body_id> <thickness> <remove_face_index> [inward=1]");
        Body body = GetBody(parts[1]);
        double thickness = D(parts[2]);
        int removeIdx;
        if (!int.TryParse(parts[3], out removeIdx))
            return ErrJson("remove_face_index must be an integer");
        bool inward = parts.Length < 5 || parts[4] != "0";

        if (double.IsNaN(thickness) || double.IsInfinity(thickness) || thickness <= 0)
            return ErrJson("thickness must be > 0");
        Face[] faces = body.GetFaces();
        if (removeIdx < 0 || removeIdx >= faces.Length)
            return ErrJson("remove_face_index out of range (0.." + (faces.Length - 1) + ")");

        var coll = _part.ScCollectors.CreateCollector();
        var faceRule = _part.ScRuleFactory.CreateRuleFaceDumb(new Face[] { faces[removeIdx] });
        coll.AddRules(new SelectionIntentRule[] { faceRule });

        var sb = _part.Features.CreateShellBuilder(null);
        try
        {
            sb.Body = body;
            sb.Tolerance = 0.0254;
            sb.DefaultThickness.RightHandSide = thickness.ToString("0.###", CultureInfo.InvariantCulture);
            sb.DefaultThicknessFlip = inward;
            sb.RemovedFacesCollector = coll;
            sb.CommitFeature();
        }
        catch (Exception e)
        {
            try { sb.Destroy(); } catch { }
            try { coll.Destroy(); } catch { }
            return ErrJson("shell failed: " + e.Message);
        }
        sb.Destroy();
        try { coll.Destroy(); } catch { }

        return OkJson("body_id", parts[1],
            "shell t=" + thickness + " face=" + removeIdx + " inward=" + inward);
    }

    private static string ListEdges(string[] parts)
    {
        if (parts.Length < 2) return ErrJson("usage: nx_list_edges <body_id>");
        Body body;
        try { body = GetBody(parts[1]); }
        catch
        {
            // open-part sessions do not repopulate _bodies; fall back to the
            // first solid body in the work part.
            Body[] all = _part.Bodies.ToArray();
            body = null;
            foreach (Body b in all) if (b.IsSolidBody) { body = b; break; }
            if (body == null && all.Length > 0) body = all[0];
            if (body == null) return ErrJson("no body in work part");
        }
        Edge[] edges = body.GetEdges();
        var sb = new StringBuilder();
        sb.Append("{\"ok\":true,\"body_id\":\"").Append(Esc(parts[1])).Append("\",\"edge_count\":").Append(edges.Length).Append(",\"edges\":[");
        for (int i = 0; i < edges.Length; i++)
        {
            Edge e = edges[i];
            if (i > 0) sb.Append(",");
            string ctype = "Other";
            try { ctype = e.SolidEdgeType.ToString(); } catch { }
            Point3d s = new Point3d(), en = new Point3d();
            try { e.GetVertices(out s, out en); } catch { }
            double mx = (s.X + en.X) / 2.0, my = (s.Y + en.Y) / 2.0, mz = (s.Z + en.Z) / 2.0;
            bool linear = (ctype == "Linear");
            // bbox: exact for linear edges (start/end min/max); for curves the
            // endpoint min/max is NOT the true bounding box, so return null
            // rather than misleading geometry.
            string bbMinStr = "null", bbMaxStr = "null";
            if (linear)
            {
                Point3d bbmin = new Point3d(
                    Math.Min(s.X, en.X), Math.Min(s.Y, en.Y), Math.Min(s.Z, en.Z));
                Point3d bbmax = new Point3d(
                    Math.Max(s.X, en.X), Math.Max(s.Y, en.Y), Math.Max(s.Z, en.Z));
                bbMinStr = "[" + F(bbmin.X) + "," + F(bbmin.Y) + "," + F(bbmin.Z) + "]";
                bbMaxStr = "[" + F(bbmax.X) + "," + F(bbmax.Y) + "," + F(bbmax.Z) + "]";
            }
            double length = 0.0;
            try { length = e.GetLength(); } catch { }
            // direction: only for linear edges
            string dir = "OTHER";
            try
            {
                if (ctype == "Linear")
                {
                    double dx = Math.Abs(en.X - s.X), dy = Math.Abs(en.Y - s.Y), dz = Math.Abs(en.Z - s.Z);
                    if (dx > 1e-6 && dy < 1e-6 && dz < 1e-6) dir = "X";
                    else if (dy > 1e-6 && dx < 1e-6 && dz < 1e-6) dir = "Y";
                    else if (dz > 1e-6 && dx < 1e-6 && dy < 1e-6) dir = "Z";
                }
            }
            catch { }
            int nAdj = 0;
            try { Face[] af = e.GetFaces(); nAdj = (af != null) ? af.Length : 0; } catch { }
            string tagStr = "0";
            try { tagStr = e.Tag.ToString(); } catch { }
            sb.Append("{\"index\":").Append(i)
              .Append(",\"tag\":").Append(tagStr)
              .Append(",\"curve_type\":\"").Append(ctype).Append("\"")
              .Append(",\"start\":[").Append(F(s.X)).Append(",").Append(F(s.Y)).Append(",").Append(F(s.Z)).Append("]")
              .Append(",\"end\":[").Append(F(en.X)).Append(",").Append(F(en.Y)).Append(",").Append(F(en.Z)).Append("]")
              .Append(",\"midpoint\":[").Append(F(mx)).Append(",").Append(F(my)).Append(",").Append(F(mz)).Append("]")
              .Append(",\"length\":").Append(F(length))
              .Append(",\"bbox_min\":").Append(bbMinStr)
              .Append(",\"bbox_max\":").Append(bbMaxStr)
              .Append(",\"direction\":\"").Append(dir).Append("\"")
              .Append(",\"adjacent_faces\":").Append(nAdj)
              .Append("}");
        }
        sb.Append("]}");
        return sb.ToString();
    }

    private static string ListFaces(string[] parts)
    {
        if (parts.Length < 2) return ErrJson("usage: nx_list_faces <body_id>");
        Body body;
        try { body = GetBody(parts[1]); }
        catch
        {
            Body[] all = _part.Bodies.ToArray();
            body = null;
            foreach (Body b in all) if (b.IsSolidBody) { body = b; break; }
            if (body == null && all.Length > 0) body = all[0];
            if (body == null) return ErrJson("no body in work part");
        }
        Face[] faces = body.GetFaces();
        var sb = new StringBuilder();
        sb.Append("{\"ok\":true,\"body_id\":\"").Append(Esc(parts[1])).Append("\",\"face_count\":").Append(faces.Length).Append(",\"faces\":[");
        for (int i = 0; i < faces.Length; i++)
        {
            Face fc = faces[i];
            if (i > 0) sb.Append(",");
            string ftype = "Other";
            try { ftype = fc.SolidFaceType.ToString(); } catch { }
            string tagStr = "0";
            try { tagStr = fc.Tag.ToString(); } catch { }
            double area = 0.0;
            Point3d cent = new Point3d();
            Point3d normalP = new Point3d();
            bool planar = (ftype == "Planar");
            string faceErr = "";
            bool measured = false;
            try
            {
                var measProp = _session.GetType().GetProperty("Measurement",
                    System.Reflection.BindingFlags.Public | System.Reflection.BindingFlags.Instance);
                object meas = (measProp != null) ? measProp.GetValue(_session, null) : null;
                if (meas != null)
                {
                    var measType = meas.GetType();
                    var mGetFP = measType.GetMethod("GetFaceProperties");
                    if (mGetFP != null)
                    {
                        var par = mGetFP.GetParameters();
                    object alt = 0;
                    foreach (var pp in par)
                    {
                        if (pp.IsOut) continue;
                        if (pp.ParameterType.IsEnum) alt = System.Enum.Parse(pp.ParameterType, "Radius");
                    }
                    object[] margs = new object[] {
                        new ISurface[] { fc },
                        0.0,
                        alt,
                        false,
                        0.0, 0.0, null, new Point3d(), 0.0, 0.0, new Point3d(), false
                    };
                    mGetFP.Invoke(meas, margs);
                    area = (double)margs[4];
                    cent = (Point3d)margs[7];
                    measured = true;
                    }
                }
            }
            catch (System.Exception ex) { faceErr = ex.Message; }
            if (!measured)
            {
                area = 0.0; cent = new Point3d();
            }
            // normal for planar
            if (planar)
            {
                var t = fc.GetType();
                var surfProp = t.GetProperty("Surface");
                object surf = (surfProp != null) ? surfProp.GetValue(fc, null) : null;
                if (surf == null)
                {
                    var mGetSurf = t.GetMethod("GetSurface", new System.Type[0]);
                    if (mGetSurf != null) surf = mGetSurf.Invoke(fc, null);
                }
                if (surf is NXOpen.Plane)
                {
                    NXOpen.Vector3d nv = ((NXOpen.Plane)surf).Normal;
                    normalP = new Point3d(nv.X, nv.Y, nv.Z);
                }
            }
            string normalStr = "null";
            if (planar && (normalP.X != 0 || normalP.Y != 0 || normalP.Z != 0))
            {
                normalStr = "[" + F(normalP.X) + "," + F(normalP.Y) + "," + F(normalP.Z) + "]";
            }
            int nAdj = 0;
            try { Edge[] ed = fc.GetEdges(); nAdj = (ed != null) ? ed.Length : 0; } catch { }
            sb.Append("{\"index\":").Append(i)
              .Append(",\"tag\":").Append(tagStr)
              .Append(",\"face_type\":\"").Append(ftype).Append("\"")
              .Append(",\"centroid\":[").Append(F(cent.X)).Append(",").Append(F(cent.Y)).Append(",").Append(F(cent.Z)).Append("]")
              .Append(",\"area\":").Append(F(area))
              .Append(",\"normal\":").Append(normalStr)
              .Append(",\"adjacent_edges\":").Append(nAdj)
              .Append(",\"err\":\"").Append(Esc(faceErr)).Append("\"")
              .Append("}");
        }
        sb.Append("]}");
        return sb.ToString();
    }

    private static string F(double v)
    {
        return v.ToString("0.###", CultureInfo.InvariantCulture);
    }

    private static string EdgeBlend(string[] parts)
    {
        if (parts.Length < 3) return ErrJson("usage: nx_edge_blend <body_id> <radius> [edge_indices csv]");
        Body body = GetBody(parts[1]);
        double r = D(parts[2]);
        int[] idx = parts.Length > 3 ? ParseIndices(parts[3]) : null;
        int done = BlendChamfer(body, r, idx, true);
        return OkJson("edge_blend done=" + done);
    }

    private static string Chamfer(string[] parts)
    {
        if (parts.Length < 3) return ErrJson("usage: nx_chamfer <body_id> <offset> [edge_indices csv]");
        Body body = GetBody(parts[1]);
        double off = D(parts[2]);
        int[] idx = parts.Length > 3 ? ParseIndices(parts[3]) : null;
        int done = BlendChamfer(body, off, idx, false);
        return OkJson("chamfer done=" + done);
    }

    private static string Unite(string[] parts)
    {
        if (parts.Length < 3) return ErrJson("usage: nx_unite <target_body_id> <tool_body_ids csv>");
        Body target;
        try { target = GetBody(parts[1]); }
        catch (Exception e) { return ErrJson(e.Message); }
        List<Body> tools = new List<Body>();
        string[] ids = parts[2].Split(',');
        foreach (string id in ids)
        {
            string t = id.Trim();
            if (t.Length == 0) continue;
            try { tools.Add(GetBody(t)); }
            catch (Exception e) { return ErrJson(e.Message); }
        }
        if (tools.Count == 0) return ErrJson("no tool bodies given");
        try
        {
            if (_session == null) _session = Session.GetSession();
            NXOpen.Features.BooleanBuilder bld = null;
            try { bld = _part.Features.CreateBooleanBuilder(null); }
            catch (Exception e) { return ErrJson("unite[create] failed: " + e.Message); }
            try { bld.Operation = NXOpen.Features.Feature.BooleanType.Unite; }
            catch (Exception e) { return ErrJson("unite[op] failed: " + e.Message); }
            try { bld.Target = target; }
            catch (Exception e) { return ErrJson("unite[target] failed: " + e.Message); }
            NXOpen.DisplayableObject[] objs = new NXOpen.DisplayableObject[tools.Count];
            for (int i = 0; i < tools.Count; i++) objs[i] = tools[i];
            try { bld.Tools.Add(objs); }
            catch (Exception e) { return ErrJson("unite[tools] failed: " + e.Message); }
            NXOpen.NXObject obj = null;
            try { obj = bld.Commit(); }
            catch (Exception e) { return ErrJson("unite[commit] failed: " + e.Message); }
            try { bld.Destroy(); } catch { }
            // tool bodies are consumed by the boolean; drop their stale ids
            foreach (string id in ids)
            {
                string t = id.Trim();
                if (t.Length > 0) _bodies.Remove(t);
            }
            return OkJson("body_id", parts[1], "united " + tools.Count + " tool body(ies)");
        }
        catch (Exception e)
        {
            return ErrJson("unite failed: " + e.Message);
        }
    }

    private static string FitView()
    {
        if (_session == null) _session = Session.GetSession();
        try { _session.Parts.Work.Views.WorkView.Fit(); } catch { }
        return OkJson("view fitted");
    }

    private static string ExportStep(string[] parts)
    {
        if (parts.Length < 2) return ErrJson("usage: nx_export_step <path>");
        if (_part == null) return ErrJson("no active part");
        string stepPath = parts[1];
        if (File.Exists(stepPath)) { try { File.Delete(stepPath); } catch { } }
        if (_session == null) _session = Session.GetSession();
        var creator = _session.DexManager.CreateStepCreator();
        try
        {
            creator.OutputFile = stepPath;
            creator.InputFile = _partPath;
            creator.ObjectTypes.Solids = true;
            creator.ExportAs = StepCreator.ExportAsOption.Ap214;
            string baseDir = Environment.GetEnvironmentVariable("UGII_BASE_DIR");
            if (!string.IsNullOrEmpty(baseDir))
            {
                string settings = Path.Combine(baseDir, "STEP214UG", "ugstep214.def");
                if (File.Exists(settings)) creator.SettingsFile = settings;
            }
            creator.Commit();
        }
        finally
        {
            try { creator.Destroy(); } catch { }
        }
        return OkJson("step", stepPath, "exported AP214");
    }

    // ---- composite acceptance part ------------------------------------
    private static string BuildPlateTest()
    {
        string ws = Environment.GetEnvironmentVariable("NX_MCP_WORKSPACE") ?? "";
        if (string.IsNullOrEmpty(ws))
            ws = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "NX_MCP_WORKSPACE");
        string prtPath = Path.Combine(ws, "loader_plate_demo.prt");
        string stepPath = Path.Combine(ws, "loader_plate_demo.step");

        // 1. part
        string cr = CreatePart(new string[] { "nx_create_part", prtPath });
        if (cr.Contains("\"ok\":false")) return cr;

        // 2. base plate 120x80x12
        string skB = CreateSketch(new string[] { "nx_create_sketch", "XY" });
        string skBId = ExtractId(skB, "sketch_id");
        SketchRectangle(new string[] { "nx_sketch_rectangle", skBId, "0", "0", "120", "80" });
        FinishSketch(new string[] { "nx_finish_sketch", skBId });
        string exB = Extrude(new string[] { "nx_extrude", skBId, "12" });
        if (exB.Contains("\"ok\":false")) return exB;
        string bodyId = ExtractId(exB, "body_id");

        // 3. boss 30x20 (circle dia 30, extrude 20 starting at z=12, unite)
        string skT = CreateSketch(new string[] { "nx_create_sketch", "XY" });
        string skTId = ExtractId(skT, "sketch_id");
        SketchCircle(new string[] { "nx_sketch_circle", skTId, "0", "0", "30" });
        FinishSketch(new string[] { "nx_finish_sketch", skTId });
        string exT = Extrude(new string[] { "nx_extrude", skTId, "20", "0", "12", "create", bodyId });
        if (exT.Contains("\"ok\":false")) return exT;
        string bodyId2 = ExtractId(exT, "body_id");

        // 4. center hole 12 through (32)
        string skH1 = CreateSketch(new string[] { "nx_create_sketch", "XY" });
        string skH1Id = ExtractId(skH1, "sketch_id");
        SketchCircle(new string[] { "nx_sketch_circle", skH1Id, "0", "0", "12" });
        FinishSketch(new string[] { "nx_finish_sketch", skH1Id });
        string exH1 = Extrude(new string[] { "nx_extrude", skH1Id, "32", "0", "0", "subtract", bodyId2 });
        if (exH1.Contains("\"ok\":false")) return exH1;

        // 5. two 10 holes at (+-40, 0) through plate (12)
        string skH2 = CreateSketch(new string[] { "nx_create_sketch", "XY" });
        string skH2Id = ExtractId(skH2, "sketch_id");
        SketchCircle(new string[] { "nx_sketch_circle", skH2Id, "-40", "0", "10" });
        SketchCircle(new string[] { "nx_sketch_circle", skH2Id, "40", "0", "10" });
        FinishSketch(new string[] { "nx_finish_sketch", skH2Id });
        string exH2 = Extrude(new string[] { "nx_extrude", skH2Id, "12", "0", "0", "subtract", bodyId2 });
        if (exH2.Contains("\"ok\":false")) return exH2;

        // 6. slot 40x12: middle rect 28x12 + two 12 circles at (+-14,0)
        string skS = CreateSketch(new string[] { "nx_create_sketch", "XY" });
        string skSId = ExtractId(skS, "sketch_id");
        SketchRectangle(new string[] { "nx_sketch_rectangle", skSId, "0", "0", "28", "12" });
        SketchCircle(new string[] { "nx_sketch_circle", skSId, "-14", "0", "12" });
        SketchCircle(new string[] { "nx_sketch_circle", skSId, "14", "0", "12" });
        FinishSketch(new string[] { "nx_finish_sketch", skSId });
        string exS = Extrude(new string[] { "nx_extrude", skSId, "12", "0", "0", "subtract", bodyId2 });
        if (exS.Contains("\"ok\":false")) return exS;

        // 7. blend R8 (tolerant all edges), 8. blend R3, 9. chamfer C2
        int b8 = 0, b3 = 0, c2 = 0;
        try { b8 = BlendChamfer(_bodies[bodyId2], 8.0, null, true); } catch { }
        try { b3 = BlendChamfer(_bodies[bodyId2], 3.0, null, true); } catch { }
        try { c2 = BlendChamfer(_bodies[bodyId2], 2.0, null, false); } catch { }

        // 10. save + export
        SavePart();
        string exStep = ExportStep(new string[] { "nx_export_step", stepPath });

        return "{\"ok\":true,\"result\":\"plate test built\"," +
               "\"part\":\"" + Esc(prtPath) + "\"," +
               "\"step\":\"" + Esc(stepPath) + "\"," +
               "\"blend8\":" + b8 + ",\"blend3\":" + b3 + ",\"chamfer2\":" + c2 + "}";
    }

    // ---- helpers -------------------------------------------------------
    private static Sketch GetSketch(string id)
    {
        Sketch sk;
        if (!_sketches.TryGetValue(id, out sk)) throw new Exception("unknown sketch_id: " + id);
        return sk;
    }

    private static Body GetBody(string id)
    {
        Body b;
        if (!_bodies.TryGetValue(id, out b)) throw new Exception("unknown body_id: " + id);
        return b;
    }

    private static double D(string s)
    {
        return double.Parse(s, CultureInfo.InvariantCulture);
    }

    private static int[] ParseIndices(string csv)
    {
        string[] t = csv.Split(',');
        int[] r = new int[t.Length];
        for (int i = 0; i < t.Length; i++) r[i] = int.Parse(t[i], CultureInfo.InvariantCulture);
        return r;
    }

    private static string ExtractId(string json, string key)
    {
        string needle = "\"" + key + "\":\"";
        int i = json.IndexOf(needle, StringComparison.Ordinal);
        if (i < 0) throw new Exception("missing " + key + " in " + json);
        int s = i + needle.Length;
        int e = json.IndexOf('"', s);
        return json.Substring(s, e - s);
    }

    private static string GetSketchPlaneName(string id)
    {
        string plane;
        if (_sketchPlanes.TryGetValue(id, out plane)) return plane;
        return "XY";
    }

    private static Point3d SketchPoint(string sketchId, double u, double v)
    {
        return SketchPointForPlane(GetSketchPlaneName(sketchId), u, v);
    }

    private static Point3d SketchPointForPlane(string plane, double u, double v)
    {
        switch (plane)
        {
            case "XZ": return new Point3d(u, 0.0, v);
            case "YZ": return new Point3d(0.0, u, v);
            default: return new Point3d(u, v, 0.0);
        }
    }

    private static Vector3d SketchVectorForPlane(string plane, double u, double v)
    {
        switch (plane)
        {
            case "XZ": return new Vector3d(u, 0.0, v);
            case "YZ": return new Vector3d(0.0, u, v);
            default: return new Vector3d(u, v, 0.0);
        }
    }

    private static Vector3d SketchPlaneNormal(string plane)
    {
        // Normals are right-handed with the local (u,v) mapping above.
        switch (plane)
        {
            case "XZ": return new Vector3d(0.0, -1.0, 0.0);
            case "YZ": return new Vector3d(1.0, 0.0, 0.0);
            default: return new Vector3d(0.0, 0.0, 1.0);
        }
    }

    private static Vector3d SketchExtrudeAxis(string plane)
    {
        // Stable user-facing default: positive global axis for reverse=false.
        switch (plane)
        {
            case "XZ": return new Vector3d(0.0, 1.0, 0.0);
            case "YZ": return new Vector3d(1.0, 0.0, 0.0);
            default: return new Vector3d(0.0, 0.0, 1.0);
        }
    }

    private static void AddLine(Sketch sk, string sketchId, double x1, double y1, double x2, double y2)
    {
        var curve = _part.Curves.CreateLine(
            SketchPoint(sketchId, x1, y1),
            SketchPoint(sketchId, x2, y2));
        sk.AddGeometry(curve, Sketch.InferConstraintsOption.InferNoConstraints);
    }

    private static void ExtrudeSketchCore(Sketch sk, double startZ, double endZ,
        BooleanOperation.BooleanType bt, Body target)
    {
        var section = _part.Sections.CreateSection();
        var rule = _part.ScRuleFactory.CreateRuleCurveFeature(new Feature[] { sk.Feature });
        section.AddToSection(
            new SelectionIntentRule[] { rule }, null, null, null,
            new Point3d(0.0, 0.0, 0.0), Section.Mode.Create, false);
        var dir = _part.Directions.CreateDirection(
            new Point3d(0.0, 0.0, 0.0), new Vector3d(0.0, 0.0, 1.0),
            SmartObject.UpdateOption.WithinModeling);
        var b = _part.Features.CreateExtrudeBuilder(null);
        try
        {
            b.Section = section;
            b.Direction = dir;
            b.Limits.StartExtend.Value.RightHandSide = startZ.ToString("0.###", CultureInfo.InvariantCulture);
            b.Limits.EndExtend.Value.RightHandSide = endZ.ToString("0.###", CultureInfo.InvariantCulture);
            b.BooleanOperation.Type = bt;
            if (target != null) b.BooleanOperation.SetTargetBodies(new Body[] { target });
            b.AllowSelfIntersectingSection(true);
            b.CommitFeature();
        }
        finally
        {
            b.Destroy();
        }
    }

    private static int BlendChamfer(Body body, double value, int[] indices, bool isBlend)
    {
        Edge[] edges = body.GetEdges();
        int done = 0;
        for (int i = 0; i < edges.Length; i++)
        {
            if (indices != null && Array.IndexOf(indices, i) < 0) continue;
            try
            {
                var col = _part.ScCollectors.CreateCollector();
                var rule = _part.ScRuleFactory.CreateRuleEdgeDumb(new Edge[] { edges[i] });
                col.ReplaceRules(new SelectionIntentRule[] { rule }, false);
                if (isBlend)
                {
                    var b = _part.Features.CreateEdgeBlendBuilder(null);
                    try
                    {
                        b.AddChainset(col, value.ToString("0.###", CultureInfo.InvariantCulture));
                        b.CommitFeature();
                    }
                    finally { b.Destroy(); }
                }
                else
                {
                    var b = _part.Features.CreateChamferBuilder(null);
                    try
                    {
                        b.SmartCollector = col;
                        b.FirstOffset = value.ToString("0.###", CultureInfo.InvariantCulture);
                        b.Option = ChamferBuilder.ChamferOption.SymmetricOffsets;
                        b.CommitFeature();
                    }
                    finally { b.Destroy(); }
                }
                done++;
            }
            catch { }
        }
        return done;
    }

    // ---- legacy create_block (kept) ------------------------------------
    private static string CreateBlock(string[] parts)
    {
        if (parts.Length < 4) return ErrJson("usage: create_block W H D");
        double w = D(parts[1]), h = D(parts[2]), d = D(parts[3]);
        string ws = Environment.GetEnvironmentVariable("NX_MCP_WORKSPACE") ?? "";
        string partName = string.IsNullOrEmpty(ws)
            ? "nx_mcp_block.prt"
            : Path.Combine(ws, "nx_mcp_block.prt");
        string cr = CreatePart(new string[] { "nx_create_part", partName });
        if (cr.Contains("\"ok\":false")) return cr;

        string sk = CreateSketch(new string[] { "nx_create_sketch", "XY" });
        string sid = ExtractId(sk, "sketch_id");
        SketchRectangle(new string[] { "nx_sketch_rectangle", sid, "0", "0", w.ToString("0.###", CultureInfo.InvariantCulture), h.ToString("0.###", CultureInfo.InvariantCulture) });
        FinishSketch(new string[] { "nx_finish_sketch", sid });
        string ex = Extrude(new string[] { "nx_extrude", sid, d.ToString("0.###", CultureInfo.InvariantCulture) });
        SavePart();
        return OkJson("block " + w + "x" + h + "x" + d + " saved=" + partName);
    }

    // ---- phase-2: remaining certified tools ----------------------------
    private static string OpenPart(string[] parts)
    {
        if (parts.Length < 2) return ErrJson("usage: nx_open_part <path>");
        string path = parts[1];
        if (!File.Exists(path)) return ErrJson("part file not found: " + path);
        if (_session == null) _session = Session.GetSession();
        NXOpen.PartLoadStatus status = null;
        try
        {
            var opened = _session.Parts.OpenBaseDisplay(path, out status);
            NXOpen.Part part = opened as NXOpen.Part;
            if (part == null) part = _session.Parts.Work;
            _part = part;
            _partPath = part.FullPath;
            ResetTaskState();
            // register existing bodies so GetBody works after nx_open_part
            try
            {
                foreach (Body b in _part.Bodies.ToArray())
                {
                    string bid = "";
                    try { bid = b.Name; } catch { }
                    if (string.IsNullOrEmpty(bid)) { try { bid = b.JournalIdentifier; } catch { } }
                    if (string.IsNullOrEmpty(bid)) bid = "BODY_" + (++_bodyCounter);
                    _bodies[bid] = b;
                }
            }
            catch { }
            return OkJson("part", _partPath, "opened");
        }
        finally
        {
            if (status != null) { try { status.Dispose(); } catch { } }
        }
    }

    private static string ClosePart(string[] parts)
    {
        bool save = parts.Length < 2 || parts[1] != "0";
        if (_part != null && save) SavePart();
        if (_part != null)
        {
            _part.Close(NXOpen.BasePart.CloseWholeTree.False,
                        NXOpen.BasePart.CloseModified.CloseModified, null);
            _part = null;
            _partPath = null;
        }
        ResetTaskState();
        return OkJson("closed part");
    }

    private static string ListObjects(string collection, string kind)
    {
        if (_part == null) return ErrJson("no active part");
        var names = new List<string>();
        try
        {
            if (collection == "Sketches")
            {
                foreach (NXOpen.Sketch o in _part.Sketches)
                {
                    string n = "";
                    try { n = o.Name; } catch { }
                    if (string.IsNullOrEmpty(n)) { try { n = o.JournalIdentifier; } catch { } }
                    names.Add(n);
                }
            }
            else if (collection == "Bodies")
            {
                foreach (NXOpen.Body o in _part.Bodies)
                {
                    string n = "";
                    try { n = o.Name; } catch { }
                    if (string.IsNullOrEmpty(n)) { try { n = o.JournalIdentifier; } catch { } }
                    names.Add(n);
                }
            }
            else if (collection == "Features")
            {
                foreach (NXOpen.Features.Feature o in _part.Features)
                {
                    string n = "";
                    try { n = o.GetFeatureName(); } catch { }
                    if (string.IsNullOrEmpty(n)) { try { n = o.JournalIdentifier; } catch { } }
                    names.Add(n);
                }
            }
        }
        catch { }
        var sb = new System.Text.StringBuilder();
        sb.Append("{\"ok\":true,\"objects\":[");
        for (int i = 0; i < names.Count; i++)
        {
            if (i > 0) sb.Append(",");
            sb.Append("\"").Append(Esc(names[i])).Append("\"");
        }
        sb.Append("],\"message\":\"Found ").Append(names.Count).Append(" ").Append(kind).Append("(s).\"}");
        return sb.ToString();
    }

    private static string Undo()
    {
        if (_session == null) _session = Session.GetSession();
        try
        {
            _session.UndoToLastVisibleMark();
            return OkJson("undone to last visible mark");
        }
        catch
        {
            // no visible undo mark (e.g. right after startup): no-op success,
            // matching the Python bridge behaviour of reporting no change
            return OkJson("nothing to undo");
        }
    }
}
