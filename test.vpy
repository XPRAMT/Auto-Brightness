import vapoursynth as vs
import socket as _socket
import struct as _struct

core = vs.core
core.num_threads = 15

# UDP 目標：brightness.pyw 監聽的 port（可改）
_UDP_HOST = "127.0.0.1"
_UDP_PORT = 57321
_udp_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)

# 載入 SVP 插件（沿用 b0db803.py）
if not hasattr(core, "svp1"):
    core.std.LoadPlugin(r"C:\Program Files (x86)\SVP 4\plugins64\svpflow1_vs.dll")
if not hasattr(core, "svp2"):
    core.std.LoadPlugin(r"C:\Program Files (x86)\SVP 4\plugins64\svpflow2_vs.dll")

# ===== SVP 參數 =====
super_params = "{scale:{up:2},gpu:0,rc:true}"
analyse_params = "{block:{w:32,overlap:0},main:{search:{coarse:{satd:false,type:2,distance:-6,width:260},type:2}},refine:[{thsad:250}]}"

# 自動計算補幀倍率，目標 120fps
def get_smoothfps_params(clip, target_fps=120):
    clip_fps = float(clip.fps_num) / float(clip.fps_den)
    rate_num = max(1, int(round(target_fps / clip_fps)))
    return f"{{rate:{{num:{rate_num},den:1}},algo:13,mask:{{area:50}},scene:{{adaptive:310}}}}"

smoothfps_params = get_smoothfps_params

src = VpsFilterSource.std.Trim(length=5000000)
demo_mode = 0
stereo_type = 0
nvof = 0
rife = 0



def interpolate(clip):
    input_um = clip.resize.Point(format=vs.YUV420P8, dither_type="random")
    input_m = input_um
    input_m8 = input_m

    if nvof:
        fps = float(clip.fps_num) / float(clip.fps_den)
        params = smoothfps_params(clip)
        smooth = core.svp2.SmoothFps_NVOF(input_m, params, vec_src=input_m8, src=input_um, fps=fps)
    elif not rife:
        super_clip = core.svp1.Super(input_m8, super_params)
        vectors = core.svp1.Analyse(super_clip["clip"], super_clip["data"], input_m8, analyse_params)
        fps = float(clip.fps_num) / float(clip.fps_den)
        params = smoothfps_params(clip)
        smooth = core.svp2.SmoothFps(
            input_m,
            super_clip["clip"],
            super_clip["data"],
            vectors["clip"],
            vectors["data"],
            params,
            src=input_um,
            fps=fps,
        )
    else:
        smooth = input_m

    if demo_mode == 1:
        return smooth
    return smooth




# ===== 亮度計算 + UDP 發送（補幀前：原始幀） =====
src_y = core.std.ShufflePlanes(src, 0, vs.GRAY)
src_stats = core.std.PlaneStats(src_y)


def on_src_frame(n, f):
    # clips=[src, src_stats]，f[0] 是原始幀，f[1] 是統計幀
    src_frame = f[0] if isinstance(f, (list, tuple)) else f
    stat_frame = f[1] if isinstance(f, (list, tuple)) and len(f) > 1 else f
    avg = float(stat_frame.props.get("PlaneStatsAverage", 0.0)) * 100.0
    try:
        _udp_sock.sendto(_struct.pack("f", avg), (_UDP_HOST, _UDP_PORT))
    except Exception:
        pass
    return src_frame


src_for_svp = core.std.ModifyFrame(src, clips=[src, src_stats], selector=on_src_frame)

if stereo_type == 1:
    lf = interpolate(core.std.CropRel(src_for_svp, 0, int(src_for_svp.width / 2), 0, 0))
    rf = interpolate(core.std.CropRel(src_for_svp, int(src_for_svp.width / 2), 0, 0, 0))
    svp_out = core.std.StackHorizontal([lf, rf])
elif stereo_type == 2:
    lf = interpolate(core.std.CropRel(src_for_svp, 0, 0, 0, int(src_for_svp.height / 2)))
    rf = interpolate(core.std.CropRel(src_for_svp, 0, 0, int(src_for_svp.height / 2), 0))
    svp_out = core.std.StackVertical([lf, rf])
else:
    svp_out = interpolate(src_for_svp)


y = core.std.ShufflePlanes(svp_out, 0, vs.GRAY)
stats = core.std.PlaneStats(y)


def overlay_status(n, f):
    stat_frame = f[0] if isinstance(f, (list, tuple)) else f
    avg = float(stat_frame.props.get("PlaneStatsAverage", 0.0)) * 100.0
    # 原始幀率
    src_fps = float(src.fps_num) / float(src.fps_den)
    # 補幀前（進 SVP 前）幀率
    # src_for_svp 可能有快進/慢放等前處理
    clip_fps = float(src_for_svp.fps_num) / float(src_for_svp.fps_den)
    # 補幀後（SVP 輸出）幀率
    svp_fps = float(svp_out.fps_num) / float(svp_out.fps_den)
    status_text = f"{avg:.1f}% {src_fps:.3f}({clip_fps:.3f}) -> {svp_fps:.3f}"
    return core.text.Text(svp_out, text=status_text, alignment=7)

out = core.std.FrameEval(svp_out, eval=overlay_status, prop_src=[stats])

out.set_output()