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
//   nx_mass_properties [density_kg_m3] [accuracy] [per=1]  (体积/表面积/重心/质量，工艺常用；per=1 逐体返回)
//   nx_measure_angle <body_id> <edge1> <edge2>      (两直线边夹角，装夹/工序定位常用)
//   nx_create_section_view <view> <x1> <y1> <x2> <y2> <px> <py> [scale=f] [arrow=dx,dy] [step=dx,dy] [name=t]
//                                                   (简单剖视图，UFUN CreateSimpleSxview，坐标均为图纸坐标；
//                                                    step=剖切线方向 arrow=视向，二者平行则 NX 报零矢量错误)
//   nx_add_section_segment <section_view> <x1> <y1> <x2> <y2>  (向剖切线追加割段=阶梯剖，
//                                                   AskSxlineOfSxview + AddSxlineSxseg，NX 自动生成转折)
//   nx_create_auxiliary_view <parent_view> <hx1> <hy1> <hx2> <hy2> <px> <py>  (辅助视图，试验性)
//   nx_delete_view <view_id>                        (删除图纸视图，GTAC 路线 UF_OBJ_delete_object)
//   nx_list_assembly                                (装配树拍平 -> BOM 行，工艺路线表数据源)
//   nx_create_id_symbol <type> <x> <y> [size=k] [lower=t] [upper text...]  (球标/ID 符号，
//                                                   装配工艺零件序号气泡，type 见 CreateIdSymbol)
//   nx_create_datum_symbol <letter> <x> <y>        (基准符号 GD&T，GB/T 17851 基准字母框，
//                                                   GdtDatumCollection 路线，坐标为图纸坐标)
//   nx_export_pdf <path> [ALL|sheet]  (ALL = 全部图纸页导出到一个多页 PDF)
//   nx_build_plate_test           (single-command acceptance part)
//   nx_create_wave_link <src_master> <target> [body_index] [assoc=1] [x=..] [y=..] [z=..]   (WAVE 链接：源件为装配根，目标件作下游组件，固定时间戳记/提取几何，可选摆放偏移)
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
using NXOpen.Drawings;
using NXOpen.Annotations;

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
            string ws = null;
            try { ws = Environment.GetEnvironmentVariable("NX_MCP_WORKSPACE"); } catch { }
            if (string.IsNullOrEmpty(ws))
            {
                ws = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                    "NX_MCP_WORKSPACE");
            }
            try { Directory.CreateDirectory(ws); } catch { }
            return Path.Combine(ws, "nx_mcp_loader.log");
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
            // ---- drafting (2D drawing) ---------------------------------
            // Ported from CAD-Agent-Hub's proven NXOpen sequences; every call
            // below was verified present in the NX 12 NXOpen.xml member list.
            case "nx_create_drawing_sheet":
                return CreateDrawingSheet(parts);
            case "nx_create_projected_view":
                return CreateProjectedView(parts);
            case "nx_list_drawing_sheets":
                return ListDrawingSheets(parts);
            case "nx_list_drafting_views":
                return ListDraftingViews(parts);
            case "nx_list_dimensions":
                return ListDimensions(parts);
            case "nx_list_annotations":
                return ListAnnotations(parts);
            case "nx_create_note":
                return CreateNote(parts);
            case "nx_create_diameter_dimension":
                return CreateDiameterDimension(parts);
            case "nx_create_radius_dimension":
                return CreateRadiusDimension(parts);
            case "nx_delete_annotation":
                return DeleteAnnotation(parts);
            case "nx_set_dimension_value":
                return SetDimensionValue(parts);
            case "nx_export_pdf":
                return ExportPdf(parts);
            case "nx_measure_distance":
                return MeasureDistance(parts);
            case "nx_measure_angle":
                return MeasureAngle(parts);
            case "nx_mass_properties":
                return MassProperties(parts);
            case "nx_create_centerline":
                return CreateCenterline(parts);
            case "nx_create_roughness":
                return CreateRoughness(parts);
            case "nx_create_detail_view":
                return CreateDetailView(parts);
            case "nx_create_title_block":
                return CreateTitleBlock(parts);
            case "nx_set_attribute":
                return SetAttributeCmd(parts);
            case "nx_list_attributes":
                return ListAttributes(parts);
            case "nx_create_sheet_border":
                return CreateSheetBorder(parts);
            case "nx_export_dwg":
                return ExportDwg(parts);
            // ---- drafting: view update + dimension annotation ------------
            case "nx_update_views":
                return UpdateViewsCmd();
            case "nx_create_dimension":
                return CreateDimension(parts);
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
            case "nx_read_sketch":
                return ReadSketch(parts);
            case "nx_undo":
                return Undo();
            case "nx_build_plate_test":
                return BuildPlateTest();
            case "create_block":
                return CreateBlock(parts);
            case "nx_create_id_symbol":
                return CreateIdSymbol(parts);
            case "nx_create_datum_symbol":
                return CreateDatumSymbol(parts);
            case "nx_create_wave_link":
                return CreateWaveLink(parts);
            case "nx_create_section_view":
                return CreateSectionView(parts);
            case "nx_add_section_segment":
                return AddSectionSegment(parts);
            case "nx_create_auxiliary_view":
                return CreateAuxiliaryView(parts);
            case "nx_delete_view":
                return DeleteViewCmd(parts);
            case "nx_list_assembly":
                return ListAssembly(parts);
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
                return "{\"ok\":true,\"ready\":true,\"loader\":\"nx_mcp_loader\",\"nx\":\"" + Esc(NxVersion()) + "\"," +
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

            return "{\"ok\":true,\"ready\":true,\"loader\":\"nx_mcp_loader\",\"nx\":\"" + Esc(NxVersion()) + "\"," +
                   "\"part\":\"" + Esc(partName) + "\",\"sketches\":" + _sketches.Count +
                   ",\"bodies\":" + _bodies.Count + "}";
        }
        catch (Exception e)
        {
            Log("status ERR: " + e.Message);
            return ErrJson("status failed: " + e.Message);
        }
    }

    /// <summary>
    /// [NX12 port] Report the ACTUAL running NX version instead of the hardcoded
    /// "2506" marketing string the upstream project compiled in. Upstream only ever
    /// validated on NX 2506, so clients would otherwise be told the wrong version.
    /// GetEnvironmentVariableValue exists since NX7.5.0, so it is safe on NX 12.
    /// </summary>
    private static string NxVersion()
    {
        try
        {
            if (_session != null)
            {
                string v = _session.GetEnvironmentVariableValue("UGII_VERSION");
                if (!string.IsNullOrEmpty(v)) return v.Trim();
            }
        }
        catch { }
        try
        {
            string v2 = Environment.GetEnvironmentVariable("UGII_VERSION");
            if (!string.IsNullOrEmpty(v2)) return v2.Trim();
        }
        catch { }
        return "unknown";
    }

    // ---- drafting (2D drawing) ---------------------------------------
    // Ported from CAD-Agent-Hub (MCP/UG/nx_remote_ops.py :: _op_create_drawing_sheet
    // / _op_create_projected_view). Those sequences were validated by that project;
    // the exact members were then confirmed against the NX 12 NXOpen.xml.
    // Every NXOpen call is guarded so a failure returns JSON instead of killing NX.

    private static DrawingSheet CurrentSheet()
    {
        if (_part == null) throw new Exception("no active part; run nx_create_part first");
        DrawingSheet s = _part.DrawingSheets.CurrentDrawingSheet;
        if (s == null) throw new Exception("no drawing sheet is currently open");
        return s;
    }

    private static DrawingSheet.StandardSheetSize ParseSheetSize(string token)
    {
        switch (token.Trim().ToUpperInvariant())
        {
            case "A0": return DrawingSheet.StandardSheetSize.A0;
            case "A1": return DrawingSheet.StandardSheetSize.A1;
            case "A2": return DrawingSheet.StandardSheetSize.A2;
            case "A3": return DrawingSheet.StandardSheetSize.A3;
            case "A4": return DrawingSheet.StandardSheetSize.A4;
            default: throw new Exception("size must be A0/A1/A2/A3/A4, got '" + token + "'");
        }
    }

    // nx_create_drawing_sheet <name> [size] [first|third] [scale] [model_view]
    private static string CreateDrawingSheet(string[] parts)
    {
        if (parts.Length < 2)
            return ErrJson("usage: nx_create_drawing_sheet <name> [A0..A4] [first|third] [scale] [model_view]");
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part; run nx_create_part first");

            string name = parts[1];
            string sizeToken = parts.Length > 2 ? parts[2] : "A4";
            DrawingSheet.StandardSheetSize size = ParseSheetSize(sizeToken);

            string projToken = parts.Length > 3 ? parts[3].Trim().ToLowerInvariant() : "first";
            DrawingSheet.ProjectionAngleType proj =
                (projToken == "third") ? DrawingSheet.ProjectionAngleType.ThirdAngle
                                       : DrawingSheet.ProjectionAngleType.FirstAngle;
            if (projToken != "first" && projToken != "third")
                return ErrJson("projection must be 'first' or 'third'");

            double numerator = parts.Length > 4 ? D(parts[4]) : 1.0;
            if (numerator <= 0) return ErrJson("scale must be > 0");
            double denominator = 1.0;

            // [NX12 port] DrawingSheetCollection.InsertSheet is deprecated exactly in
            // NX12.0.0; use the recommended DraftingDrawingSheetCollection instead.
            // The InsertSheet signatures are identical, so this is a drop-in swap.
            DrawingSheet sheet = _part.DraftingDrawingSheets.InsertSheet(name, size, numerator, denominator, proj);
            sheet.Open();

            // Place a base view at roughly the left-of-centre of the sheet, mirroring
            // what CAD-Agent-Hub does with an explicit A4 coordinate.
            string baseNote = "";
            try
            {
                string mvName = parts.Length > 5 ? parts[5].Trim() : "Top";
                ModelingView mv;
                try { mv = _part.ModelingViews.FindObject(mvName); }
                catch { mv = _part.ModelingViews.WorkView; }

                double sx, sy;
                try { sx = sheet.Length; sy = sheet.Height; }
                catch { sx = 297.0; sy = 210.0; }
                if (sx <= 0 || sy <= 0) { sx = 297.0; sy = 210.0; }

                DraftingView bv = sheet.SheetDraftingViews.CreateBaseView(
                    mv, new Point3d(sx * 0.42, sy * 0.5, 0.0), numerator / denominator, false);
                try { baseNote = " base=" + bv.JournalIdentifier; } catch { }
            }
            catch (Exception e2)
            {
                baseNote = " base_view_failed: " + e2.Message;
            }

            int count = 0;
            try { count = sheet.GetDraftingViews().Length; } catch { }

            try { ForceUpdateSheet(sheet); } catch { }

            return OkJson("sheet", sheet.Name, "created size=" + sizeToken.ToUpperInvariant()
                + " views=" + count + baseNote);
        }
        catch (Exception e)
        {
            return ErrJson("create_drawing_sheet failed: " + e.Message);
        }
    }

    // nx_create_projected_view <x> <y> [parent_view_id] [view_name]
    private static string CreateProjectedView(string[] parts)
    {
        if (parts.Length < 3)
            return ErrJson("usage: nx_create_projected_view <x> <y> [parent_view_id] [view_name]");
        try
        {
            DrawingSheet sheet = CurrentSheet();
            DraftingView[] views = sheet.GetDraftingViews();
            if (views == null || views.Length == 0) return ErrJson("sheet has no drafting views");

            string token = parts.Length > 3 ? parts[3].Trim() : "";
            DraftingView parent = views[0];
            if (!string.IsNullOrEmpty(token))
            {
                bool found = false;
                for (int i = 0; i < views.Length; i++)
                {
                    string nm = ""; try { nm = views[i].Name; } catch { }
                    string ji = ""; try { ji = views[i].JournalIdentifier; } catch { }
                    if (token == i.ToString() || token == nm || token == ji)
                    {
                        parent = views[i];
                        found = true;
                        break;
                    }
                }
                if (!found) return ErrJson("parent_view_id did not match a drafting view: " + token);
            }

            double x = D(parts[1]);
            double y = D(parts[2]);
            DraftingView pv = sheet.SheetDraftingViews.CreateProjectedView(parent, new Point3d(x, y, 0.0));

            if (parts.Length > 4 && !string.IsNullOrEmpty(parts[4].Trim()))
            {
                try { pv.SetName(parts[4].Trim()); } catch { }
            }
            try { ForceUpdateSheet(sheet); } catch { }

            string id = ""; try { id = pv.JournalIdentifier; } catch { }
            int count = 0;
            try { count = sheet.GetDraftingViews().Length; } catch { }
            return OkJson("view", id, "projected view created views=" + count);
        }
        catch (Exception e)
        {
            return ErrJson("create_projected_view failed: " + e.Message);
        }
    }

    // nx_list_drawing_sheets
    private static string ListDrawingSheets(string[] parts)
    {
        try
        {
            if (_part == null) return ErrJson("no active part");
            var names = new List<string>();
            foreach (DrawingSheet s in _part.DrawingSheets.ToArray())
            {
                string ji = ""; try { ji = s.JournalIdentifier; } catch { }
                names.Add(s.Name + " (" + ji + ")");
            }
            return OkJsonList("sheets", names, "found " + names.Count + " sheet(s)");
        }
        catch (Exception e)
        {
            return ErrJson("list_drawing_sheets failed: " + e.Message);
        }
    }

    // nx_list_drafting_views
    private static string ListDraftingViews(string[] parts)
    {
        try
        {
            DrawingSheet sheet = GetSheetToUse();
            var ids = new List<string>();
            foreach (DraftingView v in sheet.GetDraftingViews())
            {
                string nm = ""; try { nm = v.Name; } catch { }
                string ji = ""; try { ji = v.JournalIdentifier; } catch { }
                bool ood = false; try { ood = v.IsOutOfDate; } catch { }
                // format: journalIdentifier | name | out_of_date(0/1)
                ids.Add(ji + "|" + nm + "|" + (ood ? "1" : "0"));
            }
            return OkJsonList("views", ids, "found " + ids.Count + " view(s)");
        }
        catch (Exception e)
        {
            return ErrJson("list_drafting_views failed: " + e.Message);
        }
    }

    // nx_update_views  — force every drafting view on the current sheet to a
    // fully-up-to-date state. Previously CreateProjectedView only called
    // RefreshCurrentSheet(), which leaves the views flagged out-of-date (the
    // "clock" icon in the Part Navigator). A real update is what clears it.
    private static string UpdateViewsCmd()
    {
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part");
            DrawingSheet sheet = GetSheetToUse();
            ForceUpdateSheet(sheet);
            int n = 0;
            try { n = sheet.GetDraftingViews().Length; } catch { }
            return OkJson("updated", n.ToString(), "updated " + n + " view(s) on sheet " + sheet.Name);
        }
        catch (Exception e)
        {
            return ErrJson("update_views failed: " + e.Message);
        }
    }

    // Performs a genuine view update (not just a sheet refresh) and then
    // refreshes the sheet. Used after every drafting operation so the Part
    // Navigator never shows an out-of-date / clock status.
    private static void ForceUpdateSheet(DrawingSheet sheet)
    {
        Session.UndoMarkId mark = _session.SetUndoMark(Session.MarkVisibility.Invisible, "MCP Force Update Views");
        foreach (DraftingView v in sheet.GetDraftingViews())
        {
            try { v.Update(); }
            catch (Exception e) { Log("view update warn: " + e.Message); }
        }
        try { _session.UpdateManager.DoUpdate(mark); } catch { }
        try { _part.DrawingSheets.RefreshCurrentSheet(); } catch { }
    }

    private static DraftingView ResolveView(DrawingSheet sheet, string token)
    {
        DraftingView[] views = sheet.GetDraftingViews();
        if (views == null || views.Length == 0) throw new Exception("sheet has no drafting views");
        if (string.IsNullOrEmpty(token)) return views[0];
        foreach (DraftingView v in views)
        {
            string nm = ""; try { nm = v.Name; } catch { }
            string ji = ""; try { ji = v.JournalIdentifier; } catch { }
            string tg = ""; try { tg = v.Tag.ToString(); } catch { }
            if (token == nm || token == ji || token == tg) return v;
        }
        Log("ResolveView: '" + token + "' did not match; using first view " + views[0].Name);
        return views[0];
    }

    // Returns a pick point that lies on the geometry (midpoint of an edge, the
    // coordinate of a point). Used as the dimension associativity PickPoint so
    // the dimension stays associative to real model geometry.
    private static Point3d MidpointOfObject(NXOpen.TaggedObject obj)
    {
        try
        {
            NXOpen.Edge e = obj as NXOpen.Edge;
            if (e != null)
            {
                Point3d s, en;
                e.GetVertices(out s, out en);
                return new Point3d((s.X + en.X) / 2.0, (s.Y + en.Y) / 2.0, (s.Z + en.Z) / 2.0);
            }
            NXOpen.Point pt = obj as NXOpen.Point;
            if (pt != null)
            {
                Point3d c;
                try { c = pt.Coordinates; return c; } catch { }
            }
        }
        catch (Exception e) { Log("MidpointOfObject warn: " + e.Message); }
        return new Point3d(0.0, 0.0, 0.0);
    }

    // nx_create_dimension <view_id> <body_id> <first_edge_index> <second_edge_index>
    //   <pos_x> <pos_y> [measurement=horizontal|vertical|parallel] [upper=0.1] [lower=-0.1] [dim_name]
    // Creates an associative linear dimension between two model edges (selected
    // by their 0-based index from nx_list_edges, on the given body) inside the
    // specified drafting view, then applies a bilateral (two-line) tolerance.
    // Default tolerance is ±0.1 mm, matching the user's "标注尺寸公差±0.1".
    // Returns a usable drawing sheet: the current one if set, otherwise the
    // first sheet that has drafting views (and opens it so it becomes current).
    private static DrawingSheet GetSheetToUse()
    {
        DrawingSheet s = null;
        try { s = _part.DrawingSheets.CurrentDrawingSheet; } catch { }
        if (s != null) return s;
        foreach (DrawingSheet sh in _part.DrawingSheets)
        {
            try { if (sh.GetDraftingViews().Length > 0) { sh.Open(); return sh; } } catch { }
        }
        throw new Exception("no drawing sheet available");
    }

    // Resolves a drafting view by token across ALL sheets (not just the current
    // one) and returns its owning sheet, so dimensioning works even when no
    // sheet is flagged "current" in the session.
    private static DraftingView ResolveViewAnySheet(string token, out DrawingSheet sheet)
    {
        sheet = null;
        try
        {
            DrawingSheet cur = _part.DrawingSheets.CurrentDrawingSheet;
            if (cur != null)
            {
                DraftingView v = TryResolveInSheet(cur, token);
                if (v != null) { sheet = cur; return v; }
            }
        }
        catch { }
        foreach (DrawingSheet sh in _part.DrawingSheets)
        {
            DraftingView v = TryResolveInSheet(sh, token);
            if (v != null) { sheet = sh; return v; }
        }
        foreach (DrawingSheet sh in _part.DrawingSheets)
        {
            DraftingView[] vs = sh.GetDraftingViews();
            if (vs != null && vs.Length > 0) { sheet = sh; return vs[0]; }
        }
        throw new Exception("no drafting view found (token=" + token + ")");
    }

    private static DraftingView TryResolveInSheet(DrawingSheet sheet, string token)
    {
        DraftingView[] views = sheet.GetDraftingViews();
        if (views == null || views.Length == 0) return null;
        if (string.IsNullOrEmpty(token)) return views[0];
        foreach (DraftingView v in views)
        {
            string nm = ""; try { nm = v.Name; } catch { }
            string ji = ""; try { ji = v.JournalIdentifier; } catch { }
            string tg = ""; try { tg = v.Tag.ToString(); } catch { }
            if (token == nm || token == ji || token == tg) return v;
        }
        return null;
    }

    private static string CreateDimension(string[] parts)
    {
        if (parts.Length < 7)
            return ErrJson("usage: nx_create_dimension <view_id> <body_id> <first_edge_index> <second_edge_index> <pos_x> <pos_y> [horizontal|vertical|parallel] [upper=0.1] [lower=-0.1] [dim_name]");
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part; run nx_create_part first");

            DrawingSheet sheet;
            DraftingView view = ResolveViewAnySheet(parts[1].Trim(), out sheet);
            try { sheet.Open(); } catch { }

            Body body = ResolveBody(parts[2].Trim());
            Edge[] edges = body.GetEdges();
            int ei1 = int.Parse(parts[3], CultureInfo.InvariantCulture);
            int ei2 = int.Parse(parts[4], CultureInfo.InvariantCulture);
            if (ei1 < 0 || ei1 >= edges.Length) return ErrJson("first_edge_index out of range: " + ei1 + " (have " + edges.Length + ")");
            if (ei2 < 0 || ei2 >= edges.Length) return ErrJson("second_edge_index out of range: " + ei2 + " (have " + edges.Length + ")");
            Edge e1 = edges[ei1];
            Edge e2 = edges[ei2];

            double posx = D(parts[5]);
            double posy = D(parts[6]);

            string measurement = (parts.Length > 7 && !string.IsNullOrEmpty(parts[7].Trim()))
                ? parts[7].Trim().ToLowerInvariant() : "horizontal";
            double upper = (parts.Length > 8 && !string.IsNullOrEmpty(parts[8].Trim())) ? D(parts[8]) : 0.1;
            double lower = (parts.Length > 9 && !string.IsNullOrEmpty(parts[9].Trim())) ? D(parts[9]) : -0.1;
            string dimName = (parts.Length > 10 && !string.IsNullOrEmpty(parts[10].Trim()))
                ? parts[10].Trim() : "MCP_DIMENSION";

            NXOpen.Annotations.DimensionData ddd = _part.Annotations.NewDimensionData();
            NXOpen.Annotations.Associativity a1 = _part.Annotations.NewAssociativity();
            NXOpen.Annotations.Associativity a2 = _part.Annotations.NewAssociativity();

            a1.FirstObject = e1;
            a1.SecondObject = null;
            a1.ObjectView = view;
            a1.PickPoint = MidpointOfObject(e1);
            a1.PointOption = NXOpen.Annotations.AssociativityPointOption.OnCurve;
            a1.LineOption = NXOpen.Annotations.AssociativityLineOption.None;

            a2.FirstObject = e2;
            a2.SecondObject = null;
            a2.ObjectView = view;
            a2.PickPoint = MidpointOfObject(e2);
            a2.PointOption = NXOpen.Annotations.AssociativityPointOption.OnCurve;
            a2.LineOption = NXOpen.Annotations.AssociativityLineOption.None;

            ddd.SetAssociativity(1, new NXOpen.Annotations.Associativity[] { a1 });
            ddd.SetAssociativity(2, new NXOpen.Annotations.Associativity[] { a2 });

            Point3d origin = new Point3d(posx, posy, 0.0);
            NXOpen.Annotations.Dimension dim;
            switch (measurement)
            {
                case "vertical":
                    dim = _part.Dimensions.CreateVerticalDimension(ddd, origin);
                    break;
                case "parallel":
                    dim = _part.Dimensions.CreateParallelDimension(ddd, origin);
                    break;
                default:
                    dim = _part.Dimensions.CreateHorizontalDimension(ddd, origin);
                    break;
            }
            try { dim.SetName(dimName); } catch { }

            // Force 2-decimal NOMINAL display so mid-tolerance values (99.96, 80.05)
            // are not rounded to 100 / 80 by NX's default 0-decimal precision.
            // NX 12 exposes these directly on Dimension (no DimensionStyle API yet).
            try
            {
                dim.NominalDecimalPlaces = 2;
                dim.MetricNominalDecimalPlaces = 2;
            }
            catch (Exception ep) { Log("precision warn: " + ep.Message); }

            // Apply bilateral (two-line) tolerance. Best-effort: if the
            // dimension type does not support linear tolerance we skip it.
            try
            {
                NXOpen.Annotations.ILinearTolerance ltObj = (NXOpen.Annotations.ILinearTolerance)dim;
                NXOpen.Annotations.LinearTolerance lt = ltObj.GetTolerance();
                lt.ToleranceType = NXOpen.Annotations.ToleranceType.BilateralTwoLines;
                lt.SetUpperToleranceMm(new NXOpen.Annotations.Value(upper, null, 0));
                lt.SetLowerToleranceMm(new NXOpen.Annotations.Value(lower, null, 0));
                ltObj.SetTolerance(lt);
            }
            catch (Exception et) { Log("tolerance warn: " + et.Message); }

            // Tolerance decimals must be set AFTER the bilateral tolerance is
            // applied, otherwise the LinearTolerance assignment resets them to 0.
            try
            {
                dim.ToleranceDecimalPlaces = 1;
                dim.MetricToleranceDecimalPlaces = 1;
            }
            catch (Exception ep) { Log("tol precision warn: " + ep.Message); }

            try { ForceUpdateSheet(sheet); } catch { }

            string jid = ""; try { jid = dim.JournalIdentifier; } catch { }
            double computed = 0.0; try { computed = dim.ComputedSize; } catch { }
            return OkJson("dimension", jid,
                "created " + measurement + " dim name=" + dimName
                + " value=" + F(computed) + " tol=+" + F(upper) + "/" + F(lower));
        }
        catch (Exception e)
        {
            return ErrJson("create_dimension failed: " + e.Message);
        }
    }

    // Resolves a body by its MCP id (from _bodies) or falls back to the first
    // solid body in the work part — matching the fallback used by nx_list_edges.
    private static Body ResolveBody(string id)
    {
        if (!string.IsNullOrEmpty(id))
        {
            Body b;
            if (_bodies.TryGetValue(id, out b)) return b;
        }
        Body[] all = _part.Bodies.ToArray();
        foreach (Body b in all) if (b.IsSolidBody) return b;
        if (all.Length > 0) return all[0];
        throw new Exception("no body in work part");
    }

    // nx_list_dimensions
    // Read-back of every dimension in the part: measured size + bilateral tolerance.
    // This closes the write/verify loop and lets an agent "read" an existing 2D
    // drawing (values and tolerances), not just create one.
    // Members confirmed present in NX 12: Part.Dimensions, Dimension.ComputedSize,
    // Dimension.UpperMetricToleranceValue / LowerMetricToleranceValue, ToleranceType.
    private static string ListDimensions(string[] parts)
    {
        try
        {
            if (_part == null) return ErrJson("no active part");
            var items = new List<string>();
            foreach (NXOpen.Annotations.Dimension dim in _part.Dimensions.ToArray())
            {
                string ji = "";  try { ji = dim.JournalIdentifier; } catch { }
                string nm = "";  try { nm = dim.Name; } catch { }
                double size = 0; try { size = dim.ComputedSize; } catch { }
                double up = 0, lo = 0;
                try { up = dim.UpperMetricToleranceValue; lo = dim.LowerMetricToleranceValue; } catch { }
                string tt = "";
                try { tt = dim.ToleranceType.ToString(); } catch { }
                items.Add(
                    ji + "|" + nm +
                    "|size=" + size.ToString("0.###", CultureInfo.InvariantCulture) +
                    "|tol=" + up.ToString("0.###", CultureInfo.InvariantCulture) +
                    "/" + lo.ToString("0.###", CultureInfo.InvariantCulture) +
                    "|" + tt);
            }
            return OkJsonList("dimensions", items, "found " + items.Count + " dimension(s)");
        }
        catch (Exception e)
        {
            return ErrJson("list_dimensions failed: " + e.Message);
        }
    }

    // nx_create_note <pos_x> <pos_y> <text...>
    // Technical-requirement / process text on the current drawing sheet.
    // Text is everything after the position (spaces allowed); use '|' for line breaks.
    // Ported from CAD-Agent-Hub _op_create_drafting_note (validated sequence).
    private static string CreateNote(string[] parts)
    {
        if (parts.Length < 4)
            return ErrJson("usage: nx_create_note <pos_x> <pos_y> <text...> (use | for line breaks)");
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part; run nx_create_part first");

            double posx = D(parts[1]);
            double posy = D(parts[2]);

            // Text == all remaining tokens re-joined, so it may contain spaces.
            var sb = new StringBuilder();
            for (int i = 3; i < parts.Length; i++)
            {
                if (i > 3) sb.Append(" ");
                sb.Append(parts[i]);
            }
            string raw = sb.ToString().Trim();
            if (raw.Length == 0) return ErrJson("note text is empty");

            string[] lines = raw.Split('|');
            for (int i = 0; i < lines.Length; i++) lines[i] = lines[i].Trim();

            string jid = "";
            NXOpen.Annotations.DraftingNoteBuilder nb =
                // C# has no SimpleDraftingAid.Null (that is the Python spelling);
                // passing null creates a brand-new note, same as the other builders here.
                _part.Annotations.CreateDraftingNoteBuilder(null);
            try
            {
                nb.Text.TextBlock.SetText(lines);
                nb.Origin.OriginPoint = new Point3d(posx, posy, 0.0);
                nb.Origin.Anchor = NXOpen.Annotations.OriginBuilder.AlignmentPosition.TopLeft;
                NXOpen.Annotations.Note note = (NXOpen.Annotations.Note)nb.Commit();
                try { note.SetName("MCP_NOTE"); } catch { }
                try { jid = note.JournalIdentifier; } catch { }
            }
            finally { try { nb.Destroy(); } catch { } }

            try { _part.DrawingSheets.RefreshCurrentSheet(); } catch { }
            return OkJson("note", jid, "created note lines=" + lines.Length);
        }
        catch (Exception e)
        {
            return ErrJson("create_note failed: " + e.Message);
        }
    }

    // nx_create_diameter_dimension <view_id> <body_id> <edge_index> <pos_x> <pos_y> [upper] [lower] [name]
    // Needed for shafts: a linear dimension between two edges cannot express a diameter.
    private static string CreateDiameterDimension(string[] parts)
    {
        if (parts.Length < 6)
            return ErrJson("usage: nx_create_diameter_dimension <view_id> <body_id> <edge_index> <pos_x> <pos_y> [upper=0.1] [lower=-0.1] [name]");
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part; run nx_create_part first");

            DrawingSheet sheet;
            DraftingView view = ResolveViewAnySheet(parts[1].Trim(), out sheet);
            try { sheet.Open(); } catch { }

            Body body = ResolveBody(parts[2].Trim());
            Edge[] edges = body.GetEdges();
            int ei = int.Parse(parts[3], CultureInfo.InvariantCulture);
            if (ei < 0 || ei >= edges.Length)
                return ErrJson("edge_index out of range: " + ei + " (have " + edges.Length + ")");
            Edge e1 = edges[ei];

            double posx = D(parts[4]);
            double posy = D(parts[5]);
            double upper = (parts.Length > 6 && !string.IsNullOrEmpty(parts[6].Trim())) ? D(parts[6]) : 0.1;
            double lower = (parts.Length > 7 && !string.IsNullOrEmpty(parts[7].Trim())) ? D(parts[7]) : -0.1;
            string dimName = (parts.Length > 8 && !string.IsNullOrEmpty(parts[8].Trim()))
                ? parts[8].Trim() : "MCP_DIAMETER";

            NXOpen.Annotations.DimensionData ddd = _part.Annotations.NewDimensionData();
            NXOpen.Annotations.Associativity a1 = _part.Annotations.NewAssociativity();
            a1.FirstObject = e1;
            a1.SecondObject = null;
            a1.ObjectView = view;
            a1.PickPoint = MidpointOfObject(e1);
            a1.PointOption = NXOpen.Annotations.AssociativityPointOption.OnCurve;
            a1.LineOption = NXOpen.Annotations.AssociativityLineOption.None;
            ddd.SetAssociativity(1, new NXOpen.Annotations.Associativity[] { a1 });

            NXOpen.Annotations.Dimension dim =
                _part.Dimensions.CreateDiameterDimension(ddd, new Point3d(posx, posy, 0.0));
            try { dim.SetName(dimName); } catch { }

            // Force 2-decimal NOMINAL display so mid-tolerance values (99.96, 80.05)
            // are not rounded to 100 / 80 by NX's default 0-decimal precision.
            // NX 12 exposes these directly on Dimension (no DimensionStyle API yet).
            try
            {
                dim.NominalDecimalPlaces = 2;
                dim.MetricNominalDecimalPlaces = 2;
            }
            catch (Exception ep) { Log("precision warn: " + ep.Message); }

            try
            {
                NXOpen.Annotations.ILinearTolerance ltObj = (NXOpen.Annotations.ILinearTolerance)dim;
                NXOpen.Annotations.LinearTolerance lt = ltObj.GetTolerance();
                lt.ToleranceType = NXOpen.Annotations.ToleranceType.BilateralTwoLines;
                lt.SetUpperToleranceMm(new NXOpen.Annotations.Value(upper, null, 0));
                lt.SetLowerToleranceMm(new NXOpen.Annotations.Value(lower, null, 0));
                ltObj.SetTolerance(lt);
            }
            catch (Exception et) { Log("diameter tolerance warn: " + et.Message); }

            // Tolerance decimals must be set AFTER the bilateral tolerance is
            // applied, otherwise the LinearTolerance assignment resets them to 0.
            try
            {
                dim.ToleranceDecimalPlaces = 1;
                dim.MetricToleranceDecimalPlaces = 1;
            }
            catch (Exception ep) { Log("tol precision warn: " + ep.Message); }

            try { ForceUpdateSheet(sheet); } catch { }

            string jid = ""; try { jid = dim.JournalIdentifier; } catch { }
            double computed = 0.0; try { computed = dim.ComputedSize; } catch { }
            return OkJson("dimension", jid,
                "created diameter dim name=" + dimName
                + " value=" + F(computed) + " tol=+" + F(upper) + "/" + F(lower));
        }
        catch (Exception e)
        {
            return ErrJson("create_diameter_dimension failed: " + e.Message);
        }
    }

    // nx_create_radius_dimension <view_id> <body_id> <edge_index> <pos_x> <pos_y> [name]
    // For fillets and arcs; a linear dimension cannot express a radius.
    private static string CreateRadiusDimension(string[] parts)
    {
        if (parts.Length < 6)
            return ErrJson("usage: nx_create_radius_dimension <view_id> <body_id> <edge_index> <pos_x> <pos_y> [name]");
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part; run nx_create_part first");

            DrawingSheet sheet;
            DraftingView view = ResolveViewAnySheet(parts[1].Trim(), out sheet);
            try { sheet.Open(); } catch { }

            Body body = ResolveBody(parts[2].Trim());
            Edge[] edges = body.GetEdges();
            int ei = int.Parse(parts[3], CultureInfo.InvariantCulture);
            if (ei < 0 || ei >= edges.Length)
                return ErrJson("edge_index out of range: " + ei + " (have " + edges.Length + ")");
            Edge e1 = edges[ei];

            double posx = D(parts[4]);
            double posy = D(parts[5]);
            string dimName = (parts.Length > 6 && !string.IsNullOrEmpty(parts[6].Trim()))
                ? parts[6].Trim() : "MCP_RADIUS";

            NXOpen.Annotations.DimensionData ddd = _part.Annotations.NewDimensionData();
            NXOpen.Annotations.Associativity a1 = _part.Annotations.NewAssociativity();
            a1.FirstObject = e1;
            a1.SecondObject = null;
            a1.ObjectView = view;
            a1.PickPoint = MidpointOfObject(e1);
            a1.PointOption = NXOpen.Annotations.AssociativityPointOption.OnCurve;
            a1.LineOption = NXOpen.Annotations.AssociativityLineOption.None;
            ddd.SetAssociativity(1, new NXOpen.Annotations.Associativity[] { a1 });

            NXOpen.Annotations.Dimension dim = _part.Dimensions.CreateRadiusDimension(
                ddd, NXOpen.Annotations.RadiusDimensionType.ToCenter,
                new Point3d(posx, posy, 0.0));
            try { dim.SetName(dimName); } catch { }

            try { ForceUpdateSheet(sheet); } catch { }

            string jid = ""; try { jid = dim.JournalIdentifier; } catch { }
            double computed = 0.0; try { computed = dim.ComputedSize; } catch { }
            return OkJson("dimension", jid,
                "created radius dim name=" + dimName + " value=" + F(computed));
        }
        catch (Exception e)
        {
            return ErrJson("create_radius_dimension failed: " + e.Message);
        }
    }

    // nx_export_pdf <path.pdf> [sheet_name]
    // Exports the current display (or a chosen sheet) to PDF.
    // Entry point is PlotManager.CreatePrintPdfbuilder() - note the lowercase 'b'
    // in "Pdfbuilder"; searching for "PdfBuilder" finds nothing and makes PDF
    // export look impossible. Available since NX5.0.1, Windows only.
    private static string ExportPdf(string[] parts)
    {
        if (parts.Length < 2)
            return ErrJson("usage: nx_export_pdf <path.pdf> [sheet_name]");
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part; run nx_create_part first");

            string path = parts[1].Trim();
            string dir = null;
            try { dir = Path.GetDirectoryName(path); } catch { }
            if (!string.IsNullOrEmpty(dir))
            {
                try { Directory.CreateDirectory(dir); } catch { }
            }

            // The PDF builder exports the CURRENT DISPLAY. Manually this works because
            // opening a drawing sheet puts NX into the Drafting application; headless
            // we must switch explicitly, otherwise Commit() succeeds but writes nothing.
            try { _session.ApplicationSwitchImmediate("UG_APP_DRAFTING"); }
            catch (Exception ea) { Log("app switch warn: " + ea.Message); }

            // Resolve the sheet to export: sheet_name "ALL" exports every sheet of
            // the part into ONE multi-page PDF (SetSheets takes an array); by name
            // if given, else the current one.
            string want = (parts.Length > 2) ? parts[2].Trim() : "";
            List<NXObject> sheetsToPlot = new List<NXObject>();
            try
            {
                if (!string.IsNullOrEmpty(want) && want.Equals("ALL", StringComparison.OrdinalIgnoreCase))
                {
                    foreach (DrawingSheet s in _part.DrawingSheets.ToArray())
                    {
                        try { s.Open(); } catch { }
                        sheetsToPlot.Add(s);
                    }
                    if (sheetsToPlot.Count == 0) return ErrJson("part has no drawing sheets");
                }
                else
                {
                    DrawingSheet target = null;
                    if (!string.IsNullOrEmpty(want))
                    {
                        foreach (DrawingSheet s in _part.DrawingSheets.ToArray())
                        {
                            string nm = "";
                            try { nm = s.Name; } catch { }
                            if (nm == want) { target = s; break; }
                        }
                    }
                    if (target == null) target = _part.DrawingSheets.CurrentDrawingSheet;
                    if (target != null) { try { target.Open(); } catch { } }
                    if (target != null) sheetsToPlot.Add(target);
                }
            }
            catch { }

            // NX12's PrintPDFBuilder ignores multi-sheet SetSheets (0-page PDF,
            // SelectedSheets has no effect either), so "ALL" loops the sheets and
            // appends one page per Commit - no external PDF library needed.
            if (sheetsToPlot.Count > 1)
            {
                try { if (File.Exists(path)) File.Delete(path); } catch { }
            }
            for (int i = 0; i < sheetsToPlot.Count; i++)
            {
                NXOpen.PrintPDFBuilder pb = _part.PlotManager.CreatePrintPdfbuilder();
                try
                {
                    pb.Filename = path;
                    try { pb.Append = (i > 0); } catch { }
                    // PlotSourceBuilder.SetSheets(NXObject[]) is what actually
                    // selects the sheet to export. Without it Commit() succeeds
                    // but writes no file.
                    try { pb.SourceBuilder.SetSheets(new NXObject[] { sheetsToPlot[i] }); }
                    catch (Exception es) { Log("set sheets warn: " + es.Message); }
                    pb.Commit();
                }
                finally { try { pb.Destroy(); } catch { } }
            }

            bool exists = File.Exists(path);
            long size = 0;
            if (exists) { try { size = new FileInfo(path).Length; } catch { } }
            return OkJson("pdf", path,
                exists ? ("exported " + size + " bytes")
                       : "commit ok but file not found - check NX display state");
        }
        catch (Exception e)
        {
            return ErrJson("export_pdf failed: " + e.Message);
        }
    }

    // nx_measure_distance <body_id> <edge_index_1> <edge_index_2>
    // 3D QUERY (no annotation is created) - returns the minimum distance between two
    // edges of a body so an agent can verify geometry numerically.
    // The NXOpen Measure classes do not expose the measured value, so this uses UFUN
    // (NXOpen.UF.UFSession.Sf.FindMinimumDistance), which returns a real number.
    // Requires NXOpen.UF.dll, added to build.bat.
    private static string MeasureDistance(string[] parts)
    {
        if (parts.Length < 4)
            return ErrJson("usage: nx_measure_distance <body_id> <edge_index_1> <edge_index_2>");
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part; run nx_create_part first");

            Body body = ResolveBody(parts[1].Trim());
            Edge[] edges = body.GetEdges();
            int i1 = int.Parse(parts[2], CultureInfo.InvariantCulture);
            int i2 = int.Parse(parts[3], CultureInfo.InvariantCulture);
            if (i1 < 0 || i1 >= edges.Length)
                return ErrJson("edge_index_1 out of range: " + i1 + " (have " + edges.Length + ")");
            if (i2 < 0 || i2 >= edges.Length)
                return ErrJson("edge_index_2 out of range: " + i2 + " (have " + edges.Length + ")");

            Edge e1 = edges[i1];
            Edge e2 = edges[i2];

            NXOpen.UF.UFSession uf = NXOpen.UF.UFSession.GetUFSession();
            double dist = 0.0;
            double[] p1 = new double[3];
            double[] p2 = new double[3];
            // UFSf.FindMinimumDistance returned 0 with (0,0,0) points - it does not
            // work for edge-to-edge. UFModl.AskMinimumDist is the general one.
            uf.Modl.AskMinimumDist(e1.Tag, e2.Tag, 0, new double[3], 0, new double[3],
                                   out dist, p1, p2);

            return OkJson(
                "distance", dist.ToString("0.####", CultureInfo.InvariantCulture),
                "min distance=" + F(dist) + " mm  p1=(" + F(p1[0]) + "," + F(p1[1]) + "," + F(p1[2])
                + ")  p2=(" + F(p2[0]) + "," + F(p2[1]) + "," + F(p2[2]) + ")");
        }
        catch (Exception e)
        {
            return ErrJson("measure_distance failed: " + e.Message);
        }
    }

    // nx_mass_properties [density_kg_m3] [accuracy]
    // 3D QUERY - volume / surface area / centre of gravity / mass of every solid
    // body in the work part. Entry: MeasureManager.NewMassProperties (the NX12
    // .NET wrapper does NOT expose UF_MODL_ask_mass_properties_3, verified via
    // reflection over NXOpen.UF.dll).
    // Mass = measured volume x caller-supplied density (default steel 7830 kg/m^3):
    // MeasureBodies.Mass follows the NX part density default, which is not what a
    // process engineer wants when quoting a specific material.
    // Units pinned to kg / mm / mm^3 through UnitCollection lookups.
    private static string MassProperties(string[] parts)
    {
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part; run nx_create_part first");

            double density = 7830.0; // kg/m^3, steel - the everyday process default
            double accuracy = 0.99;
            if (parts.Length > 1 && parts[1].Trim().Length > 0)
            {
                if (!double.TryParse(parts[1].Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out density))
                    return ErrJson("usage: nx_mass_properties [density_kg_m3] [accuracy]");
            }
            if (parts.Length > 2 && parts[2].Trim().Length > 0)
            {
                if (!double.TryParse(parts[2].Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out accuracy))
                    return ErrJson("usage: nx_mass_properties [density_kg_m3] [accuracy]");
            }
            if (density <= 0.0) return ErrJson("density must be > 0 (kg/m^3)");
            if (accuracy <= 0.0 || accuracy >= 1.0) accuracy = 0.99;

            // optional trailing "per=1": also report per-body values (multi-cavity
            // dies / multi-operation parts need each solid, not just the sum)
            bool perBody = false;
            for (int i = 3; i < parts.Length; i++)
                if (parts[i].Trim().Equals("per=1", StringComparison.OrdinalIgnoreCase))
                    perBody = true;

            List<Body> solids = new List<Body>();
            foreach (Body b in _part.Bodies.ToArray())
                if (b.IsSolidBody) solids.Add(b);
            if (solids.Count == 0) return ErrJson("no solid bodies in active part");

            Unit massU = null, lenU = null, volU = null;
            try { massU = _part.UnitCollection.FindObject("Kilogram"); } catch { }
            try { lenU = _part.UnitCollection.FindObject("Millimeter"); } catch { }
            try { volU = _part.UnitCollection.FindObject("CubicMillimeter"); } catch { }
            if (massU == null || lenU == null || volU == null)
                return ErrJson("unit lookup failed: mass=" + (massU != null)
                    + " length=" + (lenU != null) + " volume=" + (volU != null));

            MeasureBodies mp = null;
            try
            {
                mp = _part.MeasureManager.NewMassProperties(
                    new Unit[] { massU, lenU, volU }, accuracy, solids.ToArray());
            }
            catch (Exception e3)
            {
                Log("massprops [mass,len,vol] rejected: " + e3.Message);
                // Some NX builds take only [mass, length]; retry that shape.
                try
                {
                    mp = _part.MeasureManager.NewMassProperties(
                        new Unit[] { massU, lenU }, accuracy, solids.ToArray());
                }
                catch (Exception e2)
                {
                    return ErrJson("mass_properties failed: " + e2.Message);
                }
            }

            double volume = mp.Volume;   // mm^3
            double area = mp.Area;       // mm^2
            Point3d cog = mp.Centroid;   // mm
            // kg = kg/m^3 * mm^3 * 1e-9  (1 mm^3 = 1e-9 m^3); mass needs round-trip
            // formatting - the 3-decimal F() would flatten 7.83e-6 to "0".
            double massKg = volume * density * 1e-9;

            string res = "bodies=" + solids.Count
                + "  volume=" + F(volume) + " mm^3"
                + "  area=" + F(area) + " mm^2"
                + "  mass=" + massKg.ToString("R", CultureInfo.InvariantCulture) + " kg"
                + "  (density " + F(density) + " kg/m^3)"
                + "  cog=(" + F(cog.X) + "," + F(cog.Y) + "," + F(cog.Z) + ") mm";

            // per-body array (only when per=1 was passed)
            string perJson = "";
            if (perBody)
            {
                var sbp = new StringBuilder();
                sbp.Append(",\"per_body\":[");
                int idx = 0;
                foreach (Body b in solids)
                {
                    MeasureBodies mb = null;
                    try
                    {
                        try
                        {
                            mb = _part.MeasureManager.NewMassProperties(
                                new Unit[] { massU, lenU, volU }, accuracy, new Body[] { b });
                        }
                        catch
                        {
                            mb = _part.MeasureManager.NewMassProperties(
                                new Unit[] { massU, lenU }, accuracy, new Body[] { b });
                        }
                    }
                    catch (Exception eb)
                    {
                        Log("massprops per-body ERR: " + eb.Message);
                        continue;
                    }
                    double bvol = mb.Volume;
                    double bmass = bvol * density * 1e-9;
                    Point3d bcog = mb.Centroid;
                    string bid = ""; try { bid = b.Name; } catch { }
                    if (string.IsNullOrEmpty(bid)) { try { bid = b.JournalIdentifier; } catch { } }
                    if (idx > 0) sbp.Append(",");
                    sbp.Append("{\"body_id\":\"").Append(Esc(bid)).Append("\"");
                    sbp.Append(",\"volume_mm3\":").Append(bvol.ToString("R", CultureInfo.InvariantCulture));
                    sbp.Append(",\"surface_area_mm2\":").Append(mb.Area.ToString("R", CultureInfo.InvariantCulture));
                    sbp.Append(",\"mass_kg\":").Append(bmass.ToString("R", CultureInfo.InvariantCulture));
                    sbp.Append(",\"cog_x_mm\":").Append(F(bcog.X));
                    sbp.Append(",\"cog_y_mm\":").Append(F(bcog.Y));
                    sbp.Append(",\"cog_z_mm\":").Append(F(bcog.Z));
                    sbp.Append("}");
                    idx++;
                }
                sbp.Append("]");
                perJson = sbp.ToString();
            }

            return "{\"ok\":true"
                + ",\"body_count\":" + solids.Count
                + ",\"volume_mm3\":" + volume.ToString("R", CultureInfo.InvariantCulture)
                + ",\"surface_area_mm2\":" + area.ToString("R", CultureInfo.InvariantCulture)
                + ",\"mass_kg\":" + massKg.ToString("R", CultureInfo.InvariantCulture)
                + ",\"density_kg_m3\":" + density.ToString("R", CultureInfo.InvariantCulture)
                + ",\"cog_x_mm\":" + F(cog.X)
                + ",\"cog_y_mm\":" + F(cog.Y)
                + ",\"cog_z_mm\":" + F(cog.Z)
                + perJson
                + ",\"result\":\"" + Esc(res) + "\"}";
        }
        catch (Exception e)
        {
            return ErrJson("mass_properties failed: " + e.Message);
        }
    }

    // nx_create_centerline
    // Auto centreline for cylindrical/conical faces - very common on shaft drawings.
    // Entry: Part.Annotations.Centerlines.CreateAutomaticCenterlineBuilder().
    // nx_measure_angle <body_id> <edge_index_1> <edge_index_2>
    // 3D QUERY - angle between two STRAIGHT edges of a body. Pure math on the
    // edge direction vectors: MeasureManager.NewAngle exists but reading the
    // value goes through Scalar/CreateEmbedded indirection, and the UF wrapper
    // has no angle measurement either. Fixture/setup work mostly wants the
    // angle between two profile lines, so both the [0,180] angle and its acute
    // complement are reported.
    private static string MeasureAngle(string[] parts)
    {
        if (parts.Length < 4)
            return ErrJson("usage: nx_measure_angle <body_id> <edge_index_1> <edge_index_2>");
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part; run nx_create_part first");

            Body body = ResolveBody(parts[1].Trim());
            Edge[] edges = body.GetEdges();
            int i1 = int.Parse(parts[2], CultureInfo.InvariantCulture);
            int i2 = int.Parse(parts[3], CultureInfo.InvariantCulture);
            if (i1 < 0 || i1 >= edges.Length)
                return ErrJson("edge_index_1 out of range: " + i1 + " (have " + edges.Length + ")");
            if (i2 < 0 || i2 >= edges.Length)
                return ErrJson("edge_index_2 out of range: " + i2 + " (have " + edges.Length + ")");

            Edge e1 = edges[i1], e2 = edges[i2];
            string t1 = "", t2 = "";
            try { t1 = e1.SolidEdgeType.ToString(); } catch { }
            try { t2 = e2.SolidEdgeType.ToString(); } catch { }
            if (t1 != "Linear" || t2 != "Linear")
                return ErrJson("angle needs two straight edges; got " + t1 + " and " + t2
                    + " (pick line edges from nx_list_edges)");

            Point3d s1 = new Point3d(), en1 = new Point3d(), s2 = new Point3d(), en2 = new Point3d();
            e1.GetVertices(out s1, out en1);
            e2.GetVertices(out s2, out en2);
            double v1x = en1.X - s1.X, v1y = en1.Y - s1.Y, v1z = en1.Z - s1.Z;
            double v2x = en2.X - s2.X, v2y = en2.Y - s2.Y, v2z = en2.Z - s2.Z;
            double n1 = Math.Sqrt(v1x * v1x + v1y * v1y + v1z * v1z);
            double n2 = Math.Sqrt(v2x * v2x + v2y * v2y + v2z * v2z);
            if (n1 < 1e-9 || n2 < 1e-9) return ErrJson("degenerate (zero-length) edge");
            double dot = (v1x * v2x + v1y * v2y + v1z * v2z) / (n1 * n2);
            if (dot > 1.0) dot = 1.0; if (dot < -1.0) dot = -1.0;
            double ang = Math.Acos(dot) * 180.0 / Math.PI;          // [0,180]
            double acute = Math.Min(ang, 180.0 - ang);              // [0,90]

            string res = "angle=" + F(ang) + " deg (acute " + F(acute)
                + " deg) between edge " + i1 + " and edge " + i2;
            return "{\"ok\":true"
                + ",\"angle_deg\":" + F(ang)
                + ",\"acute_deg\":" + F(acute)
                + ",\"result\":\"" + Esc(res) + "\"}";
        }
        catch (Exception e)
        {
            return ErrJson("measure_angle failed: " + e.Message);
        }
    }

    // nx_create_centerline <view_id> <body_id> <face_index>
    // Symmetrical centreline (对称中心线) - the correct "shaft axis" annotation
    // for turned parts. AutomaticCenterlineBuilder was a dead end: Commit()
    // failed with NX status 1175325 even with the view added to its list (see
    // 受阻项调研档案). SymmetricalCenterlineBuilder with Type=FromFace
    // takes the cylindrical face of the shaft and draws its axis in the view.
    private static string CreateCenterline(string[] parts)
    {
        if (parts.Length < 4)
            return ErrJson("usage: nx_create_centerline <view_id> <body_id> <face_index>");
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part; run nx_create_part first");
            try { _session.ApplicationSwitchImmediate("UG_APP_DRAFTING"); }
            catch (Exception ea) { Log("app switch warn: " + ea.Message); }

            DrawingSheet sheet;
            DraftingView view = ResolveViewAnySheet(parts[1].Trim(), out sheet);
            try { sheet.Open(); } catch { }

            Body body = ResolveBody(parts[2].Trim());
            Face[] faces = body.GetFaces();
            int fi = int.Parse(parts[3].Trim(), CultureInfo.InvariantCulture);
            if (fi < 0 || fi >= faces.Length)
                return ErrJson("face_index out of range: " + fi + " (have " + faces.Length + ")");
            Face face = faces[fi];

            int before = 0;
            try { before = _part.Annotations.Centerlines.ToArray().Length; } catch { }

            // centreline annotation is view-dependent: expand the target view first
            bool activated = false;
            try
            {
                if (!view.IsActiveForSketching) { view.ActivateForSketching(); activated = true; }
            }
            catch (Exception ea) { Log("activate warn: " + ea.Message); }

            SymmetricalCenterlineBuilder cb = null;
            try
            {
                cb = _part.Annotations.Centerlines.CreateSymmetricalCenterlineBuilder(null);
                cb.Type = SymmetricalCenterlineBuilder.Types.FromFace;
                try { cb.Settings.Extension = 3.0; }
                catch (Exception es) { Log("extension warn: " + es.Message); }

                // a pick point somewhere on the face (midpoint of its first edge)
                Point3d pick = new Point3d(0, 0, 0);
                try
                {
                    Edge[] fedges = face.GetEdges();
                    if (fedges.Length > 0)
                    {
                        Point3d s = new Point3d(), e2 = new Point3d();
                        fedges[0].GetVertices(out s, out e2);
                        pick = new Point3d((s.X + e2.X) / 2.0, (s.Y + e2.Y) / 2.0, (s.Z + e2.Z) / 2.0);
                    }
                }
                catch (Exception ep) { Log("pick point warn: " + ep.Message); }

                cb.Face.SetValue(face, view, pick);
                cb.Commit();
            }
            finally { try { if (cb != null) cb.Destroy(); } catch { } }

            // An expanded view changes how later drafting commands behave. The
            // NX12 .NET API has no DeactivateForSketching, so collapse it via an
            // application switch round trip and verify below.
            if (activated)
            {
                try
                {
                    _session.ApplicationSwitchImmediate("UG_APP_MODELING");
                    _session.ApplicationSwitchImmediate("UG_APP_DRAFTING");
                }
                catch (Exception ea) { Log("app switch back warn: " + ea.Message); }
            }

            try { _part.DrawingSheets.RefreshCurrentSheet(); } catch { }
            int after = 0;
            try { after = _part.Annotations.Centerlines.ToArray().Length; } catch { }
            if (after <= before)
                return ErrJson("symmetrical_centerline created nothing (centerlines "
                    + before + " -> " + after + "); check that the face is cylindrical "
                    + "and visible in the view");
            bool stillActive = false;
            try { stillActive = view.IsActiveForSketching; } catch { }

            return OkJson("centerlines", after.ToString(CultureInfo.InvariantCulture),
                "symmetrical centerline created in view '" + view.Name + "' (centerlines "
                + before + " -> " + after + (stillActive ? ", view left expanded" : "") + ")");
        }
        catch (Exception e)
        {
            return ErrJson("create_centerline failed: " + e.Message);
        }
    }

    // nx_create_roughness <pos_x> <pos_y> [value] [text]
    // Surface finish (Ra) symbol - very common on machining drawings.
    // Entry: Part.Annotations.DraftingSurfaceFinishSymbols.CreateDraftingSurfaceFinishBuilder.
    private static string CreateRoughness(string[] parts)
    {
        if (parts.Length < 3)
            return ErrJson("usage: nx_create_roughness <pos_x> <pos_y> [value] [text]");
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part; run nx_create_part first");
            try { _session.ApplicationSwitchImmediate("UG_APP_DRAFTING"); }
            catch (Exception ea) { Log("app switch warn: " + ea.Message); }

            double posx = D(parts[1]);
            double posy = D(parts[2]);
            string value = (parts.Length > 3) ? parts[3].Trim() : "1.6";

            NXOpen.Annotations.DraftingSurfaceFinishBuilder rb =
                _part.Annotations.DraftingSurfaceFinishSymbols.CreateDraftingSurfaceFinishBuilder(null);
            string jid = "";
            try
            {
                rb.Origin.OriginPoint = new Point3d(posx, posy, 0.0);
                rb.Origin.Anchor = NXOpen.Annotations.OriginBuilder.AlignmentPosition.TopLeft;
                // A1 is the roughness value field (the "a1" slot of the symbol).
                rb.SingleRoughnessValue = true;
                rb.A1 = value;
                NXOpen.Annotations.DraftingSurfaceFinish sf =
                    (NXOpen.Annotations.DraftingSurfaceFinish)rb.Commit();
                try { sf.SetName("MCP_ROUGHNESS"); } catch { }
                try { jid = sf.JournalIdentifier; } catch { }
            }
            finally { try { rb.Destroy(); } catch { } }

            try { _part.DrawingSheets.RefreshCurrentSheet(); } catch { }
            return OkJson("roughness", jid, "created surface finish value=" + value);
        }
        catch (Exception e)
        {
            return ErrJson("create_roughness failed: " + e.Message);
        }
    }

    // nx_export_dwg <path.dwg|.dxf> [sheet_name]
    // DXF/DWG export. Entry is Session.DexManager.CreateDxfdwgCreator() - a NO-ARG
    // method, so its XML name has no "(" and is easy to miss when grepping for
    // signatures with parameters.
    private static string ExportDwg(string[] parts)
    {
        if (parts.Length < 2)
            return ErrJson("usage: nx_export_dwg <path.dwg|.dxf> [sheet_name]");
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part; run nx_create_part first");

            string path = parts[1].Trim();
            string dir = null;
            try { dir = Path.GetDirectoryName(path); } catch { }
            if (!string.IsNullOrEmpty(dir)) { try { Directory.CreateDirectory(dir); } catch { } }

            // OutputTo must be set to Drafting, otherwise Commit() succeeds but
            // writes no file - same class of trap as SourceBuilder.SetSheets for PDF.
            try { _session.ApplicationSwitchImmediate("UG_APP_DRAFTING"); }
            catch (Exception ea) { Log("app switch warn: " + ea.Message); }

            NXOpen.DxfdwgCreator dx = _session.DexManager.CreateDxfdwgCreator();
            string defCopy = null, logFile = null;
            try
            {
                dx.OutputFile = path;
                dx.OutputTo = NXOpen.DxfdwgCreator.OutputToOption.Drafting;
                dx.OutputFileType = path.ToLowerInvariant().EndsWith(".dxf")
                    ? NXOpen.DxfdwgCreator.OutputFileTypeOption.Dxf
                    : NXOpen.DxfdwgCreator.OutputFileTypeOption.Dwg;
                // Try Explicit source part instead of DisplayPart: headless the
                // display-part route produced no file at all.
                dx.InputFile = _partPath ?? _part.FullPath;
                dx.ExportFrom = NXOpen.DxfdwgCreator.ExportFromOption.ExistingPart;
                dx.ExportAs = NXOpen.DxfdwgCreator.ExportAsOption.TwoD;
                try { dx.ExportData = NXOpen.DxfdwgCreator.ExportDataOption.Drawing; }
                catch (Exception ed) { Log("export data warn: " + ed.Message); }
                string sheetName = parts.Length > 2 ? parts[2].Trim() : "";
                if (!string.IsNullOrEmpty(sheetName))
                {
                    try { dx.DrawingList = sheetName; }
                    catch (Exception el) { Log("drawing list warn: " + el.Message); }
                }
                // The translator runs as a separate dxfdwg.exe process configured by
                // a .def settings file. Without SettingsFile the commit "succeeds"
                // and NOTHING happens - the same class of trap as the PDF export's
                // missing SourceBuilder.SetSheets(). We copy the NX default def,
                // turn its LOG_FILE on and pin UGI_DRAWING_NAMES to the requested
                // sheet so the translator has a real configuration and we get logs.
                string baseDir = Environment.GetEnvironmentVariable("UGII_BASE_DIR");
                if (string.IsNullOrEmpty(baseDir))
                    baseDir = @"C:\Program Files\Siemens\NX 12.0";
                string defSrc = Path.Combine(baseDir, "DXFDWG", "dxfdwg.def");
                if (File.Exists(defSrc))
                {
                    try
                    {
                        defCopy = Path.Combine(
                            string.IsNullOrEmpty(dir) ? Path.GetTempPath() : dir,
                            "dxfdwg_mcp.def");
                        logFile = Path.Combine(
                            string.IsNullOrEmpty(dir) ? Path.GetTempPath() : dir,
                            "dxfdwg_mcp.log");
                        try { if (File.Exists(defCopy)) File.Delete(defCopy); } catch { }
                        try { if (File.Exists(logFile)) File.Delete(logFile); } catch { }
                        string[] lines = File.ReadAllLines(defSrc);
                        var sb = new StringBuilder();
                        foreach (string ln in lines)
                        {
                            string outLn = ln;
                            if (ln.StartsWith("LOG_FILE", StringComparison.OrdinalIgnoreCase))
                                outLn = "LOG_FILE = " + logFile;
                            else if (ln.StartsWith("UGI_DRAWING_NAMES", StringComparison.OrdinalIgnoreCase)
                                     && !string.IsNullOrEmpty(sheetName))
                                outLn = "UGI_DRAWING_NAMES = " + sheetName;
                            sb.Append(outLn).Append("\r\n");
                        }
                        File.WriteAllText(defCopy, sb.ToString());
                        dx.SettingsFile = defCopy;
                    }
                    catch (Exception es) { Log("settings file warn: " + es.Message); }
                }
                else
                {
                    Log("dxfdwg.def not found at " + defSrc);
                }
                dx.Commit();
            }
            finally { try { dx.Destroy(); } catch { } }

            bool exists = File.Exists(path);
            long size = 0;
            if (exists) { try { size = new FileInfo(path).Length; } catch { } }
            if (exists)
                return OkJson("dwg", path, "exported " + size + " bytes");

            // no file: surface whatever the translator logged so the next
            // iteration does not have to guess
            string diag = "commit ok but file not found";
            if (!string.IsNullOrEmpty(logFile) && File.Exists(logFile))
            {
                string[] lg = File.ReadAllLines(logFile);
                var tail = new StringBuilder();
                int start = Math.Max(0, lg.Length - 12);
                for (int i = start; i < lg.Length; i++) tail.Append(lg[i]).Append(" | ");
                diag = diag + "; translator log tail: " + tail.ToString();
            }
            else
            {
                diag = diag + (defCopy != null
                    ? "; no translator log written (translator did not run?)"
                    : "; no dxfdwg.def found to configure the translator");
            }
            return OkJson("dwg", path, diag);
        }
        catch (Exception e)
        {
            return ErrJson("export_dwg failed: " + e.Message);
        }
    }

    /// compiled with the legacy csc, which predates C# 7 local functions.
    // nx_create_detail_view <parent_view_id> <x> <y> [radius] [scale] [view_name]
    // Detail (enlarged) view: circular boundary around a point in the parent view.
    // UF "smart point" from three scalar doubles - UFDraw.AddCircDetailView
    // rejects plain points ("点必须是一个智能点"); smart points are UF_SO objects.
    private static Tag CreateSmartPoint(NXOpen.UF.UFSession uf, double x, double y, double z)
    {
        Tag sx, sy, sz, pt;
        NXOpen.UF.UFSo.UpdateOption opt =
            NXOpen.UF.UFSo.UpdateOption.UpdateWithinModeling;
        uf.So.CreateScalarDouble(_part.Tag, opt, x, out sx);
        uf.So.CreateScalarDouble(_part.Tag, opt, y, out sy);
        uf.So.CreateScalarDouble(_part.Tag, opt, z, out sz);
        uf.So.CreatePoint3Scalars(_part.Tag, opt, new Tag[] { sx, sy, sz }, out pt);
        return pt;
    }

    // nx_create_detail_view <parent_view_id> <cx> <cy> <radius> <px> <py> [scale] [view_name]
    // Detail (enlarged) view via UFDraw.AddDetailView - the UFUN route is the
    // one actually designed for programmatic creation. The NXOpen
    // DetailViewBuilder path creates view/label/boundary but the enlarged
    // geometry never renders (empty circle; see 受阻项调研档案 section 4).
    // The user-facing semantics stay "circular-ish": centre + radius are converted
    // to the rectangle (cx-r, cy-r)..(cx+r, cy+r) in parent-view coordinates.
    private static string CreateDetailView(string[] parts)
    {
        if (parts.Length < 7)
            return ErrJson("usage: nx_create_detail_view <parent_view_id> <cx> <cy> <radius> <px> <py> [scale] [view_name]");
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part; run nx_create_part first");
            try { _session.ApplicationSwitchImmediate("UG_APP_DRAFTING"); }
            catch (Exception ea) { Log("app switch warn: " + ea.Message); }

            DrawingSheet sheet = CurrentSheet();
            DraftingView[] views = sheet.GetDraftingViews();
            if (views == null || views.Length == 0) return ErrJson("sheet has no drafting views");

            string token = parts[1].Trim();
            DraftingView parent = null;
            for (int i = 0; i < views.Length; i++)
            {
                string nm = ""; try { nm = views[i].Name; } catch { }
                string ji = ""; try { ji = views[i].JournalIdentifier; } catch { }
                if (token == i.ToString() || token == nm || token == ji) { parent = views[i]; break; }
            }
            if (parent == null) parent = views[0];

            double cx = D(parts[2]);
            double cy = D(parts[3]);
            double radius = D(parts[4]);
            if (radius <= 0.0) return ErrJson("radius must be > 0");
            double px = D(parts[5]);
            double py = D(parts[6]);
            double scale = (parts.Length > 7 && !string.IsNullOrEmpty(parts[7].Trim())) ? D(parts[7]) : 0.0;
            string name = (parts.Length > 8) ? parts[8].Trim() : "";

            int before = 0;
            try { before = sheet.GetDraftingViews().Length; } catch { }

            NXOpen.UF.UFSession uf = NXOpen.UF.UFSession.GetUFSession();
            Tag dvTag = Tag.Null;
            string usedRoute = "";
            // Optional model-space height (parts[9]) for the circular-boundary
            // smart points: the points must be VISIBLE in the parent member view
            // (unobstructed), e.g. z = top face height or hovering above the
            // solid for a top view. 0 (inside the block) fails with
            // "点在成员视图中不可见".
            double detZ = (parts.Length > 9 && !string.IsNullOrEmpty(parts[9].Trim()))
                ? D(parts[9]) : 0.0;
            try
            {
                // circular route first: smart points + true circular boundary
                Tag cP = CreateSmartPoint(uf, cx, cy, detZ);
                Tag rP = CreateSmartPoint(uf, cx + radius, cy, detZ);
                if (cP == Tag.Null || rP == Tag.Null)
                    throw new Exception("smart point creation failed");
                // dwg_reference_point behaves like METRES while the xy boundary
                // behaves like millimetres (empirically: refPt in mm places the
                // detail view ~1000x off-sheet and the PDF shows only the parent
                // boundary box; refPt/1000 puts it where asked).
                double[] refPtC = new double[] { px / 1000.0, py / 1000.0 };
                uf.Draw.AddCircDetailView(sheet.Tag, parent.Tag, cP, rP,
                    scale > 0.0 ? scale : 1.0, refPtC, out dvTag);
                usedRoute = "circular";
            }
            catch (Exception ec)
            {
                Log("circ detail route failed (" + ec.Message + "); trying rectangle");
                // rectangular fallback: plain 2D doubles in drawing coordinates
                // (uf_draw.h: "Left lower / right upper corner of area to create
                // detailed view, in drawing coordinates")
                dvTag = Tag.Null;
                try
                {
                    double[] xy1 = new double[] { cx - radius, cy - radius };
                    double[] xy2 = new double[] { cx + radius, cy + radius };
                    double[] refPt = new double[] { px / 1000.0, py / 1000.0 };
                    uf.Draw.AddDetailView(sheet.Tag, parent.Tag, xy1, xy2,
                        scale > 0.0 ? scale : 1.0, refPt, out dvTag);
                    usedRoute = "rectangle";
                }
                catch (Exception eu)
                {
                    return ErrJson("AddDetailView failed: " + eu.Message);
                }
            }
            if (dvTag == Tag.Null)
                return ErrJson("detail view returned a null view tag");

            DraftingView created = null;
            try
            {
                created = NXOpen.Utilities.NXObjectManager.Get(dvTag) as DraftingView;
            }
            catch { }
            if (created == null)
            {
                DraftingView[] after = sheet.GetDraftingViews();
                if (after.Length > before) created = after[after.Length - 1];
            }
            if (created == null)
                return ErrJson("detail view tag could not be resolved to a view");

            if (!string.IsNullOrEmpty(name))
            {
                try { created.SetName(name); } catch { }
            }
            try { ForceUpdateSheet(sheet); } catch { }

            string id = ""; try { id = created.JournalIdentifier; } catch { }
            int count = 0;
            try { count = sheet.GetDraftingViews().Length; } catch { }
            return OkJson("view", id, "detail view created via " + usedRoute
                + " route, views=" + count
                + " boundary=center(" + F(cx) + "," + F(cy) + ") r=" + F(radius)
                + " placed=(" + F(px) + "," + F(py) + ")"
                + (scale > 0.0 ? " scale=" + F(scale) + ":1" : ""));
        }
        catch (Exception e)
        {
            return ErrJson("create_detail_view failed: " + e.Message);
        }
    }

    // nx_create_title_block <pos_x> <pos_y>
    // Places a title block on the current sheet.
    private static string CreateTitleBlock(string[] parts)
    {
        if (parts.Length < 3)
            return ErrJson("usage: nx_create_title_block <pos_x> <pos_y>");
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part; run nx_create_part first");
            try { _session.ApplicationSwitchImmediate("UG_APP_DRAFTING"); }
            catch (Exception ea) { Log("app switch warn: " + ea.Message); }

            double x = D(parts[1]);
            double y = D(parts[2]);

            int before = 0;
            try { before = _part.DraftingManager.TitleBlocks.ToArray().Length; } catch { }

            NXOpen.Annotations.DefineTitleBlockBuilder tbb =
                _part.DraftingManager.TitleBlocks.CreateDefineTitleBlockBuilder(null);
            try
            {
                // Origin here is a plain Point3d, not an OriginBuilder.
                tbb.Origin = new Point3d(x, y, 0.0);
                tbb.Commit();
            }
            finally { try { tbb.Destroy(); } catch { } }

            int after = 0;
            try { after = _part.DraftingManager.TitleBlocks.ToArray().Length; } catch { }

            try { ForceUpdateSheet(CurrentSheet()); } catch { }
            return OkJson("title_block", "ok",
                "title block created at " + F(x) + "," + F(y)
                + "  count " + before + " -> " + after
                + (after <= before ? "  [WARNING: nothing was actually created]" : ""));
        }
        catch (Exception e)
        {
            return ErrJson("create_title_block failed: " + e.Message);
        }
    }

    // nx_set_attribute <name> <value...>
    // Title-block cells normally display PART ATTRIBUTES, so filling a title block
    // means setting attributes. Value may contain spaces (tokens are re-joined).
    private static string SetAttributeCmd(string[] parts)
    {
        if (parts.Length < 3)
            return ErrJson("usage: nx_set_attribute <name> <value...>");
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part; run nx_create_part first");

            string name = parts[1].Trim();
            var sb = new StringBuilder();
            for (int i = 2; i < parts.Length; i++)
            {
                if (i > 2) sb.Append(" ");
                sb.Append(parts[i]);
            }
            string value = sb.ToString().Trim();

            // Second arg is the array INDEX, not the type: -1 means "not an array".
            _part.SetUserAttribute(name, -1, value, NXOpen.Update.Option.Now);
            return OkJson("attribute", name, "set " + name + " = " + value);
        }
        catch (Exception e)
        {
            return ErrJson("set_attribute failed: " + e.Message);
        }
    }

    // nx_list_attributes
    // Read back the part attributes (what a title block would display).
    private static string ListAttributes(string[] parts)
    {
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part; run nx_create_part first");

            var items = new System.Collections.Generic.List<string>();
            NXOpen.NXObject.AttributeInformation[] atts = _part.GetUserAttributes();
            if (atts != null)
            {
                foreach (NXOpen.NXObject.AttributeInformation a in atts)
                {
                    string t = ""; try { t = a.Title; } catch { }
                    string v = "";
                    try { v = a.StringValue; } catch { }
                    if (string.IsNullOrEmpty(v)) { try { v = a.IntegerValue.ToString(CultureInfo.InvariantCulture); } catch { } }
                    if (string.IsNullOrEmpty(v)) { try { v = a.RealValue.ToString(CultureInfo.InvariantCulture); } catch { } }
                    items.Add(t + "=" + v);
                }
            }
            return OkJson("attributes", items.Count.ToString(CultureInfo.InvariantCulture),
                "found " + items.Count + " attribute(s) [" + string.Join(", ", items.ToArray()) + "]");
        }
        catch (Exception e)
        {
            return ErrJson("list_attributes failed: " + e.Message);
        }
    }

    // nx_create_sheet_border [zones=1]
    // Sheet border lines + optional zone letters/numbers.
    // [NX12 port] Drawings.BordersAndZonesBuilder is deprecated in NX10.0.0; the
    // supported route is Diagramming.SheetBordersAndZonesBuilder, reached via
    // Part.DiagrammingManager.SheetBordersAndZones.
    private static string CreateSheetBorder(string[] parts)
    {
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part; run nx_create_part first");
            try { _session.ApplicationSwitchImmediate("UG_APP_DRAFTING"); }
            catch (Exception ea) { Log("app switch warn: " + ea.Message); }

            bool zones = !(parts.Length > 1 && parts[1].Trim() == "0");

            int before = 0;
            try { before = _part.DiagrammingManager.SheetBordersAndZones.ToArray().Length; } catch { }

            NXOpen.Diagramming.SheetBordersAndZonesBuilder bz =
                _part.DiagrammingManager.SheetBordersAndZones
                     .CreateSheetBordersAndZonesBuilder(null);
            try
            {
                bz.CreateBorders = true;
                // Zone sizes default to 0, which NX rejects:
                // "宽度必须大于零...或小于图纸页大小的四分之一".
                bz.HorizontalSize = 50.0;
                bz.VerticalSize = 50.0;
                bz.Width = 1.0;
                // Zone settings live on the sub-builder and CreateZones must be on.
                NXOpen.Diagramming.SheetZoneSettingsBuilder zs = bz.SheetZoneSettings;
                try { zs.CreateZones = true; }
                catch (Exception ez) { Log("zone create warn: " + ez.Message); }
                try { zs.HorizontalSize = 50.0; }
                catch (Exception ez) { Log("zone hsize warn: " + ez.Message); }
                try { zs.VerticalSize = 50.0; }
                catch (Exception ez) { Log("zone vsize warn: " + ez.Message); }
                try { zs.CreateZoneMarkings = true; }
                catch (Exception ez) { Log("zone marking warn: " + ez.Message); }
                if (zones)
                {
                    bz.CreateZoneLabels = true;
                    bz.CreateZoneMarking = true;
                }
                bz.Commit();
            }
            finally { try { bz.Destroy(); } catch { } }

            int after = 0;
            try { after = _part.DiagrammingManager.SheetBordersAndZones.ToArray().Length; } catch { }

            try { ForceUpdateSheet(CurrentSheet()); } catch { }
            return OkJson("border", "ok",
                "sheet border enabled zones=" + zones
                + "  count " + before + " -> " + after
                + (after <= before ? "  [WARNING: nothing was actually created]" : ""));
        }
        catch (Exception e)
        {
            return ErrJson("create_sheet_border failed: " + e.Message);
        }
    }

    private static bool AnnotationMatches(NXObject o, string token)
    {
        try { if (token == o.JournalIdentifier) return true; } catch { }
        try { if (token == o.Name) return true; } catch { }
        return false;
    }

    // nx_delete_annotation <id | kind:index>
    // Removes a note / dimension / GDT / roughness symbol. Needed because a stray
    // annotation cannot be removed any other way (nx_undo only reaches the most
    // recent visible mark, which is often not enough).
    // Deletion goes through Session.UpdateManager: AddToDeleteList + DoUpdate.
    private static string DeleteAnnotation(string[] parts)
    {
        if (parts.Length < 2)
            return ErrJson("usage: nx_delete_annotation <id|kind:index>  (kind = note|dim|gdt|rough)");
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part; run nx_create_part first");
            string token = parts[1].Trim();

            // [NX12 port] Update.AddToDeleteList is deprecated exactly in NX12.0.0;
            // use Update.AddObjectsToDeleteList(TaggedObject[]) instead.
            var objs = new List<NXOpen.TaggedObject>();

            // ---- collect candidates by kind ----
            var notes = new List<NXOpen.Annotations.Note>();
            var dims = new List<NXOpen.Annotations.Dimension>();
            var gdts = new List<NXOpen.Annotations.Gdt>();
            var roughs = new List<NXOpen.Annotations.DraftingSurfaceFinish>();
            try { foreach (NXOpen.Annotations.Note n in _part.Notes.ToArray()) notes.Add(n); } catch { }
            try { foreach (NXOpen.Annotations.Dimension d in _part.Dimensions.ToArray()) dims.Add(d); } catch { }
            try { foreach (NXOpen.Annotations.Gdt g in _part.Gdts.ToArray()) gdts.Add(g); } catch { }
            try
            {
                foreach (NXOpen.Annotations.DraftingSurfaceFinish sf
                         in _part.Annotations.DraftingSurfaceFinishSymbols.ToArray()) roughs.Add(sf);
            }
            catch { }

            // ---- 1) exact id / name match across all kinds ----
            foreach (var n in notes) if (AnnotationMatches(n, token)) objs.Add(n);
            foreach (var d in dims) if (AnnotationMatches(d, token)) objs.Add(d);
            foreach (var g in gdts) if (AnnotationMatches(g, token)) objs.Add(g);
            foreach (var r in roughs) if (AnnotationMatches(r, token)) objs.Add(r);

            // ---- 2) fallback: "kind:index" ----
            if (objs.Count == 0 && token.Contains(":"))
            {
                string[] kv = token.Split(':');
                string kind = kv[0].Trim().ToLowerInvariant();
                int idx;
                if (kv.Length == 2 && int.TryParse(kv[1], NumberStyles.Integer, CultureInfo.InvariantCulture, out idx))
                {
                    if (kind == "note" && idx >= 0 && idx < notes.Count) objs.Add(notes[idx]);
                    else if (kind == "dim" && idx >= 0 && idx < dims.Count) objs.Add(dims[idx]);
                    else if (kind == "gdt" && idx >= 0 && idx < gdts.Count) objs.Add(gdts[idx]);
                    else if (kind == "rough" && idx >= 0 && idx < roughs.Count) objs.Add(roughs[idx]);
                    else return ErrJson("kind:index out of range: " + token);
                }
            }

            if (objs.Count == 0)
                return ErrJson("no annotation matched: " + token
                    + " (try nx_list_dimensions / nx_list_annotations first)");

            NXOpen.Session.UndoMarkId mark =
                _session.SetUndoMark(NXOpen.Session.MarkVisibility.Visible, "Delete Annotation");
            _session.UpdateManager.AddObjectsToDeleteList(objs.ToArray());
            _session.UpdateManager.DoUpdate(mark);

            return OkJson("deleted", token, "deleted " + objs.Count + " annotation(s)");
        }
        catch (Exception e)
        {
            return ErrJson("delete_annotation failed: " + e.Message);
        }
    }

    // nx_set_dimension_value <target_size> <new_value>
    // Find the associative dimension whose ComputedSize is closest to
    // <target_size>, then force its displayed value to <new_value> so the
    // drawing shows a mid-tolerance (中差) nominal. NX 12's Dimension API has
    // no public Value/BreakAssociativity, so we probe via reflection and apply
    // whatever override mechanism the loaded assembly exposes.
    private static string SetDimensionValue(string[] parts)
    {
        if (parts.Length < 3) return ErrJson("usage: nx_set_dimension_value <target_size> <new_value>");
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part");
            double target = D(parts[1]);
            double nv = D(parts[2]);
            var dims = new List<NXOpen.Annotations.Dimension>();
            try { foreach (NXOpen.Annotations.Dimension d in _part.Dimensions.ToArray()) dims.Add(d); } catch { }
            if (dims.Count == 0) return ErrJson("no dimensions in part");

            NXOpen.Annotations.Dimension best = null;
            double bestErr = 1e9;
            foreach (var d in dims)
            {
                double cs = 0; try { cs = d.ComputedSize; } catch { continue; }
                double err = Math.Abs(cs - target);
                if (err < bestErr) { bestErr = err; best = d; }
            }
            if (best == null) return ErrJson("no dimension near " + F(target));

            var t = best.GetType();
            var sb = new System.Text.StringBuilder();
            // 1) try to detach associativity (any of these names)
            foreach (string mn in new[] { "BreakAssociativity", "Disassociate", "RemoveAllAssociativity", "MakeNonAssociative" })
            {
                var m = t.GetMethod(mn);
                if (m != null) { try { m.Invoke(best, null); sb.Append("broke(" + mn + ");"); } catch (Exception ex) { sb.Append("breakErr(" + mn + "):" + ex.Message + ";"); } break; }
            }
            // 2) try to set the displayed value
            bool setOk = false;
            var vp = t.GetProperty("Value");
            if (vp != null && vp.CanWrite) { try { vp.SetValue(best, nv, null); sb.Append("setValueProp;"); setOk = true; } catch (Exception ex) { sb.Append("setValuePropErr:" + ex.Message + ";"); } }
            if (!setOk)
            {
                var ms = t.GetMethod("SetValue", new System.Type[] { typeof(double) });
                if (ms != null) { try { ms.Invoke(best, new object[] { nv }); sb.Append("setValueMeth;"); setOk = true; } catch (Exception ex) { sb.Append("setValueMethErr:" + ex.Message + ";"); } }
            }
            if (!setOk)
            {
                // NX 12 Dimension has no public Value setter; the only supported
                // way to pin a displayed value is through the DimensionBuilder.
                // Open the builder on the dimension, set its Value, commit.
                try
                {
                    var mgrType = _part.Annotations.GetType();
                    var bld = mgrType.GetMethod("CreateDimensionBuilder");
                    if (bld != null)
                    {
                        object builder = bld.Invoke(_part.Annotations, new object[] { best });
                        var bt = builder.GetType();
                        bool bset = false;
                        var bvp = bt.GetProperty("Value");
                        if (bvp != null && bvp.CanWrite) { try { bvp.SetValue(builder, nv, null); bset = true; sb.Append("builderValue;"); } catch (Exception ex) { sb.Append("bValErr:" + ex.Message + ";"); } }
                        if (!bset)
                        {
                            var bvm = bt.GetMethod("SetValue", new System.Type[] { typeof(double) });
                            if (bvm != null) { try { bvm.Invoke(builder, new object[] { nv }); bset = true; sb.Append("builderSetValue;"); } catch (Exception ex) { sb.Append("bSetErr:" + ex.Message + ";"); } }
                        }
                        var commit = bt.GetMethod("Commit");
                        if (commit != null) { try { commit.Invoke(builder, null); setOk = true; } catch (Exception ex) { sb.Append("commitErr:" + ex.Message + ";"); } }
                        var destroy = bt.GetMethod("Destroy");
                        if (destroy != null) { try { destroy.Invoke(builder, null); } catch { } }
                    }
                    else sb.Append("noCreateDimBuilder;");
                }
                catch (Exception ex) { sb.Append("builderErr:" + ex.Message + ";"); }
            }
            if (!setOk)
            {
                // last resort: report candidate members to guide a follow-up fix
                sb.Append("NO_OVERRIDE; candidates: ");
                foreach (var p in t.GetProperties()) if (p.Name.ToLower().Contains("value") || p.Name.ToLower().Contains("assoc")) sb.Append(p.Name + "(" + (p.CanWrite ? "w" : "r") + ") ");
                foreach (var m in t.GetMethods()) if (m.Name.ToLower().Contains("value") || m.Name.ToLower().Contains("assoc") || m.Name.ToLower().Contains("break")) sb.Append(m.Name + "() ");
            }

            foreach (NXOpen.Drawings.DrawingSheet sh in _part.DrawingSheets.ToArray())
            {
                try { ForceUpdateSheet(sh); } catch { }
            }
            string jid = ""; try { jid = best.JournalIdentifier; } catch { }
            return OkJson("dimension", jid, "target=" + F(target) + " set=" + F(nv) + " " + sb.ToString());
        }
        catch (Exception e)
        {
            return ErrJson("set_dimension_value failed: " + e.Message);
        }
    }

    // nx_list_annotations
    // Read back the non-dimension annotations of a drawing, so an agent can
    // understand an existing 2D process drawing beyond just sizes:
    //   NOTE  - drafting / technical-requirement text   (Part.Notes -> GetText)
    //   GDT   - geometric tolerance                     (Part.Gdts -> GetFcfFrameData)
    //   ROUGH - surface finish symbols                  (Annotations.DraftingSurfaceFinishSymbols)
    private static string ListAnnotations(string[] parts)
    {
        try
        {
            if (_part == null) return ErrJson("no active part");
            var items = new List<string>();

            // ---- 1. notes (technical requirement text) ----
            try
            {
                foreach (NXOpen.Annotations.Note n in _part.Notes.ToArray())
                {
                    string ji = "";
                    try { ji = n.JournalIdentifier; } catch { }
                    string txt = "";
                    try
                    {
                        string[] lines = n.GetText();
                        if (lines != null) txt = string.Join(" / ", lines);
                    }
                    catch { }
                    txt = txt.Replace("\r", " ").Replace("\n", " ").Trim();
                    items.Add("NOTE|" + ji + "|" + txt);
                }
            }
            catch { }

            // ---- 2. geometric dimensioning & tolerancing ----
            try
            {
                foreach (NXOpen.Annotations.Gdt g in _part.Gdts.ToArray())
                {
                    string ji = "";
                    try { ji = g.JournalIdentifier; } catch { }
                    // [NX12 port] FcfFrameData.GetFrames()/GeometricCharacteristic and
                    // FcfFrame.GetTolerance()/Get*DatumReference() are all deprecated in
                    // NX10.0.0. Use the recommended drafting FCF builder instead, obtained
                    // from AnnotationManager.CreateDraftingFeatureControlFrameBuilder(Gdt).
                    NXOpen.Annotations.FeatureControlFrameBuilder fb = null;
                    try
                    {
                        fb = _part.Annotations.CreateDraftingFeatureControlFrameBuilder(g);
                        string ch = "";
                        try { ch = fb.Characteristic.ToString(); } catch { }
                        // FeatureControlFrameDataList is a builder *list* object
                        // (Length + FindItem), not a plain array.
                        NXOpen.Annotations.FeatureControlFrameDataBuilderList dataList = null;
                        try { dataList = fb.FeatureControlFrameDataList; } catch { }
                        int nFrames = 0;
                        try { if (dataList != null) nFrames = dataList.Length; } catch { }
                        if (dataList == null || nFrames <= 0)
                        {
                            items.Add("GDT|" + ji + "|" + ch + "|<no frames>");
                            continue;
                        }
                        for (int fi = 0; fi < nFrames; fi++)
                        {
                            NXOpen.Annotations.FeatureControlFrameDataBuilder d = null;
                            try { d = dataList.FindItem(fi); } catch { }
                            if (d == null) continue;
                            string tol = "";
                            try { object v = d.ToleranceValue; if (v != null) tol = v.ToString(); } catch { }
                            string d1 = "", d2 = "", d3 = "";
                            try { object r = d.PrimaryDatumReference; if (r != null) d1 = r.ToString(); } catch { }
                            try { object r = d.SecondaryDatumReference; if (r != null) d2 = r.ToString(); } catch { }
                            try { object r = d.TertiaryDatumReference; if (r != null) d3 = r.ToString(); } catch { }
                            items.Add("GDT|" + ji + "|" + ch
                                + "|tol=" + OneLine(tol)
                                + "|datum=" + OneLine(d1) + "," + OneLine(d2) + "," + OneLine(d3));
                        }
                    }
                    catch (Exception eg)
                    {
                        items.Add("GDT|" + ji + "|<read error: " + OneLine(eg.Message) + ">");
                    }
                    finally
                    {
                        if (fb != null) { try { fb.Destroy(); } catch { } }
                    }
                }
            }
            catch { }

            // ---- 3. surface finish (roughness) symbols ----
            try
            {
                NXOpen.Annotations.DraftingSurfaceFinishCollection sfs =
                    _part.Annotations.DraftingSurfaceFinishSymbols;
                foreach (NXOpen.Annotations.DraftingSurfaceFinish sf in sfs.ToArray())
                {
                    string ji = "";
                    try { ji = sf.JournalIdentifier; } catch { }
                    items.Add("ROUGH|" + ji);
                }
            }
            catch { }

            return OkJsonList("annotations", items, "found " + items.Count + " annotation(s)");
        }
        catch (Exception e)
        {
            return ErrJson("list_annotations failed: " + e.Message);
        }
    }

    private static string OneLine(string s)
    {
        if (string.IsNullOrEmpty(s)) return "";
        return s.Replace("\r", " ").Replace("\n", " ").Trim();
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
        // [NX12 fix] When a part holds drawing sheets that are not displayed at save
        // time, NX pops a MODAL "保存 CGM" warning ("CGM will be deleted, continue?").
        // NX then blocks until a human clicks, so every pipe call hangs until the
        // 300s timeout. Explicitly disabling drawing-CGM generation removes the
        // prompt. PartSaveOptions.DrawingCgmData exists since NX3.0, so it is safe
        // on NX 12.
        try { _part.SaveOptions.DrawingCgmData = false; }
        catch (Exception e) { Log("save options warn: " + e.Message); }
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

        // NX 2506 does not reliably honor a bare CreatePlane(origin, normal)
        // when CreateSketchInPlaceBuilder2 commits a non-XY sketch: the sketch
        // can silently fall back to Z=0. Use an explicitly oriented fixed plane
        // and fully specify origin + horizontal axis + ExistingPlane mode.
        Point3d origin = new Point3d(0.0, 0.0, 0.0);
        Plane planeRef = _part.Planes.CreateFixedTypePlane(
            origin,
            SketchPlaneMatrix(plane),
            SmartObject.UpdateOption.WithinModeling);
        Direction axisRef = _part.Directions.CreateDirection(
            origin,
            SketchLocalXAxis(plane),
            SmartObject.UpdateOption.WithinModeling);
        NXOpen.Point sketchOrigin = _part.Points.CreatePoint(origin);

        var b = _part.Sketches.CreateSketchInPlaceBuilder2(null);
        Sketch sk;
        try
        {
            b.PlaneReference = planeRef;
            b.AxisReference = axisRef;
            b.SketchOrigin = sketchOrigin;
            b.PlaneOption = Sketch.PlaneOption.ExistingPlane;
            b.OriginOption = OriginMethod.SpecifyPoint;
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

    // Read a (possibly pre-existing, externally-authored) sketch and dump its
    // 2D geometry in sketch-local coordinates so an agent can faithfully
    // re-create the profile in another part. X axis = u, Y axis = w of the
    // sketch's own orientation; conversion uses Origin + Orientation so it
    // works regardless of which plane the sketch lives on.
    private static string ReadSketch(string[] parts)
    {
        if (parts.Length < 2) return ErrJson("usage: nx_read_sketch <sketch_name>");
        if (_part == null) return ErrJson("no active part");
        string token = parts[1];
        Sketch sk = null;
        foreach (Sketch s in _part.Sketches)
        {
            if (s.Name == token || s.Name.EndsWith(token) || token.EndsWith(s.Name)
                || s.Name.Contains(token) || token.Contains(s.Name))
            { sk = s; break; }
        }
        if (sk == null) return ErrJson("sketch not found: " + token);

        Point3d origin = sk.Origin;
        Matrix3x3 om = sk.Orientation.Element;
        Vector3d xAxis = new Vector3d(om.Xx, om.Xy, om.Xz);
        Vector3d yAxis = new Vector3d(om.Yx, om.Yy, om.Yz);
        Vector3d zAxis = new Vector3d(om.Zx, om.Zy, om.Zz);
        System.Func<Point3d, double> U = (p) => (p.X - origin.X) * xAxis.X + (p.Y - origin.Y) * xAxis.Y + (p.Z - origin.Z) * xAxis.Z;
        System.Func<Point3d, double> W = (p) => (p.X - origin.X) * yAxis.X + (p.Y - origin.Y) * yAxis.Y + (p.Z - origin.Z) * yAxis.Z;

        // Point on an arc/ellipse given its own CSYS matrix, center, radius, angle.
        System.Func<NXOpen.NXMatrix, Point3d, double, double, Point3d> OnCurve = (mat, ctr, r, ang) =>
        {
            Matrix3x3 eM = mat.Element;
            Vector3d ax = new Vector3d(eM.Xx, eM.Xy, eM.Xz);
            Vector3d ay = new Vector3d(eM.Yx, eM.Yy, eM.Yz);
            double ca = Math.Cos(ang), sa = Math.Sin(ang);
            return new Point3d(
                ctr.X + r * (ca * ax.X + sa * ay.X),
                ctr.Y + r * (ca * ax.Y + sa * ay.Y),
                ctr.Z + r * (ca * ax.Z + sa * ay.Z));
        };

        string plane = "XY";
        if (IsAxis(xAxis, 1, 0, 0) && IsAxis(yAxis, 0, 0, 1)) plane = "XZ";
        else if (IsAxis(xAxis, 0, 1, 0) && IsAxis(yAxis, 0, 0, 1)) plane = "YZ";
        else if (IsAxis(xAxis, 1, 0, 0) && IsAxis(yAxis, 0, 1, 0)) plane = "XY";

        var sb = new System.Text.StringBuilder();
        sb.Append("{\"ok\":true,\"sketch\":\"").Append(Esc(sk.Name)).Append("\"");
        sb.Append(",\"plane\":\"").Append(plane).Append("\"");
        sb.Append(",\"origin\":[").Append(F(origin.X)).Append(",").Append(F(origin.Y)).Append(",").Append(F(origin.Z)).Append("]");
        sb.Append(",\"xaxis\":[").Append(F(xAxis.X)).Append(",").Append(F(xAxis.Y)).Append(",").Append(F(xAxis.Z)).Append("]");
        sb.Append(",\"yaxis\":[").Append(F(yAxis.X)).Append(",").Append(F(yAxis.Y)).Append(",").Append(F(yAxis.Z)).Append("]");
        sb.Append(",\"curves\":[");
        bool first = true;
        try
        {
            foreach (object o in sk.GetAllGeometry())
            {
                NXOpen.Curve c = o as NXOpen.Curve;
                if (c == null) continue;
                if (!first) sb.Append(",");
                first = false;
                Line ln = c as Line;
                if (ln != null)
                {
                    sb.Append("{\"type\":\"line\",\"a\":[").Append(F(U(ln.StartPoint))).Append(",").Append(F(W(ln.StartPoint))).Append("]");
                    sb.Append(",\"b\":[").Append(F(U(ln.EndPoint))).Append(",").Append(F(W(ln.EndPoint))).Append("]}");
                    continue;
                }
                Arc arc = c as Arc;
                if (arc != null)
                {
                    double cx = U(arc.CenterPoint), cy = W(arc.CenterPoint);
                    Point3d sp = OnCurve(arc.Matrix, arc.CenterPoint, arc.Radius, arc.StartAngle);
                    Point3d ep = OnCurve(arc.Matrix, arc.CenterPoint, arc.Radius, arc.EndAngle);
                    double a1 = Math.Atan2(W(sp) - cy, U(sp) - cx) * 180.0 / Math.PI;
                    double a2 = Math.Atan2(W(ep) - cy, U(ep) - cx) * 180.0 / Math.PI;
                    sb.Append("{\"type\":\"arc\",\"c\":[").Append(F(cx)).Append(",").Append(F(cy)).Append("],\"r\":").Append(F(arc.Radius));
                    sb.Append(",\"a1\":").Append(F(a1)).Append(",\"a2\":").Append(F(a2)).Append("}");
                    continue;
                }
                Ellipse el = c as Ellipse;
                if (el != null)
                {
                    double cx = U(el.CenterPoint), cy = W(el.CenterPoint);
                    bool isCircle = Math.Abs(el.MajorRadius - el.MinorRadius) < 1e-6;
                    double span = Math.Abs(el.EndAngle - el.StartAngle);
                    if (isCircle && span >= 2.0 * Math.PI - 1e-3)
                    {
                        sb.Append("{\"type\":\"circle\",\"c\":[").Append(F(cx)).Append(",").Append(F(cy)).Append("],\"d\":").Append(F(2.0 * el.MajorRadius)).Append("}");
                    }
                    else
                    {
                        Point3d sp = OnCurve(el.Matrix, el.CenterPoint, el.MajorRadius, el.StartAngle);
                        Point3d ep = OnCurve(el.Matrix, el.CenterPoint, el.MajorRadius, el.EndAngle);
                        double a1 = Math.Atan2(W(sp) - cy, U(sp) - cx) * 180.0 / Math.PI;
                        double a2 = Math.Atan2(W(ep) - cy, U(ep) - cx) * 180.0 / Math.PI;
                        sb.Append("{\"type\":\"arc\",\"c\":[").Append(F(cx)).Append(",").Append(F(cy)).Append("],\"r\":").Append(F(el.MajorRadius));
                        sb.Append(",\"a1\":").Append(F(a1)).Append(",\"a2\":").Append(F(a2)).Append("}");
                    }
                    continue;
                }
                sb.Append("{\"type\":\"unknown\"}");
            }
        }
        catch (Exception e) { sb.Append("{\"type\":\"read_error\",\"msg\":\"").Append(Esc(e.Message)).Append("\"}"); }
        sb.Append("]}");
        return sb.ToString();
    }

    private static bool IsAxis(Vector3d v, double x, double y, double z)
    {
        return Math.Abs(v.X - x) < 0.01 && Math.Abs(v.Y - y) < 0.01 && Math.Abs(v.Z - z) < 0.01;
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
            // [NX12 port] MirrorBodyBuilder.DeleteSourceBody does NOT exist in NX 12
            // (the member list in NXOpen.xml for NX 12 has no such property; it was
            // introduced in a later release). NX 12's Mirror Body keeps the source
            // body by default, which already matches the intended `false` semantics.
            // mb.DeleteSourceBody = false;
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
        // [NX12 port] NX 12 ScCollector has no AddRules(). Use the functionally
        // equivalent ReplaceRules(rules, createRulesWoUpdate) instead.
        // ReplaceRules has existed since NX4.0.0, so it is safe on NX 12.
        coll.ReplaceRules(new SelectionIntentRule[] { faceRule }, false);

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

    // Resolves a body by its MCP id (from _bodies) or falls back to the first
    // solid body in the work part. This lets edge_blend/chamfer/hole work on
    // parts that were opened (not created) by the loader, where _bodies is empty.
    private static Body GetBody(string id)
    {
        if (!string.IsNullOrEmpty(id))
        {
            Body b;
            if (_bodies.TryGetValue(id, out b)) return b;
        }
        Body[] all = _part.Bodies.ToArray();
        foreach (Body b in all) if (b.IsSolidBody) return b;
        if (all.Length > 0) return all[0];
        throw new Exception("no body in work part");
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

    private static Vector3d SketchLocalXAxis(string plane)
    {
        switch (plane)
        {
            case "YZ": return new Vector3d(0.0, 1.0, 0.0);
            default: return new Vector3d(1.0, 0.0, 0.0);
        }
    }

    private static Matrix3x3 SketchPlaneMatrix(string plane)
    {
        Matrix3x3 m = new Matrix3x3();
        if (plane == "XZ")
        {
            // local x -> +X, local y -> +Z, local normal -> -Y
            m.Xx = 1.0; m.Xy = 0.0;  m.Xz = 0.0;
            m.Yx = 0.0; m.Yy = 0.0;  m.Yz = 1.0;
            m.Zx = 0.0; m.Zy = -1.0; m.Zz = 0.0;
        }
        else if (plane == "YZ")
        {
            // local x -> +Y, local y -> +Z, local normal -> +X
            m.Xx = 0.0; m.Xy = 1.0; m.Xz = 0.0;
            m.Yx = 0.0; m.Yy = 0.0; m.Yz = 1.0;
            m.Zx = 1.0; m.Zy = 0.0; m.Zz = 0.0;
        }
        else
        {
            m.Xx = 1.0; m.Xy = 0.0; m.Xz = 0.0;
            m.Yx = 0.0; m.Yy = 1.0; m.Yz = 0.0;
            m.Zx = 0.0; m.Zy = 0.0; m.Zz = 1.0;
        }
        return m;
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
    /// Normalise a path so comparisons are not defeated by mixed '/' and '\'.
    private static string NormPath(string p)
    {
        if (string.IsNullOrEmpty(p)) return "";
        return p.Trim().Replace("/", "\\");
    }

    private static void RegisterExistingBodies(NXOpen.Part part)
    {
        if (part == null) return;
        try
        {
            foreach (Body b in part.Bodies.ToArray())
            {
                string bid = "";
                try { bid = b.Name; } catch { }
                if (string.IsNullOrEmpty(bid)) { try { bid = b.JournalIdentifier; } catch { } }
                if (string.IsNullOrEmpty(bid)) bid = "BODY_" + (++_bodyCounter);
                _bodies[bid] = b;
            }
        }
        catch { }
    }

    // ------------------------------------------------------------------
    // WAVE 几何链接器：从上游客体源部件把实体链入当前工作部件（目标件）。
    // 链接类型 = 固定时间戳记（Timestamp），即抓取一份"冻结时刻"的快照，
    // 不再随主模型实时更新——用于"主模型 → 各工序文件"的自上而下设计。
    //   用法: nx_create_wave_link <source_part_path> [body_index]
    // ------------------------------------------------------------------
    private static string CreateWaveLink(string[] parts)
    {
        // 用法: nx_create_wave_link <源主模型> <目标工序件> [body_index] [assoc=1] [x=..] [y=..] [z=..]
        // 源件成为装配根；目标件作为源件的下游组件被链入（NX WAVE 要求"下游"结构）。
        // x=/y=/z= 为链入几何的摆放偏移（mm），用于"不同位置链出不同工序件"。
        if (parts.Length < 3) return ErrJson("usage: nx_create_wave_link <source_master> <target_part> [body_index] [assoc=1] [x=0] [y=0] [z=0]");
        if (_session == null) _session = Session.GetSession();

        string srcPath = parts[1];
        string tgtPath = parts[2];
        if (!File.Exists(srcPath)) return ErrJson("source part not found: " + srcPath);
        if (string.Equals(NormPath(srcPath), NormPath(tgtPath), StringComparison.OrdinalIgnoreCase))
            return ErrJson("source and target are the same file");

        // 参数解析（token 式，向后兼容旧的位置式写法）：
        //   [body_index]            纯数字（仅第 4 个 token 位置生效）
        //   [assoc=1]               关联链接（默认 0 = 固定时间戳记/提取几何）
        //   [x=10] [y=5] [z=-3]     链入几何的摆放偏移（对链接体施加变换）
        int bodyIdx = 0;
        bool assoc = false;
        double placeX = 0.0, placeY = 0.0, placeZ = 0.0;
        for (int i = 3; i < parts.Length; i++)
        {
            string t = parts[i];
            if (t.StartsWith("assoc=", StringComparison.OrdinalIgnoreCase))
                assoc = (t.Substring(6) == "1");
            else if (t.StartsWith("x=", StringComparison.OrdinalIgnoreCase))
                placeX = D(t.Substring(2));
            else if (t.StartsWith("y=", StringComparison.OrdinalIgnoreCase))
                placeY = D(t.Substring(2));
            else if (t.StartsWith("z=", StringComparison.OrdinalIgnoreCase))
                placeZ = D(t.Substring(2));
            else if (i == 3 || (i == 4 && t == "1" && !assoc))
            {
                // i==3: 旧的 [body_index] 位置；i==4 的裸 "1": 旧的 [assoc=1] 位置
                if (!(i == 4 && t == "1")) { try { bodyIdx = int.Parse(t, CultureInfo.InvariantCulture); } catch { bodyIdx = 0; } }
                else assoc = true;
            }
        }
        bool extractAs = !assoc;
        bool hasPlacement = (placeX != 0.0 || placeY != 0.0 || placeZ != 0.0);

        try
        {
            // 1) 打开源件（主模型），作为装配根
            NXOpen.Part sourcePart = null;
            string want = NormPath(srcPath);
            foreach (NXOpen.BasePart bp in _session.Parts.ToArray())
            {
                string fp = "";
                try { fp = bp.FullPath; } catch { }
                if (string.IsNullOrEmpty(fp)) continue;
                string have = NormPath(fp);
                if (string.Equals(have, want, StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(Path.GetFileName(have), Path.GetFileName(want), StringComparison.OrdinalIgnoreCase))
                { sourcePart = bp as NXOpen.Part; break; }
            }
            if (sourcePart == null)
            {
                NXOpen.PartLoadStatus st = null;
                try { sourcePart = _session.Parts.OpenBaseDisplay(srcPath, out st) as NXOpen.Part; }
                catch (Exception e) { Log("wave link open source warn: " + e.Message); }
                finally { if (st != null) { try { st.Dispose(); } catch { } } }
            }
            if (sourcePart == null) return ErrJson("could not load source part: " + srcPath);

            // 2) 选源件要链接的实体（默认第一个实体）
            Body[] sbodies = sourcePart.Bodies.ToArray();
            if (sbodies.Length == 0) return ErrJson("source part has no bodies to link");
            Body sourceBody = null;
            if (bodyIdx >= 1 && bodyIdx <= sbodies.Length) sourceBody = sbodies[bodyIdx - 1];
            if (sourceBody == null)
            {
                foreach (Body b in sbodies) if (b.IsSolidBody) { sourceBody = b; break; }
                if (sourceBody == null) sourceBody = sbodies[0];
            }

            // 3) 显示先切到源件（装配根），保证后续 AddComponent 语义正确，
            //    也让"关闭会话中的旧目标件"安全执行
            NXOpen.PartLoadStatus st2 = null;
            try { _session.Parts.SetDisplay(sourcePart, false, false, out st2); }
            catch (Exception e) { Log("wave link setdisplay warn: " + e.Message); }
            finally { if (st2 != null) { try { st2.Dispose(); } catch { } } }

            // 3.5) ⚠️ 源装配里若已挂同名子组件（上次执行的残留——组件引用随
            //      源件保存而永久存在），目标件会以组件身份在会话里，
            //      NewDisplay 对它报"文件已存在"。先卸载这些实例。
            //      （UF_ASSEM_ask_component_data 的 csys 数组必须给 9 元素。）
            NXOpen.UF.UFSession uf = NXOpen.UF.UFSession.GetUFSession();
            try
            {
                NXOpen.Assemblies.Component[] kids =
                    sourcePart.ComponentAssembly.RootComponent.GetChildren();
                Log("wave link child-enum: " + ((kids == null) ? -1 : kids.Length) + " children under root");
                string wantLeaf = Path.GetFileName(tgtPath);
                foreach (NXOpen.Assemblies.Component c in kids)
                {
                    string partName = "", refset = "", inst = "";
                    double[] cOrigin = new double[3];
                    double[] cCsys = new double[9];   // 3x3 矩阵按行展开，必须 9 元素
                    double[,] cXform = new double[4, 4];
                    try
                    {
                        uf.Assem.AskComponentData(c.Tag, out partName, out refset,
                                                  out inst, cOrigin, cCsys, cXform);
                    }
                    catch (Exception ae)
                    {
                        // 坏组件（引用的子文件不存在等）常在这里抛——退回 DisplayName 匹配
                        Log("wave link ask-comp-data warn: " + c.DisplayName + " -> " + ae.Message);
                        partName = "";
                    }
                    bool same = false;
                    if (!string.IsNullOrEmpty(partName))
                        same = string.Equals(NormPath(partName), NormPath(tgtPath), StringComparison.OrdinalIgnoreCase)
                            || string.Equals(Path.GetFileName(partName), wantLeaf, StringComparison.OrdinalIgnoreCase);
                    if (!same)
                    {
                        // DisplayName 形如 "工序_place_A"，或含子件名——兜底比对
                        string dn = "";
                        try { dn = c.DisplayName ?? ""; } catch { }
                        same = (!string.IsNullOrEmpty(dn) &&
                                (dn.IndexOf(wantLeaf, StringComparison.OrdinalIgnoreCase) >= 0
                                 || wantLeaf.IndexOf(dn, StringComparison.OrdinalIgnoreCase) >= 0 && dn.Length > 3));
                    }
                    if (same)
                    {
                        try
                        {
                            uf.Assem.RemoveInstance(c.Tag);
                            Log("wave link removed stale component instance: " + (partName.Length > 0 ? partName : c.DisplayName));
                        }
                        catch (Exception re) { Log("wave link remove-instance warn: " + re.Message); }
                    }
                }
            }
            catch (Exception ce) { Log("wave link child-enum warn: " + ce.Message); }

            // 3.7) 卸载实例后，会话部件表里可能仍挂着同名幽灵部件
            //      （引用的子文件不存在，加载主模型时以占位形式注册）——关掉它们。
            foreach (NXOpen.BasePart bp in _session.Parts.ToArray())
            {
                string nm = "";
                try { nm = bp.Name ?? ""; } catch { }
                string fp = "";
                try { fp = bp.FullPath ?? ""; } catch { }
                string id = string.IsNullOrEmpty(fp) ? nm : fp;
                if (string.IsNullOrEmpty(id)) continue;
                if (id.IndexOf(Path.GetFileNameWithoutExtension(tgtPath), StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    try
                    {
                        bp.Close(NXOpen.BasePart.CloseWholeTree.False,
                                 NXOpen.BasePart.CloseModified.CloseModified, null);
                        Log("wave link closed phantom part: " + id);
                    }
                    catch (Exception ce2) { Log("wave link close-phantom warn: " + id + " -> " + ce2.Message); }
                }
            }

            // 4) 用 UF_ASSEM_create_component_part 直接创建子部件文件并挂为组件。
            //    ⚠️ 不用 NewDisplay：会话部件表里只要登记过同名部件（哪怕只是
            //    组件引用/上次残留），NewDisplay 就报"文件已存在"；
            //    CreateComponentPart 立即在磁盘上建子文件，无此问题。
            if (File.Exists(tgtPath)) { try { File.Delete(tgtPath); } catch (Exception de) { Log("wave link delete warn: " + de.Message); } }
            double[] cOrigin2 = { 0.0, 0.0, 0.0 };
            double[] cCsys2 = { 1.0, 0.0, 0.0, 0.0, 1.0, 0.0 };  // x 方向 + y 方向（6 元素）
            NXOpen.Tag compTag;
            // units：UF_PART_METRIC=1（uf_part_types.h:130；ENGLISH=2）。
            // 传错会造成"根部件与工作部件单位不一致"——实测踩坑。
            uf.Assem.CreateComponentPart(
                sourcePart.Tag, tgtPath, "", "", 1, 0,
                cOrigin2, cCsys2, 0, null, out compTag);
            Log("wave link component part created: comp=" + compTag + " path=" + tgtPath);
            try { _session.ApplicationSwitchImmediate("UG_APP_MODELING"); } catch { }

            // 5) 找到子部件并设为工作部件（下游组件）
            NXOpen.Part targetPart = null;
            NXOpen.NXObject compObj = (NXOpen.NXObject)NXOpen.Utilities.NXObjectManager.Get(compTag);
            NXOpen.Assemblies.Component comp = compObj as NXOpen.Assemblies.Component;
            if (comp != null)
            {
                try { targetPart = comp.Prototype as NXOpen.Part; } catch { }
            }
            if (targetPart == null)
            {
                // 兜底：按路径在会话部件表里找
                foreach (NXOpen.BasePart bp in _session.Parts.ToArray())
                {
                    string fp = "";
                    try { fp = bp.FullPath; } catch { }
                    if (!string.IsNullOrEmpty(fp) &&
                        string.Equals(NormPath(fp), NormPath(tgtPath), StringComparison.OrdinalIgnoreCase))
                    { targetPart = bp as NXOpen.Part; break; }
                }
            }
            if (targetPart == null) return ErrJson("created component but could not resolve its part: " + tgtPath);
            _session.Parts.SetWork(targetPart);

            // 6) 创建 WAVE 链接（固定时间戳记 / 提取几何，可选摆放变换）
            NXOpen.Tag linkedTag;
            // UF_WAVE_create_linked_body:
            //   extractAs=true  -> 固定时间戳记/提取几何（静态、非关联）
            //   extractAs=false -> 关联链接（随主模型更新）
            // 注意：NXOpen.UF 的 .NET 包装器签名是
            //   CreateLinkedBody(Tag body, Tag xform, Tag object_in_part, bool update_at_timestamp, out Tag linked_feature)
            // 与 C 版 UF_WAVE_create_linked_body(target, source, body, extract_as, out) 的参数顺序不同：
            //   body             = 要链接的源实体
            //   xform            = 变换（NULL_TAG 表示原位，不平移）
            //   object_in_part   = 接收链接实体的目标件
            //   update_at_timestamp = extract_as：true=固定时间戳记/提取几何（非关联静态），false=实时关联
            NXOpen.Tag xformTag = NXOpen.Tag.Null;
            if (hasPlacement)
            {
                // ⚠️ 摆放（x=/y=/z=）暂不开放，明确报错优于静默错位。
                //
                // 已走的路（代码保留在下方，翻转 PLACEMENT_ENABLED 即可启用）：
                //   1) UF_WAVE_create_linked_body 的 xform 参数：NXOpen.Xform 与
                //      UF_SO_create_xform_doubles 两种 xform 都报
                //      "传递的第二个参数无效"（NX12 .NET 包装器问题）；
                //   2) 链出后用 UF_MODL_transform_objects 平移链接体：可行，但
                //      4x4 矩阵的行/列主序两种写法未经几何验证（行主序实测把
                //      几何畸变了；列主序因共享管道环境反复中断，未完成验证）。
                //      验证方法：链两件（一个原位、一个 x=100 y=50），用
                //      nx_list_edges 按边长配对比对 midpoint 差应恰为 (100,50,0)。
                return ErrJson("placement (x=/y=/z=) is reserved and not enabled yet; use an in-place link (no x=/y=/z=)");
            }
            const bool PLACEMENT_ENABLED = false;
            if (PLACEMENT_ENABLED && hasPlacement && assoc)
            {
                // 摆放 + 关联链接：关联模式下平移会破坏链接语义，明确拒绝。
                return ErrJson("placement (x=/y=/z=) only supports the default fixed-timestamp link; drop assoc=1");
            }
            uf.Wave.CreateLinkedBody(sourceBody.Tag, xformTag, targetPart.Tag, extractAs, out linkedTag);

            // 摆放（仅提取模式）：对链接体直接施加平移矩阵。
            // UF_MODL_transform_objects 的 4x4 行主序矩阵，平移在第 4 列。
            if (hasPlacement)
            {
                NXOpen.NXObject linkObj = (NXOpen.NXObject)NXOpen.Utilities.NXObjectManager.Get(linkedTag);
                Body linkBody = linkObj as Body;
                if (linkBody == null)
                {
                    try
                    {
                        NXOpen.Features.Feature lf = linkObj as NXOpen.Features.Feature;
                        if (lf != null)
                        {
                            NXOpen.NXObject[] ents = lf.GetEntities();
                            foreach (NXOpen.NXObject e in ents)
                            {
                                Body b = e as Body;
                                if (b != null && b.IsSolidBody) { linkBody = b; break; }
                            }
                        }
                    }
                    catch { }
                }
                if (linkBody != null)
                {
                    // ⚠️ UF 矩阵为【列主序】4x4：平移分量在最后一行。
                    //    （行主序写法会把平移误当旋转轴，几何被畸变——实测踩坑）
                    double[] mx = {
                        1.0, 0.0, 0.0, 0.0,
                        0.0, 1.0, 0.0, 0.0,
                        0.0, 0.0, 1.0, 0.0,
                        placeX, placeY, placeZ, 1.0 };
                    uf.Modl.TransformEntities(1, new NXOpen.Tag[] { linkBody.Tag }, mx);
                    Log("wave link placement applied: " + F(placeX) + "," + F(placeY) + "," + F(placeZ));
                }
            }

            NXOpen.NXObject obj = (NXOpen.NXObject)NXOpen.Utilities.NXObjectManager.Get(linkedTag);
            Body linked = obj as Body;

            // 7) 登记链接实体，并把工作部件状态切到目标件
            _bodyCounter++;
            string newId = "BODY_" + _bodyCounter;
            if (linked != null) _bodies[newId] = linked;
            _part = targetPart;
            _partPath = targetPart.FullPath;
            RegisterExistingBodies(targetPart);

            string name = (linked != null) ? linked.Name : "(unknown)";
            string placeInfo = hasPlacement
                ? " placement=" + F(placeX) + "," + F(placeY) + "," + F(placeZ)
                : "";
            return OkJson("wave link",
                "body=" + newId + " tag=" + linkedTag.ToString() + " name=" + name +
                " source=" + Path.GetFileName(srcPath) + " target=" + Path.GetFileName(tgtPath) +
                " type=" + (assoc ? "associative" : "timestamp(extract)") + placeInfo,
                "created");
        }
        catch (Exception e)
        {
            Log("wave link error: " + e.ToString());
            return ErrJson("wave link failed: " + e.Message);
        }
    }

    private static string OpenPart(string[] parts)
    {
        if (parts.Length < 2) return ErrJson("usage: nx_open_part <path>");
        string path = parts[1];
        if (!File.Exists(path)) return ErrJson("part file not found: " + path);
        if (_session == null) _session = Session.GetSession();

        // [NX12 fix] Re-opening a part that is ALREADY loaded in this session made
        // NX throw "文件已存在", and every later call then reported stale data from
        // whatever part was still current. An agent routinely re-opens the same
        // drawing, so detect that case and simply switch to the loaded part.
        try
        {
            string want = NormPath(path);
            foreach (NXOpen.BasePart bp in _session.Parts.ToArray())
            {
                string fp = "";
                try { fp = bp.FullPath; } catch { }
                if (string.IsNullOrEmpty(fp)) continue;
                string have = NormPath(fp);

                // The path arriving over the pipe can mix '/' and '\' separators,
                // which makes a raw string comparison miss an already-open part.
                bool hit = string.Equals(have, want, StringComparison.OrdinalIgnoreCase);
                if (!hit)
                {
                    try
                    {
                        hit = string.Equals(
                            Path.GetFileName(have), Path.GetFileName(want),
                            StringComparison.OrdinalIgnoreCase);
                    }
                    catch { }
                }
                if (!hit) continue;

                NXOpen.PartLoadStatus st2 = null;
                try { _session.Parts.SetDisplay(bp, false, false, out st2); } catch { }
                finally { if (st2 != null) { try { st2.Dispose(); } catch { } } }

                NXOpen.Part p2 = _session.Parts.Work;
                if (p2 == null) p2 = bp as NXOpen.Part;
                if (p2 != null)
                {
                    _part = p2;
                    _partPath = p2.FullPath;
                    ResetTaskState();
                    RegisterExistingBodies(p2);
                    return OkJson("part", _partPath, "already open; switched to it");
                }
            }
        }
        catch { }

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
            RegisterExistingBodies(_part);
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

    // nx_create_section_view <parent_view_id> <x1> <y1> <x2> <y2> <pos_x> <pos_y> [k=v ...]
    // Simple (straight) section view via UFUN UF_DRAW_create_simple_sxview - the
    // .NET SectionLineBuilder route is deprecated on NX10 and its suggested
    // replacement is a nested type, i.e. a dead end (受阻项调研档案 §3).
    // The cut segment is a line created INSIDE the expanded parent view (a
    // member-view curve) at absolute drawing coordinates, associated through a
    // UFDrf.Object (the .NET wrapper of UF_DRF_object_t; assoc type DwgPos).
    // The new view is placed at (pos_x, pos_y) in drawing coordinates.
    // Optional key=value overrides:
    //   scale=<f>       section view scale (default 1.0)
    //   arrow=<dx,dy>   arrow (viewing) direction in drawing coords
    //                   (default: perpendicular to the cut line, left of x1->x2)
    //   step=<dx,dy>    step direction in drawing coords
    //                   (default: the cut line direction - parallel steps make
    //                   NX fail with "截面线零矢量错误")
    //   name=<token>    name for the created section view
    private static string CreateSectionView(string[] parts)
    {
        if (parts.Length < 8)
            return ErrJson("usage: nx_create_section_view <parent_view_id> <x1> <y1> <x2> <y2> <pos_x> <pos_y>"
                + " [scale=<f>] [arrow=<dx,dy>] [step=<dx,dy>] [name=<token>] (all coords are drawing coords)");
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part; run nx_create_part first");
            try { _session.ApplicationSwitchImmediate("UG_APP_DRAFTING"); }
            catch (Exception ea) { Log("app switch warn: " + ea.Message); }

            DrawingSheet sheet;
            DraftingView parent = ResolveViewAnySheet(parts[1].Trim(), out sheet);
            try { sheet.Open(); } catch { }

            double x1 = D(parts[2]), y1 = D(parts[3]);
            double x2 = D(parts[4]), y2 = D(parts[5]);
            double px = D(parts[6]), py = D(parts[7]);
            if (Math.Abs(x2 - x1) < 1e-9 && Math.Abs(y2 - y1) < 1e-9)
                return ErrJson("cut segment is degenerate: (x1,y1) == (x2,y2)");

            // Direction semantics (verified live 2026-11-05): step_dir = the cut
            // line direction, arrow_dir = the viewing direction perpendicular to
            // it. When both point the same way NX fails with "截面线零矢量错误"
            // (their cross product collapses) - that was the whole puzzle.
            double cdx = x2 - x1, cdy = y2 - y1;
            double cn = Math.Sqrt(cdx * cdx + cdy * cdy);
            double sdx = cdx / cn, sdy = cdy / cn;    // step: along the cut line
            double adx = -cdy / cn, ady = cdx / cn;   // arrow: perpendicular, left of x1->x2
            double scale = 1.0;
            string name = "";
            for (int i = 8; i < parts.Length; i++)
            {
                string tok = parts[i].Trim();
                int eq = tok.IndexOf('=');
                if (eq <= 0) continue;
                string k = tok.Substring(0, eq).ToLowerInvariant();
                string v = tok.Substring(eq + 1);
                if (k == "scale")
                {
                    scale = D(v);
                }
                else if (k == "arrow" || k == "step")
                {
                    string[] xy = v.Split(',');
                    if (xy.Length != 2)
                        return ErrJson(k + " must be <dx,dy>, got '" + v + "'");
                    double vx = D(xy[0].Trim()), vy = D(xy[1].Trim());
                    double vn = Math.Sqrt(vx * vx + vy * vy);
                    if (vn < 1e-9) return ErrJson(k + " direction is zero-length");
                    vx /= vn; vy /= vn;
                    if (k == "arrow") { adx = vx; ady = vy; }
                    else { sdx = vx; sdy = vy; }
                }
                else if (k == "name") { name = v; }
            }
            if (scale <= 0.0) return ErrJson("scale must be > 0");

            int before = 0;
            try { before = sheet.GetDraftingViews().Length; } catch { }

            // the cut segment must be a member-view curve of the parent view:
            // expand the view, draw the line at sheet coordinates, then collapse
            // via the application-switch round trip (NX12 has no deactivate call)
            bool activated = false;
            try
            {
                if (!parent.IsActiveForSketching) { parent.ActivateForSketching(); activated = true; }
            }
            catch (Exception ea) { Log("activate warn: " + ea.Message); }

            Tag sxTag = Tag.Null;
            string stage = "cut line";
            try
            {
                NXOpen.Line cutLine = _part.Curves.CreateLine(
                    new Point3d(x1, y1, 0.0), new Point3d(x2, y2, 0.0));
                if (cutLine == null)
                    return ErrJson("failed to create the cut segment line inside the parent view");

                stage = "CreateSimpleSxview";
                var cutObj = new NXOpen.UF.UFDrf.Object();
                cutObj.object_tag = cutLine.Tag;
                cutObj.object_view_tag = parent.Tag;
                cutObj.object_assoc_type = NXOpen.UF.UFDrf.AssocType.DwgPos;
                cutObj.object_assoc_modifier = 0;
                cutObj.object2_tag = Tag.Null;
                cutObj.assoc_dwg_pos = new double[] { (x1 + x2) / 2.0, (y1 + y2) / 2.0 };

                NXOpen.UF.UFSession uf = NXOpen.UF.UFSession.GetUFSession();
                // the .NET wrapper signature is actually strongly typed:
                // CreateSimpleSxview(..., ref UFDrf.Object cut_object, double[] place, out Tag)
                uf.Draw.CreateSimpleSxview(sheet.Tag, scale,
                    new double[] { sdx, sdy, 0.0 }, new double[] { adx, ady, 0.0 },
                    parent.Tag, ref cutObj, new double[] { px, py }, out sxTag);
            }
            catch (Exception ex)
            {
                return ErrJson("create_section_view failed at " + stage + ": " + ex.Message);
            }
            finally
            {
                if (activated)
                {
                    try
                    {
                        _session.ApplicationSwitchImmediate("UG_APP_MODELING");
                        _session.ApplicationSwitchImmediate("UG_APP_DRAFTING");
                    }
                    catch (Exception ea) { Log("app switch back warn: " + ea.Message); }
                }
            }

            if (sxTag == Tag.Null)
                return ErrJson("section view returned a null view tag");
            DraftingView created = null;
            try { created = NXOpen.Utilities.NXObjectManager.Get(sxTag) as DraftingView; }
            catch { }
            if (created == null)
            {
                DraftingView[] viewsNow = sheet.GetDraftingViews();
                if (viewsNow.Length > before) created = viewsNow[viewsNow.Length - 1];
            }
            if (created == null)
                return ErrJson("section view tag did not resolve to a DraftingView");
            if (!string.IsNullOrEmpty(name)) { try { created.SetName(name); } catch { } }
            try { ForceUpdateSheet(sheet); } catch { }

            string id = ""; try { id = created.JournalIdentifier; } catch { }
            int count = 0;
            try { count = sheet.GetDraftingViews().Length; } catch { }
            return "{\"ok\":true,\"view\":\"" + Esc(id)
                + "\",\"result\":\"" + Esc("section view created views=" + count
                    + " (scale " + F(scale) + ", parent '" + parent.Name + "')")
                + "\"}";
        }
        catch (Exception e)
        {
            return ErrJson("create_section_view failed: " + e.Message);
        }
    }

    // nx_add_section_segment <section_view_id> <x1> <y1> <x2> <y2>
    // Stepped section (阶梯剖): adds one more CUT segment to an existing simple
    // section view's section line. NX 12's UF wrapper has no
    // CreateSteppedSxview, but AskSxlineOfSxview + AddSxlineSxseg achieve it:
    // "Adds a segment to a section line, then updates all of its associated
    // section views". The bend between the two cuts is system-generated (same
    // rule the C docs give for create_stepped_sxview). The new cut line is a
    // member-view curve of the PARENT view (retrieved via AskSimpleSxline's
    // pview output), drawn at sheet coordinates like the first cut.
    private static string AddSectionSegment(string[] parts)
    {
        if (parts.Length < 6)
            return ErrJson("usage: nx_add_section_segment <section_view_id> <x1> <y1> <x2> <y2>"
                + " (drawing coordinates)");
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part; run nx_create_part first");
            try { _session.ApplicationSwitchImmediate("UG_APP_DRAFTING"); }
            catch (Exception ea) { Log("app switch warn: " + ea.Message); }

            DrawingSheet sheet;
            DraftingView sxview = ResolveViewAnySheet(parts[1].Trim(), out sheet);
            try { sheet.Open(); } catch { }

            double x1 = D(parts[2]), y1 = D(parts[3]);
            double x2 = D(parts[4]), y2 = D(parts[5]);
            if (Math.Abs(x2 - x1) < 1e-9 && Math.Abs(y2 - y1) < 1e-9)
                return ErrJson("cut segment is degenerate: (x1,y1) == (x2,y2)");

            NXOpen.UF.UFSession uf = NXOpen.UF.UFSession.GetUFSession();

            Tag sxline = Tag.Null;
            try { uf.Draw.AskSxlineOfSxview(sxview.Tag, out sxline); }
            catch (Exception es)
            {
                return ErrJson("view '" + sxview.Name + "' has no section line (not a section view?): " + es.Message);
            }
            if (sxline == Tag.Null)
                return ErrJson("AskSxlineOfSxview returned a null section line");

            // parent view from the simple-sxline query (first Tag output after
            // the two direction arrays; per uf_draw.h UF_DRAW_ask_simple_sxline)
            Tag pviewTag = Tag.Null;
            try
            {
                double[] cutDir = new double[3];
                double[] arrowDir = new double[3];
                int numSegs = 0, numArrows = 0;
                Tag[] segs = null, arrows = null;
                NXOpen.UF.UFDraw.SxlineStatus st;
                uf.Draw.AskSimpleSxline(sxline, cutDir, arrowDir,
                    out pviewTag, out numSegs, out segs, out numArrows, out arrows, out st);
            }
            catch (Exception eq) { Log("ask simple sxline warn: " + eq.Message); }
            if (pviewTag == Tag.Null)
                return ErrJson("could not read the parent view of the section line"
                    + " (revolved/stepped lines are not simple; use their own tools)");
            DraftingView parent = NXOpen.Utilities.NXObjectManager.Get(pviewTag) as DraftingView;
            if (parent == null) return ErrJson("section line parent did not resolve to a DraftingView");

            bool activated = false;
            try
            {
                if (!parent.IsActiveForSketching) { parent.ActivateForSketching(); activated = true; }
            }
            catch (Exception ea) { Log("activate warn: " + ea.Message); }

            Tag segTag = Tag.Null;
            string stage = "cut line";
            try
            {
                NXOpen.Line cutLine = _part.Curves.CreateLine(
                    new Point3d(x1, y1, 0.0), new Point3d(x2, y2, 0.0));
                if (cutLine == null)
                    return ErrJson("failed to create the cut segment line inside the parent view");

                stage = "AddSxlineSxseg";
                var cutObj = new NXOpen.UF.UFDrf.Object();
                cutObj.object_tag = cutLine.Tag;
                cutObj.object_view_tag = parent.Tag;
                cutObj.object_assoc_type = NXOpen.UF.UFDrf.AssocType.DwgPos;
                cutObj.object_assoc_modifier = 0;
                cutObj.object2_tag = Tag.Null;
                cutObj.assoc_dwg_pos = new double[] { (x1 + x2) / 2.0, (y1 + y2) / 2.0 };
                uf.Draw.AddSxlineSxseg(sxline, NXOpen.UF.UFDraw.SxsegType.SxsegCut,
                    NXOpen.UF.UFDraw.SxlineLeg.SxlineLeg1, ref cutObj, out segTag);
            }
            catch (Exception ex)
            {
                return ErrJson("add_section_segment failed at " + stage + ": " + ex.Message);
            }
            finally
            {
                if (activated)
                {
                    try
                    {
                        _session.ApplicationSwitchImmediate("UG_APP_MODELING");
                        _session.ApplicationSwitchImmediate("UG_APP_DRAFTING");
                    }
                    catch (Exception ea) { Log("app switch back warn: " + ea.Message); }
                }
            }
            if (segTag == Tag.Null)
                return ErrJson("AddSxlineSxseg returned a null segment tag");
            try { ForceUpdateSheet(sheet); } catch { }
            return OkJson("segment", segTag.ToString(),
                "cut segment added to section line of '" + sxview.Name
                + "' (bend system-generated; check the updated view)");
        }
        catch (Exception e)
        {
            return ErrJson("add_section_segment failed: " + e.Message);
        }
    }

    // nx_create_auxiliary_view <parent_view_id> <hx1> <hy1> <hx2> <hy2> <px> <py>
    // Auxiliary view (辅助视图) via UFDraw.AddAuxiliaryView(drawing, parent,
    // hinge_line, ref_xy[2], out view). The docs demand a "smart hinge line";
    // we first try a plain member-view line drawn in the expanded parent (the
    // route that worked for section cuts) - if NX rejects it with the smart
    // object error, the command reports it and the route stays experimental.
    private static string CreateAuxiliaryView(string[] parts)
    {
        if (parts.Length < 8)
            return ErrJson("usage: nx_create_auxiliary_view <parent_view_id> <hx1> <hy1> <hx2> <hy2> <px> <py>"
                + " (hinge line + placement, all drawing coordinates)");
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part; run nx_create_part first");
            try { _session.ApplicationSwitchImmediate("UG_APP_DRAFTING"); }
            catch (Exception ea) { Log("app switch warn: " + ea.Message); }

            DrawingSheet sheet;
            DraftingView parent = ResolveViewAnySheet(parts[1].Trim(), out sheet);
            try { sheet.Open(); } catch { }

            double x1 = D(parts[2]), y1 = D(parts[3]);
            double x2 = D(parts[4]), y2 = D(parts[5]);
            double px = D(parts[6]), py = D(parts[7]);
            if (Math.Abs(x2 - x1) < 1e-9 && Math.Abs(y2 - y1) < 1e-9)
                return ErrJson("hinge line is degenerate: (x1,y1) == (x2,y2)");

            int before = 0;
            try { before = sheet.GetDraftingViews().Length; } catch { }

            bool activated = false;
            try
            {
                if (!parent.IsActiveForSketching) { parent.ActivateForSketching(); activated = true; }
            }
            catch (Exception ea) { Log("activate warn: " + ea.Message); }

            Tag auxTag = Tag.Null;
            string stage = "hinge line";
            try
            {
                NXOpen.Line hinge = _part.Curves.CreateLine(
                    new Point3d(x1, y1, 0.0), new Point3d(x2, y2, 0.0));
                if (hinge == null)
                    return ErrJson("failed to create the hinge line inside the parent view");
                stage = "AddAuxiliaryView";
                NXOpen.UF.UFSession uf = NXOpen.UF.UFSession.GetUFSession();
                uf.Draw.AddAuxiliaryView(sheet.Tag, parent.Tag, hinge.Tag,
                    new double[] { px, py }, out auxTag);
            }
            catch (Exception ex)
            {
                return ErrJson("create_auxiliary_view failed at " + stage + ": " + ex.Message);
            }
            finally
            {
                if (activated)
                {
                    try
                    {
                        _session.ApplicationSwitchImmediate("UG_APP_MODELING");
                        _session.ApplicationSwitchImmediate("UG_APP_DRAFTING");
                    }
                    catch (Exception ea) { Log("app switch back warn: " + ea.Message); }
                }
            }
            if (auxTag == Tag.Null)
                return ErrJson("auxiliary view returned a null view tag");
            DraftingView created = null;
            try { created = NXOpen.Utilities.NXObjectManager.Get(auxTag) as DraftingView; } catch { }
            if (created == null)
            {
                DraftingView[] now = sheet.GetDraftingViews();
                if (now.Length > before) created = now[now.Length - 1];
            }
            if (created == null)
                return ErrJson("auxiliary view tag did not resolve to a DraftingView");
            try { ForceUpdateSheet(sheet); } catch { }
            string id = ""; try { id = created.JournalIdentifier; } catch { }
            int count = 0;
            try { count = sheet.GetDraftingViews().Length; } catch { }
            return "{\"ok\":true,\"view\":\"" + Esc(id)
                + "\",\"result\":\"" + Esc("auxiliary view created views=" + count)
                + "\"}";
        }
        catch (Exception e)
        {
            return ErrJson("create_auxiliary_view failed: " + e.Message);
        }
    }

    // nx_create_id_symbol <type> <x> <y> [size=k] [lower=<token>] [upper text...]
    // ID symbol (球标) - the part-number balloon of assembly process drawings.
    // type: circle|divcir|tri|triup|square|divsqr|hexagon|divhex|quadrant|roundbox|underline
    // Position is sheet coordinates (mm). upper text = all trailing tokens
    // (spaces allowed), lower text via lower=<token> (single token).
    // NXOpen IdSymbolBuilder route (same pattern as CreateRoughness).
    private static string CreateIdSymbol(string[] parts)
    {
        if (parts.Length < 4)
            return ErrJson("usage: nx_create_id_symbol <type> <x> <y> [size=k] [lower=<token>] [upper text...]");
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part; run nx_create_part first");
            try { _session.ApplicationSwitchImmediate("UG_APP_DRAFTING"); }
            catch (Exception ea) { Log("app switch warn: " + ea.Message); }

            string typeTok = parts[1].Trim().ToLowerInvariant();
            NXOpen.Annotations.IdSymbolBuilder.SymbolTypes st;
            switch (typeTok)
            {
                case "circle": st = NXOpen.Annotations.IdSymbolBuilder.SymbolTypes.Circle; break;
                case "divcir": st = NXOpen.Annotations.IdSymbolBuilder.SymbolTypes.DividedCircle; break;
                case "tri": st = NXOpen.Annotations.IdSymbolBuilder.SymbolTypes.Triangle; break;
                case "triup": st = NXOpen.Annotations.IdSymbolBuilder.SymbolTypes.TriangleUp; break;
                case "square": st = NXOpen.Annotations.IdSymbolBuilder.SymbolTypes.Square; break;
                case "divsqr": st = NXOpen.Annotations.IdSymbolBuilder.SymbolTypes.DividedSquare; break;
                case "hexagon": st = NXOpen.Annotations.IdSymbolBuilder.SymbolTypes.Hexagon; break;
                case "divhex": st = NXOpen.Annotations.IdSymbolBuilder.SymbolTypes.DividedHexagon; break;
                case "quadrant": st = NXOpen.Annotations.IdSymbolBuilder.SymbolTypes.QuadrantCircle; break;
                case "roundbox": st = NXOpen.Annotations.IdSymbolBuilder.SymbolTypes.RoundedBox; break;
                case "underline": st = NXOpen.Annotations.IdSymbolBuilder.SymbolTypes.Underline; break;
                default:
                    return ErrJson("type must be circle|divcir|tri|triup|square|divsqr|hexagon|divhex|quadrant|roundbox|underline, got '" + typeTok + "'");
            }

            double posx = D(parts[2]);
            double posy = D(parts[3]);
            double size = 10.0;
            string lower = "";
            var upper = new List<string>();
            for (int i = 4; i < parts.Length; i++)
            {
                string tok = parts[i];
                if (tok.StartsWith("size=", StringComparison.OrdinalIgnoreCase) && tok.Length > 5)
                {
                    size = D(tok.Substring(5));
                }
                else if (tok.StartsWith("lower=", StringComparison.OrdinalIgnoreCase))
                {
                    lower = tok.Substring(6);
                }
                else
                {
                    upper.Add(tok);
                }
            }
            string upperText = string.Join(" ", upper.ToArray()).Trim();

            int before = 0;
            try { before = _part.Annotations.IdSymbols.ToArray().Length; } catch { }

            NXOpen.Annotations.IdSymbolBuilder ib =
                _part.Annotations.IdSymbols.CreateIdSymbolBuilder(null);
            string jid = "";
            try
            {
                ib.Type = st;
                ib.Size = size;
                ib.Origin.OriginPoint = new Point3d(posx, posy, 0.0);
                if (upperText.Length > 0) ib.UpperText = upperText;
                if (lower.Length > 0) ib.LowerText = lower;
                NXOpen.Annotations.IdSymbol sym = (NXOpen.Annotations.IdSymbol)ib.Commit();
                try { sym.SetName("MCP_ID_" + typeTok.ToUpperInvariant()); } catch { }
                try { jid = sym.JournalIdentifier; } catch { }
            }
            finally { try { ib.Destroy(); } catch { } }

            int after = 0;
            try { after = _part.Annotations.IdSymbols.ToArray().Length; } catch { }
            if (after <= before)
                return ErrJson("id symbol created nothing (id symbols " + before + " -> " + after + ")");
            try { _part.DrawingSheets.RefreshCurrentSheet(); } catch { }
            return OkJson("id_symbol", jid,
                "id symbol '" + typeTok + "' created (id symbols " + before + " -> " + after
                + (upperText.Length > 0 ? ", text '" + upperText + "'" : "")
                + (lower.Length > 0 ? "/" + lower : "") + ")");
        }
        catch (Exception e)
        {
            return ErrJson("create_id_symbol failed: " + e.Message);
        }
    }

    // nx_create_datum_symbol <letter> <x> <y>
    // Datum feature symbol (基准符号) - the GD&T datum-letter frame of process
    // drawings (GB/T 17851 / ISO 5459). Entry is
    // GdtDatumCollection.CreateDraftingDatumFeatureSymbolBuilder(null) - there
    // is no factory on AnnotationManager; found by reflection. Letter is the
    // datum letter (uppercased), position is sheet coordinates (mm).
    // Placement is standalone (no leader/attachment); the frame renders where
    // it is put, and NX verifies the datum count via GdtDatumCollection.
    private static string CreateDatumSymbol(string[] parts)
    {
        if (parts.Length < 4)
            return ErrJson("usage: nx_create_datum_symbol <letter> <x> <y>");
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part; run nx_create_part first");
            try { _session.ApplicationSwitchImmediate("UG_APP_DRAFTING"); }
            catch (Exception ea) { Log("app switch warn: " + ea.Message); }

            string letter = parts[1].Trim().ToUpperInvariant();
            if (letter.Length == 0 || letter.Length > 2)
                return ErrJson("datum letter must be 1-2 characters, got '" + letter + "'");
            double posx = D(parts[2]);
            double posy = D(parts[3]);

            // DraftingDatum : BaseDatum : Gdt, so the part-level Gdt collection
            // (_part.Gdts) is the counter that moves; Annotations.Datums did NOT
            // change (0 -> 0) even when the commit produced a real object.
            int before = 0;
            try { before = _part.Gdts.ToArray().Length; } catch { }

            NXOpen.Annotations.DraftingDatumFeatureSymbolBuilder db =
                _part.Annotations.Datums.CreateDraftingDatumFeatureSymbolBuilder(null);
            string jid = "";
            try
            {
                db.Letter = letter;
                db.Origin.OriginPoint = new Point3d(posx, posy, 0.0);
                NXOpen.Annotations.DraftingDatum sym =
                    (NXOpen.Annotations.DraftingDatum)db.Commit();
                if (sym != null)
                {
                    try { sym.SetName("MCP_DATUM_" + letter); } catch { }
                    try { jid = sym.JournalIdentifier; } catch { }
                }
            }
            finally { try { db.Destroy(); } catch { } }

            int after = 0;
            try { after = _part.Gdts.ToArray().Length; } catch { }
            if (string.IsNullOrEmpty(jid) && after <= before)
                return ErrJson("datum symbol created nothing (gdts "
                    + before + " -> " + after + ", committed object null or untracked)");
            try { _part.DrawingSheets.RefreshCurrentSheet(); } catch { }
            return OkJson("datum_symbol", jid,
                "datum '" + letter + "' created (gdts " + before + " -> " + after + ")");
        }
        catch (Exception e)
        {
            return ErrJson("create_datum_symbol failed: " + e.Message);
        }
    }

    // nx_delete_view <view_id>
    // Deletes a drafting view from its sheet. NX 12 .NET has no
    // DraftingView.Delete(); the GTAC route is UF_OBJ_delete_object on the view
    // tag. For section views SetDeleteSectionLine(true) also removes the
    // section line. Deletion is verified by the view count dropping.
    private static string DeleteViewCmd(string[] parts)
    {
        if (parts.Length < 2)
            return ErrJson("usage: nx_delete_view <view_id>");
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part; run nx_create_part first");

            string token = parts[1].Trim();
            DrawingSheet sheet;
            DraftingView v = ResolveViewAnySheet(token, out sheet);
            string nm = "", ji = "";
            try { nm = v.Name; } catch { }
            try { ji = v.JournalIdentifier; } catch { }
            try { sheet.Open(); } catch { }

            int before = 0;
            try { before = sheet.GetDraftingViews().Length; } catch { }

            try { v.SetDeleteSectionLine(true); }
            catch (Exception es) { Log("set delete-section-line warn: " + es.Message); }

            NXOpen.UF.UFSession uf = NXOpen.UF.UFSession.GetUFSession();
            uf.Obj.DeleteObject(v.Tag);

            // a zero return does not guarantee deletion - verify by count + id
            int after = before;
            bool stillThere = false;
            try
            {
                DraftingView[] views = sheet.GetDraftingViews();
                after = views.Length;
                foreach (DraftingView w in views)
                {
                    string wj = ""; try { wj = w.JournalIdentifier; } catch { }
                    if (ji.Length > 0 && wj == ji) { stillThere = true; break; }
                }
            }
            catch (Exception ev) { Log("verify delete warn: " + ev.Message); }
            if (stillThere || after >= before)
                return ErrJson("view '" + nm + "' was not deleted (views " + before
                    + " -> " + after + "); it may be a dependent base/section view");
            return OkJson("deleted", ji,
                "drafting view '" + nm + "' deleted (views " + before + " -> " + after + ")");
        }
        catch (Exception e)
        {
            return ErrJson("delete_view failed: " + e.Message);
        }
    }

    // nx_list_assembly
    // Flattens the component assembly tree of the active part - the data source
    // for BOM / 明细表 / process routing tables. One JSON row per component
    // INSTANCE: {level, name, display, part, children}; 'part' is the child
    // part file path when the component is loaded in session.
    // Each row also carries "qty" (total instances of the same prototype part
    // across the whole assembly) and "attrs" (the prototype part's user
    // attributes - material / drawing number for the BOM). qty comes from a
    // pre-pass (CountParts); attrs reads the prototype part only when loaded.
    private static void CountParts(NXOpen.Assemblies.Component comp,
        Dictionary<string, int> counts)
    {
        NXOpen.Assemblies.Component[] children = null;
        try { children = comp.GetChildren(); }
        catch { return; } // broken component tree - nothing to count here
        if (children == null) return;
        foreach (NXOpen.Assemblies.Component child in children)
        {
            string p = "";
            try
            {
                NXOpen.BasePart proto = child.Prototype as NXOpen.BasePart;
                if (proto != null) p = proto.FullPath;
            }
            catch { }
            if (p.Length > 0)
            {
                if (!counts.ContainsKey(p)) counts[p] = 0;
                counts[p] = counts[p] + 1;
            }
            CountParts(child, counts);
        }
    }

    private static string PartAttributesJson(NXOpen.BasePart proto)
    {
        // String-valued user attributes of the prototype part (material,
        // drawing number...). Non-string / empty values are skipped; the part
        // must be loaded in session or this returns "{}".
        var asb = new StringBuilder();
        asb.Append("{");
        try
        {
            NXOpen.NXObject.AttributeInformation[] infos = proto.GetUserAttributes();
            bool first = true;
            foreach (NXOpen.NXObject.AttributeInformation ai in infos)
            {
                if (ai.StringValue == null || ai.StringValue.Length == 0) continue;
                if (!first) asb.Append(",");
                first = false;
                asb.Append("\"").Append(Esc(ai.Title)).Append("\":\"")
                    .Append(Esc(ai.StringValue)).Append("\"");
            }
        }
        catch { }
        asb.Append("}");
        return asb.ToString();
    }

    private static void WalkComponent(NXOpen.Assemblies.Component comp, int level,
        List<string> rows, ref int total, Dictionary<string, int> qty)
    {
        NXOpen.Assemblies.Component[] children;
        try { children = comp.GetChildren(); }
        catch (Exception gc)
        {
            // broken component (e.g. dangling instance after a failed wave
            // link) - report it as a row instead of killing the whole listing
            Log("walk component warn: " + gc.Message);
            if (rows.Count < 5000)
            {
                var eb = new StringBuilder();
                eb.Append("{\"level\":").Append(level);
                eb.Append(",\"name\":\"<broken>\"");
                eb.Append(",\"display\":\"").Append(Esc(gc.Message)).Append("\"");
                eb.Append(",\"part\":\"\",\"children\":0,\"qty\":1,\"attrs\":{}}");
                rows.Add(eb.ToString());
            }
            return;
        }
        foreach (NXOpen.Assemblies.Component child in children)
        {
            total++;
            if (rows.Count < 5000)
            {
                string nm = "", disp = "", part = "";
                int kids = 0, n = 1;
                try { nm = child.Name; } catch { }
                try { disp = child.DisplayName; } catch { }
                try { kids = child.GetChildren().Length; } catch { }
                NXOpen.BasePart proto = null;
                try
                {
                    proto = child.Prototype as NXOpen.BasePart;
                    if (proto != null) part = proto.FullPath;
                }
                catch { }
                if (part.Length > 0 && qty.ContainsKey(part)) n = qty[part];
                var sb = new StringBuilder();
                sb.Append("{\"level\":").Append(level);
                sb.Append(",\"name\":\"").Append(Esc(nm)).Append("\"");
                sb.Append(",\"display\":\"").Append(Esc(disp)).Append("\"");
                sb.Append(",\"part\":\"").Append(Esc(part)).Append("\"");
                sb.Append(",\"children\":").Append(kids);
                sb.Append(",\"qty\":").Append(n);
                sb.Append(",\"attrs\":")
                  .Append(proto != null ? PartAttributesJson(proto) : "{}");
                sb.Append("}");
                rows.Add(sb.ToString());
            }
            WalkComponent(child, level + 1, rows, ref total, qty);
        }
    }

    private static string ListAssembly(string[] parts)
    {
        try
        {
            _session = Session.GetSession();
            if (_part == null) return ErrJson("no active part; run nx_create_part first");
            var rows = new List<string>();
            int total = 0;
            NXOpen.Assemblies.Component root = _part.ComponentAssembly.RootComponent;
            if (root == null)
                return OkJsonList("rows", rows, "part has no components (not an assembly)");
            var qty = new Dictionary<string, int>();
            CountParts(root, qty);
            WalkComponent(root, 1, rows, ref total, qty);
            var sb = new StringBuilder();
            sb.Append("{\"ok\":true,\"rows\":[");
            for (int i = 0; i < rows.Count; i++)
            {
                if (i > 0) sb.Append(",");
                sb.Append(rows[i]);
            }
            sb.Append("],\"count\":").Append(total);
            sb.Append(",\"result\":\"").Append(Esc("assembly has " + total + " component instance(s)"));
            sb.Append("\"}");
            return sb.ToString();
        }
        catch (Exception e)
        {
            return ErrJson("list_assembly failed: " + e.Message);
        }
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
