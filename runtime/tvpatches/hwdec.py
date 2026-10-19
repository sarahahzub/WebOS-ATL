#!/usr/bin/env python3
"""Hardware video decode on webOS (ATL_HWDEC=1): MediaCodec h264/hevc video goes to the 32-bit `atlvdec` helper
(libNDL_directmedia -> TV video plane) instead of FFmpeg; the SurfaceView punches a transparent hole.
 - Used only if ATL_HWDEC=1 and the codec is h264/hevc and a Surface is given and ATL_HWDEC_SOCK connects and
   the helper's OPEN reply is OK; otherwise the unchanged FFmpeg path (bench, missing helper/NDL).
 - No decoder output exists in ATL: queueInputBuffer keeps Annex-B AUs (csd prepended to the first AU and after
   each flush) in decode order plus a pts reorder window (depth 4); dequeueOutputBuffer hands out 8-byte "token"
   buffers in pts order; releaseOutputBuffer (render or not) sends all pending AUs up to the token's decode index
   (at least one) -> the hardware gets the stream at ExoPlayer's presentation pace (audio clock keeps A/V sync).
   Back-pressure: queueInputBuffer returns non-zero while > 24 AUs or > 8 MB are pending (Java retries).
 - flush -> FLUSH (helper reloads), release -> CLOSE; helper death -> logged once, video dropped (no hang).
 - SurfaceView rect (scaled to the 1920x1080 plane) -> AREA, rechecked every 500 ms on the main thread.
 - MediaCodecList gains hevc, AVC High / HEVC Main(10) profile levels - only with ATL_HWDEC=1 (default unchanged).
 - ATL_HWDEC_DEBUG=1 logs every message.
usage: hwdec.py <atl src root>"""
import os, sys
R = sys.argv[1]
A = os.path.join(R, "src/api-impl/android")
J = os.path.join(R, "src/api-impl-jni")


def edit(rel, old, new, base=A, count=1):
    p = os.path.join(base, rel)
    s = open(p).read()
    if new in s:
        return
    assert old in s, f"{rel}: anchor not found: {old[:60]!r}"
    open(p, "w").write(s.replace(old, new, count))


# ------------------------------------------------------------------ Java
edit("media/MediaCodecList.java", "\tpublic static int getCodecCount() {\n\t\treturn 6;\n\t}",
     "\tstatic boolean hwdec() {\n\t\treturn \"1\".equals(System.getenv(\"ATL_HWDEC\"));\n\t}\n\n"
     "\tpublic static int getCodecCount() {\n\t\treturn hwdec() ? 7 : 6;\n\t}")
edit("media/MediaCodecList.java", "\t\t\tcase 5:\n\t\t\t\treturn new MediaCodecInfo(\"vp9\", \"video/x-vnd.on2.vp9\");\n",
     "\t\t\tcase 5:\n\t\t\t\treturn new MediaCodecInfo(\"vp9\", \"video/x-vnd.on2.vp9\");\n"
     "\t\t\tcase 6:\n\t\t\t\treturn hwdec() ? new MediaCodecInfo(\"hevc\", \"video/hevc\") : null;\n")

edit("media/MediaCodecInfo.java", """				new CodecProfileLevel(CodecProfileLevel.AVCProfileMain, CodecProfileLevel.AVCLevel61),
			};
		}
""", """				new CodecProfileLevel(CodecProfileLevel.AVCProfileMain, CodecProfileLevel.AVCLevel61),
			};
			if (MediaCodecList.hwdec()) // TV hardware decoder
				caps.profileLevels = new CodecProfileLevel[] {
					new CodecProfileLevel(CodecProfileLevel.AVCProfileBaseline, CodecProfileLevel.AVCLevel51),
					new CodecProfileLevel(CodecProfileLevel.AVCProfileMain, CodecProfileLevel.AVCLevel51),
					new CodecProfileLevel(CodecProfileLevel.AVCProfileHigh, CodecProfileLevel.AVCLevel51),
				};
		} else if ("video/hevc".equals(type)) {
			caps.profileLevels = new CodecProfileLevel[] {
				new CodecProfileLevel(CodecProfileLevel.HEVCProfileMain, CodecProfileLevel.HEVCMainTierLevel51),
				new CodecProfileLevel(CodecProfileLevel.HEVCProfileMain10, CodecProfileLevel.HEVCMainTierLevel51),
			};
		}
""")

edit("media/MediaCodec.java",
     """		} else if ("h264".equals(codecName) || "vp8".equals(codecName) || "vp9".equals(codecName)) {
			native_configure_video(""",
     """		} else if ("h264".equals(codecName) || "hevc".equals(codecName) || "vp8".equals(codecName) || "vp9".equals(codecName)) {
			native_set_video_size(native_codec, format.containsKey("width") ? format.getInteger("width") : 0,
			                      format.containsKey("height") ? format.getInteger("height") : 0);
			native_configure_video(""")
edit("media/MediaCodec.java",
     "\tprivate native void native_configure_video(long codec, ByteBuffer csd0, ByteBuffer csd1, Surface surface);\n",
     "\tprivate native void native_configure_video(long codec, ByteBuffer csd0, ByteBuffer csd1, Surface surface);\n"
     "\tprivate native void native_set_video_size(long codec, int width, int height);\n")

# ------------------------------------------------------------------ SurfaceView hole punch
edit("widgets/android_view_SurfaceView.h", "\tgboolean needs_flip;\n",
     "\tgboolean needs_flip;\n\tgboolean hole_punch; /* video plane below (ATL_HWDEC): draw a transparent hole */\n", base=J)
edit("widgets/android_view_SurfaceView.c", "\tif (getenv(\"ATL_DIRECT_EGL\")) {\n\t\tgraphene_rect_t bounds",
     "\tif (surface_view_widget->hole_punch || getenv(\"ATL_DIRECT_EGL\")) {\n\t\tgraphene_rect_t bounds", base=J)

# ------------------------------------------------------------------ native MediaCodec
MC = "media/android_media_MediaCodec.c"
edit(MC, "#include <stdlib.h>\n\n#include \"../defines.h\"",
     "#include <stdlib.h>\n#include <errno.h>\n#include <inttypes.h>\n#include <poll.h>\n#include <pthread.h>\n#include <sys/socket.h>\n"
     "#include <sys/un.h>\n#include <unistd.h>\n\n#include \"../defines.h\"", base=J)
edit(MC, """			uint8_t *extradata;
		} video;
	};
};
""", """			uint8_t *extradata;
		} video;
	};
	struct hwdec *hw; /* ATL_HWDEC: video goes to the atlvdec helper */
	int hw_width, hw_height;
};

/* ---------------- ATL_HWDEC: hardware decode through the 32-bit atlvdec helper (gpu/atlvdec/proto.h) ---------------- */

struct avd_hdr {
	uint32_t type;
	uint32_t len;
	int64_t pts_us;
};
enum { AVD_OPEN = 1, AVD_FEED = 2, AVD_AREA = 3, AVD_FLUSH = 4, AVD_CLOSE = 5, AVD_RESULT = 16 };

#define HW_WINDOW 4
#define HW_MAX_AUS 24
#define HW_MAX_BYTES (8 << 20)

struct hw_au {
	uint8_t *data;
	size_t len;
	int64_t pts;
	int64_t idx;
};

struct hwdec {
	gint refs;
	pthread_mutex_t lock; /* fd writes: codec thread + main thread (AREA) */
	int fd;
	gboolean dead, closed, debug;
	struct hw_au *aus; /* unsent, decode order */
	int n_aus, cap_aus;
	size_t bytes;
	int64_t next_idx;
	struct {
		int64_t pts, idx;
	} win[HW_MAX_AUS + HW_WINDOW + 8]; /* not yet handed out as tokens */
	int n_win;
	gboolean eos, eos_sent;
	uint8_t *csd;
	size_t csd_len;
	gboolean need_csd;
	uint8_t *prefix; /* non-VCL input (config) waiting for the next picture */
	size_t prefix_len;
	gboolean hevc;
	SurfaceViewWidget *widget;
	int area[4];
	gint64 last_area;
	unsigned long fed;
};

static void hwdec_unref(struct hwdec *hw)
{
	if (!g_atomic_int_dec_and_test(&hw->refs))
		return;
	if (hw->widget)
		g_object_unref(hw->widget);
	pthread_mutex_destroy(&hw->lock);
	free(hw);
}

static int hw_write_all(int fd, const void *p, size_t n)
{
	const char *c = p;
	while (n) {
		ssize_t r = send(fd, c, n, MSG_NOSIGNAL);
		if (r < 0 && errno == EINTR)
			continue;
		if (r <= 0)
			return -1;
		c += r;
		n -= r;
	}
	return 0;
}

static void hw_send(struct hwdec *hw, uint32_t type, const void *a, size_t alen, const void *b, size_t blen, int64_t pts)
{
	pthread_mutex_lock(&hw->lock);
	if (!hw->dead && hw->fd >= 0) {
		struct avd_hdr h = {type, alen + blen, pts};
		if (hw_write_all(hw->fd, &h, sizeof(h)) || (alen && hw_write_all(hw->fd, a, alen)) || (blen && hw_write_all(hw->fd, b, blen))) {
			fprintf(stderr, "ATL_HWDEC: helper connection lost (%s); video stops\\n", strerror(errno));
			hw->dead = TRUE;
		} else if (hw->debug) {
			fprintf(stderr, "ATL_HWDEC: -> type %u len %zu pts %" PRId64 "\\n", type, alen + blen, pts);
		}
	}
	pthread_mutex_unlock(&hw->lock);
}

/* does this Annex-B buffer contain a picture (VCL NAL)? (no start code at all: assume yes) */
static gboolean hw_has_vcl(const uint8_t *b, size_t n, gboolean hevc)
{
	gboolean sc = FALSE;
	for (size_t i = 0; i + 3 < n; i++) {
		if (b[i] || b[i + 1] || b[i + 2] != 1)
			continue;
		sc = TRUE;
		int t = hevc ? (b[i + 3] >> 1) & 0x3f : b[i + 3] & 0x1f;
		if (hevc ? t < 32 : (t >= 1 && t <= 5))
			return TRUE;
		i += 2;
	}
	return !sc;
}

/* main thread: hole punch on, and the video plane follows the SurfaceView rect */
static gboolean hwdec_area_idle(gpointer p)
{
	struct hwdec *hw = p;
	GtkWidget *w = GTK_WIDGET(hw->widget);
	if (!hw->closed && !hw->dead) {
		if (!hw->widget->hole_punch) {
			hw->widget->hole_punch = TRUE;
			gtk_widget_queue_draw(w);
		}
		GtkNative *native = gtk_widget_get_native(w);
		graphene_point_t pos;
		if (native && gtk_widget_get_mapped(w) && gtk_widget_compute_point(w, GTK_WIDGET(native), &GRAPHENE_POINT_INIT(0, 0), &pos)) {
			int ww = gtk_widget_get_width(GTK_WIDGET(native)), wh = gtk_widget_get_height(GTK_WIDGET(native));
			if (ww > 0 && wh > 0) {
				int r[4] = {
				    (int)(pos.x * 1920 / ww), (int)(pos.y * 1080 / wh),
				    (int)(gtk_widget_get_width(w) * 1920.0 / ww), (int)(gtk_widget_get_height(w) * 1080.0 / wh)};
				if (r[2] > 0 && r[3] > 0 && memcmp(r, hw->area, sizeof(r))) {
					memcpy(hw->area, r, sizeof(r));
					int32_t a[4] = {r[0], r[1], r[2], r[3]};
					fprintf(stderr, "ATL_HWDEC: video area %d,%d %dx%d (window %dx%d)\\n", r[0], r[1], r[2], r[3], ww, wh);
					hw_send(hw, AVD_AREA, a, sizeof(a), NULL, 0, 0);
				}
			}
		}
	}
	hwdec_unref(hw);
	return G_SOURCE_REMOVE;
}

/* main thread, after release: hole off; drops the codec's reference */
static gboolean hwdec_close_idle(gpointer p)
{
	struct hwdec *hw = p;
	if (hw->widget && hw->widget->hole_punch) {
		hw->widget->hole_punch = FALSE;
		gtk_widget_queue_draw(GTK_WIDGET(hw->widget));
	}
	hwdec_unref(hw);
	return G_SOURCE_REMOVE;
}

static void hwdec_check_area(struct hwdec *hw)
{
	gint64 now = g_get_monotonic_time();
	if (now - hw->last_area < 500000)
		return;
	hw->last_area = now;
	g_atomic_int_inc(&hw->refs);
	g_idle_add(hwdec_area_idle, hw);
}

static void hwdec_clear(struct hwdec *hw)
{
	for (int i = 0; i < hw->n_aus; i++)
		free(hw->aus[i].data);
	hw->n_aus = 0;
	hw->bytes = 0;
	hw->n_win = 0;
	free(hw->prefix);
	hw->prefix = NULL;
	hw->prefix_len = 0;
	hw->eos = hw->eos_sent = FALSE;
	hw->need_csd = hw->csd_len > 0;
}

/* configure: returns TRUE if the hardware path is used */
static gboolean hwdec_open(struct ATL_codec_context *ctx, const uint8_t *csd, size_t csd_len, SurfaceViewWidget *widget)
{
	const char *gate = getenv("ATL_HWDEC");
	if (!gate || strcmp(gate, "1"))
		return FALSE;
	const char *name = ctx->codec->codec->name;
	gboolean hevc = !strcmp(name, "hevc");
	if (!hevc && strcmp(name, "h264"))
		return FALSE;
	const char *path = getenv("ATL_HWDEC_SOCK");
	if (!widget || !path || !*path) {
		fprintf(stderr, "ATL_HWDEC: %s, using FFmpeg\\n", !widget ? "no Surface" : "ATL_HWDEC_SOCK not set");
		return FALSE;
	}
	struct sockaddr_un a = {.sun_family = AF_UNIX};
	snprintf(a.sun_path, sizeof(a.sun_path), "%s", path);
	int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
	if (fd < 0 || connect(fd, (struct sockaddr *)&a, sizeof(a))) {
		fprintf(stderr, "ATL_HWDEC: helper %s not reachable (%s), using FFmpeg\\n", path, strerror(errno));
		if (fd >= 0)
			close(fd);
		return FALSE;
	}
	struct {
		struct avd_hdr h;
		uint32_t codec, width, height;
	} o = {{AVD_OPEN, 12, 0}, hevc ? 2 : 1, ctx->hw_width, ctx->hw_height};
	struct {
		struct avd_hdr h;
		int32_t status;
		char msg[60];
	} r = {0};
	size_t got = 0;
	if (hw_write_all(fd, &o, sizeof(o.h) + 12) == 0) { /* not sizeof(o): padded to 32 */
		gint64 end = g_get_monotonic_time() + 10000000; /* NDL load takes ~2 s on the TV */
		while (got < sizeof(r)) {
			int left = (int)((end - g_get_monotonic_time()) / 1000);
			struct pollfd p = {fd, POLLIN, 0};
			if (left <= 0 || poll(&p, 1, left) <= 0)
				break;
			ssize_t n = read(fd, (char *)&r + got, sizeof(r) - got);
			if (n <= 0)
				break;
			got += n;
		}
	}
	if (got < sizeof(r) || r.h.type != AVD_RESULT || r.status != 0) {
		r.msg[sizeof(r.msg) - 1] = 0;
		fprintf(stderr, "ATL_HWDEC: helper OPEN failed (%s), using FFmpeg\\n", got < sizeof(r) ? "no reply" : r.msg);
		close(fd);
		return FALSE;
	}
	struct hwdec *hw = calloc(1, sizeof(struct hwdec));
	hw->refs = 1;
	pthread_mutex_init(&hw->lock, NULL);
	hw->fd = fd;
	hw->hevc = hevc;
	hw->debug = getenv("ATL_HWDEC_DEBUG") && !strcmp(getenv("ATL_HWDEC_DEBUG"), "1");
	if (csd_len) {
		hw->csd = malloc(csd_len);
		memcpy(hw->csd, csd, csd_len);
		hw->csd_len = csd_len;
		hw->need_csd = TRUE;
	}
	hw->widget = g_object_ref(widget);
	hw->last_area = -1000000000;
	ctx->hw = hw;
	fprintf(stderr, "ATL_HWDEC: hardware decode active: %s %dx%d via %s\\n", name, ctx->hw_width, ctx->hw_height, path);
	hwdec_check_area(hw);
	return TRUE;
}

static int hwdec_queue(JNIEnv *env, struct hwdec *hw, jobject buffer, jlong pts)
{
	if (!buffer) {
		hw->eos = TRUE;
		return 0;
	}
	if (hw->n_aus >= HW_MAX_AUS || hw->bytes > HW_MAX_BYTES)
		return 1; /* full: Java retries on the next dequeueInputBuffer */
	jarray array_ref = NULL;
	jbyte *array = NULL;
	size_t len = get_nio_buffer_size(env, buffer);
	uint8_t *data = get_nio_buffer(env, buffer, &array_ref, &array);
	size_t pre = (hw->need_csd ? hw->csd_len : 0) + hw->prefix_len;
	uint8_t *au = malloc(pre + len + 1);
	size_t off = 0;
	if (hw->need_csd) {
		memcpy(au, hw->csd, hw->csd_len);
		off = hw->csd_len;
	}
	if (hw->prefix_len) {
		memcpy(au + off, hw->prefix, hw->prefix_len);
		off += hw->prefix_len;
	}
	if (len)
		memcpy(au + off, data, len);
	release_nio_buffer(env, array_ref, array);
	if (!hw_has_vcl(au + pre, len, hw->hevc)) { /* config only: goes in front of the next picture */
		free(hw->prefix);
		hw->prefix = au;
		hw->prefix_len = pre + len;
		hw->need_csd = FALSE;
		return 0;
	}
	free(hw->prefix);
	hw->prefix = NULL;
	hw->prefix_len = 0;
	hw->need_csd = FALSE;
	if (hw->n_aus == hw->cap_aus) {
		hw->cap_aus = hw->cap_aus ? hw->cap_aus * 2 : 32;
		hw->aus = realloc(hw->aus, hw->cap_aus * sizeof(*hw->aus));
	}
	hw->aus[hw->n_aus++] = (struct hw_au){au, pre + len, pts, hw->next_idx};
	hw->bytes += pre + len;
	if (hw->n_win < (int)G_N_ELEMENTS(hw->win)) {
		hw->win[hw->n_win].pts = pts;
		hw->win[hw->n_win].idx = hw->next_idx;
		hw->n_win++;
	}
	hw->next_idx++;
	return 0;
}

static jint hwdec_dequeue(JNIEnv *env, struct hwdec *hw, jobject buffer, jobject buffer_info)
{
	jarray array_ref;
	jbyte *array;
	int64_t token;
	hwdec_check_area(hw);
	if (hw->n_win > HW_WINDOW || (hw->eos && hw->n_win > 0)) {
		int m = 0;
		for (int i = 1; i < hw->n_win; i++)
			if (hw->win[i].pts < hw->win[m].pts)
				m = i;
		token = hw->win[m].idx;
		_SET_INT_FIELD(buffer_info, "flags", 0);
		_SET_LONG_FIELD(buffer_info, "presentationTimeUs", hw->win[m].pts);
		hw->win[m] = hw->win[--hw->n_win];
	} else if (hw->eos && !hw->eos_sent) {
		hw->eos_sent = TRUE;
		token = INT64_MAX;
		_SET_INT_FIELD(buffer_info, "flags", android_media_MediaCodec_BUFFER_FLAG_END_OF_STREAM);
		_SET_LONG_FIELD(buffer_info, "presentationTimeUs", 0);
	} else {
		return -1; /* INFO_TRY_AGAIN_LATER */
	}
	uint8_t *raw = get_nio_buffer(env, buffer, &array_ref, &array);
	memcpy(raw, &token, sizeof(token));
	release_nio_buffer(env, array_ref, array);
	_SET_INT_FIELD(buffer_info, "offset", 0);
	_SET_INT_FIELD(buffer_info, "size", sizeof(token));
	return 0;
}

/* feed every pending AU up to the token's decode index, at least one */
static void hwdec_release(JNIEnv *env, struct hwdec *hw, jobject buffer)
{
	jarray array_ref;
	jbyte *array;
	int64_t token;
	uint8_t *raw = get_nio_buffer(env, buffer, &array_ref, &array);
	memcpy(&token, raw, sizeof(token));
	release_nio_buffer(env, array_ref, array);
	int n = 0;
	while (n < hw->n_aus && (n == 0 || hw->aus[n].idx <= token)) {
		struct hw_au *au = &hw->aus[n];
		hw_send(hw, AVD_FEED, au->data, au->len, NULL, 0, au->pts);
		if (hw->fed++ < 3 || hw->fed % 600 == 0)
			fprintf(stderr, "ATL_HWDEC: fed AU #%lu (%zu bytes, pts %" PRId64 ")\\n", hw->fed, au->len, au->pts);
		hw->bytes -= au->len;
		free(au->data);
		n++;
	}
	if (n) {
		hw->n_aus -= n;
		memmove(hw->aus, hw->aus + n, hw->n_aus * sizeof(*hw->aus));
	}
}

JNIEXPORT void JNICALL Java_android_media_MediaCodec_native_1set_1video_1size(JNIEnv *env, jobject this, jlong codec, jint width, jint height)
{
	struct ATL_codec_context *ctx = _PTR(codec);
	ctx->hw_width = width;
	ctx->hw_height = height;
}
""", base=J)

# configure: try the helper before FFmpeg's hwaccel setup
edit(MC, """	ctx->video.extradata = extradata;
	ctx->video.extradata_size = extradata_size;

	int i = 0;
""", """	ctx->video.extradata = extradata;
	ctx->video.extradata_size = extradata_size;

	if (surface_obj && hwdec_open(ctx, extradata, extradata_size,
	                              SURFACE_VIEW_WIDGET(gtk_widget_get_first_child(_PTR(_GET_LONG_FIELD(surface_obj, "widget")))))) {
		ctx->video.surface_view_widget = ctx->hw->widget;
		return;
	}

	int i = 0;
""", base=J)
edit(MC, """	AVCodecContext *codec_ctx = ctx->codec;

	if (avcodec_open2(codec_ctx""", """	AVCodecContext *codec_ctx = ctx->codec;

	if (ctx->hw)
		return;
	if (avcodec_open2(codec_ctx""", base=J)
edit(MC, """	struct ATL_codec_context *ctx = _PTR(codec);
	AVCodecContext *codec_ctx = ctx->codec;
	AVPacket *pkt = NULL;
""", """	struct ATL_codec_context *ctx = _PTR(codec);
	AVCodecContext *codec_ctx = ctx->codec;
	AVPacket *pkt = NULL;
	if (ctx->hw)
		return hwdec_queue(env, ctx->hw, buffer, presentationTimeUs);
""", base=J)
edit(MC, """	AVFrame *frame = av_frame_alloc();
	int ret;
	jarray array_ref;
	jbyte *array;

	ret = avcodec_receive_frame""", """	if (ctx->hw)
		return hwdec_dequeue(env, ctx->hw, buffer, buffer_info);
	AVFrame *frame = av_frame_alloc();
	int ret;
	jarray array_ref;
	jbyte *array;

	ret = avcodec_receive_frame""", base=J)
edit(MC, """	AVFrame *frame;

	if (ctx->codec->codec_type == AVMEDIA_TYPE_VIDEO) {""", """	AVFrame *frame;

	if (ctx->hw) {
		hwdec_release(env, ctx->hw, buffer);
		return;
	}
	if (ctx->codec->codec_type == AVMEDIA_TYPE_VIDEO) {""", base=J)
edit(MC, """	struct ATL_codec_context *ctx = _PTR(codec);
	avcodec_flush_buffers(ctx->codec);""", """	struct ATL_codec_context *ctx = _PTR(codec);
	if (ctx->hw) {
		hwdec_clear(ctx->hw);
		hw_send(ctx->hw, AVD_FLUSH, NULL, 0, NULL, 0, 0);
		return;
	}
	avcodec_flush_buffers(ctx->codec);""", base=J)
edit(MC, """	struct ATL_codec_context *ctx = _PTR(codec);
	if (ctx->codec->codec_type == AVMEDIA_TYPE_VIDEO && ctx->video.sws) {""", """	struct ATL_codec_context *ctx = _PTR(codec);
	if (ctx->hw) {
		struct hwdec *hw = ctx->hw;
		ctx->hw = NULL;
		hw_send(hw, AVD_CLOSE, NULL, 0, NULL, 0, 0);
		pthread_mutex_lock(&hw->lock);
		hw->closed = TRUE;
		close(hw->fd);
		hw->fd = -1;
		pthread_mutex_unlock(&hw->lock);
		hwdec_clear(hw);
		free(hw->aus);
		free(hw->csd);
		hw->aus = NULL;
		hw->csd = NULL;
		fprintf(stderr, "ATL_HWDEC: released (%lu AUs fed)\\n", hw->fed);
		g_idle_add(hwdec_close_idle, hw);
	}
	if (ctx->codec->codec_type == AVMEDIA_TYPE_VIDEO && ctx->video.sws) {""", base=J)
print("hwdec: ok")
