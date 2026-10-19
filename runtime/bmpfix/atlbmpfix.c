/*
 * libatlbmpfix.so - memory-sane BitmapFactory.nativeDecodeStream for Android Translation Layer.
 *
 * Problem (Titu Flix / Coil / any image-heavy app on a 1.2 GB TV): ATL's nativeDecodeStream ignores
 * BitmapFactory.Options.inJustDecodeBounds and inSampleSize. Apps always probe the size first
 * (decode #1, a FULL decode) and then decode again at 1/2, 1/4, 1/8 size (decode #2, ALSO full).
 * A screen of 2000x3000 posters decoding in parallel allocates 500+ MB of native memory in ~10 s,
 * LG's memchute sees "critical" and closes the foreground app ("closing this app to free memory").
 * (gdk-pixbuf 2.44 in the AppImage gives no way around that: its loaders decode the whole image no matter
 * what size hints are set - measured 75-90 MB peak for a 2000x3000 image even for bounds only.)
 *
 * This library re-registers BitmapFactory.nativeDecodeStream (RegisterNatives wins over symbol lookup, so
 * the stock libtranslation_layer_main.so is left untouched) with an implementation that
 *   - JPEG: libjpeg-turbo, header-only for bounds, DCT-domain 1/2,1/4,1/8 scaling for inSampleSize, RGB out;
 *   - WebP: libwebp, WebPGetFeatures for bounds, scaled decode for inSampleSize;
 *   - PNG: libpng simplified API (all colour types incl. gray+alpha, which the AppImage's gdk-pixbuf 2.44 cannot decode at all),
 *     IHDR probe for bounds, box-filter downscale for inSampleSize;
 *   - GIF/other: gdk-pixbuf; GIF-header probe for bounds; scale_simple after decode for inSampleSize;
 *   - fills opts.outWidth/outHeight/outMimeType;
 *   - limits concurrent full decodes (ATL_DECODE_CONC, default 2);
 *   - returns 0 instead of crashing in gdk_texture_new_for_pixbuf(NULL) when a decode fails.
 *
 * libjpeg/libwebp are linked statically with hidden symbols (see Makefile / build.sh) so they cannot clash
 * with the AppImage's own libjpeg.so.8 / libwebp.so.7. GLib/GDK functions are resolved with dlsym().
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <pthread.h>
#include <setjmp.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <jpeglib.h>
#include <png.h>
#include <webp/decode.h>

typedef void *gpointer;
typedef unsigned long gulong;

#define ATL_MAX_INPUT (96u * 1024 * 1024)
#define GDK_MEMORY_R8G8B8A8 5
#define GDK_MEMORY_R8G8B8 7

/* ---- resolved GLib/GDK symbols (gdk-pixbuf fallback path + texture creation) ---- */
static gpointer (*p_loader_new)(void);
static int (*p_loader_write)(gpointer loader, const unsigned char *buf, size_t count, gpointer *err);
static int (*p_loader_close)(gpointer loader, gpointer *err);
static gpointer (*p_loader_get_pixbuf)(gpointer loader);
static gpointer (*p_loader_get_format)(gpointer loader);
static char **(*p_format_mime)(gpointer fmt);
static int (*p_pixbuf_w)(gpointer pb);
static int (*p_pixbuf_h)(gpointer pb);
static gpointer (*p_pixbuf_scale_simple)(gpointer pb, int w, int h, int interp);
static gpointer (*p_object_ref)(gpointer o);
static void (*p_object_unref)(gpointer o);
static void (*p_strfreev)(char **v);
static void (*p_error_free)(gpointer err);
static gpointer (*p_texture_for_pixbuf)(gpointer pixbuf);
static gpointer (*p_memory_texture_new)(int w, int h, int fmt, gpointer bytes, size_t stride);
static gpointer (*p_bytes_new_with_free_func)(const void *data, size_t size, void (*free_func)(void *), void *user_data);
static void (*p_bytes_unref)(gpointer bytes);

static int resolve_syms(int need_gtk)
{
#define R(var, name) do { *(void **)&var = dlsym(RTLD_DEFAULT, name); if (!var) { fprintf(stderr, "atlbmpfix: missing %s\n", name); return 0; } } while (0)
	R(p_loader_new, "gdk_pixbuf_loader_new");
	R(p_loader_write, "gdk_pixbuf_loader_write");
	R(p_loader_close, "gdk_pixbuf_loader_close");
	R(p_loader_get_pixbuf, "gdk_pixbuf_loader_get_pixbuf");
	R(p_loader_get_format, "gdk_pixbuf_loader_get_format");
	R(p_format_mime, "gdk_pixbuf_format_get_mime_types");
	R(p_pixbuf_w, "gdk_pixbuf_get_width");
	R(p_pixbuf_h, "gdk_pixbuf_get_height");
	R(p_pixbuf_scale_simple, "gdk_pixbuf_scale_simple");
	R(p_object_ref, "g_object_ref");
	R(p_object_unref, "g_object_unref");
	R(p_strfreev, "g_strfreev");
	R(p_error_free, "g_error_free");
	if (need_gtk) {
		R(p_texture_for_pixbuf, "gdk_texture_new_for_pixbuf");
		R(p_memory_texture_new, "gdk_memory_texture_new");
		R(p_bytes_new_with_free_func, "g_bytes_new_with_free_func");
		R(p_bytes_unref, "g_bytes_unref");
	}
#undef R
	return 1;
}

/* ---- concurrency limiter ---- */
static pthread_mutex_t lim_mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t lim_cv = PTHREAD_COND_INITIALIZER;
static int lim_max = 2, lim_cur = 0;

static void lim_acquire(void)
{
	pthread_mutex_lock(&lim_mu);
	while (lim_cur >= lim_max)
		pthread_cond_wait(&lim_cv, &lim_mu);
	lim_cur++;
	pthread_mutex_unlock(&lim_mu);
}

static void lim_release(void)
{
	pthread_mutex_lock(&lim_mu);
	lim_cur--;
	pthread_cond_signal(&lim_cv);
	pthread_mutex_unlock(&lim_mu);
}

/* ---- decoding core (no JNI) ---- */
typedef long (*atl_read_fn)(void *ctx, unsigned char *buf, size_t cap); /* >0 bytes, 0 = EOF, <0 = error */

typedef struct {
	int w, h;               /* decoded size */
	int fmt;                /* GDK_MEMORY_* when pix != NULL */
	size_t stride;
	unsigned char *pix;     /* malloc'd raw pixels, or NULL */
	gpointer pixbuf;        /* owned GdkPixbuf reference (fallback path), or NULL */
} Decoded;

/* Android rounds inSampleSize down to a power of two */
static int pow2_floor(int s)
{
	int p = 1;
	while (p * 2 <= s && p < (1 << 16))
		p *= 2;
	return p;
}

/* box-average downscale of tightly packed 8-bit-per-channel pixels by integer factor r (in place into a new buffer) */
static unsigned char *box_down(const unsigned char *src, int w, int h, int ch, size_t stride, int r, int *ow, int *oh)
{
	int nw = (w + r - 1) / r, nh = (h + r - 1) / r;
	unsigned char *dst = malloc((size_t)nw * nh * ch);
	if (!dst)
		return NULL;
	for (int y = 0; y < nh; y++)
		for (int x = 0; x < nw; x++) {
			unsigned sum[4] = { 0, 0, 0, 0 };
			int n = 0;
			for (int dy = 0; dy < r && y * r + dy < h; dy++)
				for (int dx = 0; dx < r && x * r + dx < w; dx++) {
					const unsigned char *p = src + (size_t)(y * r + dy) * stride + (size_t)(x * r + dx) * ch;
					for (int c = 0; c < ch; c++)
						sum[c] += p[c];
					n++;
				}
			for (int c = 0; c < ch; c++)
				dst[((size_t)y * nw + x) * ch + c] = (unsigned char)(sum[c] / n);
		}
	*ow = nw;
	*oh = nh;
	return dst;
}

/* --- JPEG --- */
struct jerr {
	struct jpeg_error_mgr pub;
	jmp_buf jb;
};

static void jerr_exit(j_common_ptr cinfo)
{
	longjmp(((struct jerr *)cinfo->err)->jb, 1);
}

static void jerr_silent(j_common_ptr cinfo, int level)
{
}

/* returns 1 = handled (d filled or bounds done), 0 = not usable, fall back to gdk-pixbuf */
static int decode_jpeg(const unsigned char *data, size_t len, int sample, int bounds, int *ow, int *oh, Decoded *d)
{
	struct jpeg_decompress_struct ci;
	struct jerr je;
	unsigned char *volatile pix = NULL;

	ci.err = jpeg_std_error(&je.pub);
	je.pub.error_exit = jerr_exit;
	je.pub.emit_message = jerr_silent;
	if (setjmp(je.jb)) {
		jpeg_destroy_decompress(&ci);
		free(pix);
		return 0;
	}
	jpeg_create_decompress(&ci);
	jpeg_mem_src(&ci, data, len);
	jpeg_read_header(&ci, TRUE);
	*ow = (int)ci.image_width;
	*oh = (int)ci.image_height;
	if (bounds) {
		jpeg_destroy_decompress(&ci);
		return 1;
	}
	if (ci.jpeg_color_space == JCS_CMYK || ci.jpeg_color_space == JCS_YCCK) { /* libjpeg cannot convert these to RGB */
		jpeg_destroy_decompress(&ci);
		return 0;
	}

	int denom = sample > 8 ? 8 : sample;
	int extra = sample / denom; /* remaining power-of-two factor beyond libjpeg's 1/8 */
	ci.out_color_space = JCS_RGB;
	ci.scale_num = 1;
	ci.scale_denom = (unsigned)denom;
	ci.dct_method = JDCT_IFAST;
	ci.do_fancy_upsampling = denom > 1 ? FALSE : TRUE;
	jpeg_start_decompress(&ci);

	int w = (int)ci.output_width, h = (int)ci.output_height;
	size_t stride = (size_t)w * 3;
	pix = malloc(stride * h);
	if (!pix)
		longjmp(je.jb, 1);
	while (ci.output_scanline < ci.output_height) {
		JSAMPROW row = pix + stride * ci.output_scanline;
		jpeg_read_scanlines(&ci, &row, 1);
	}
	jpeg_finish_decompress(&ci);
	jpeg_destroy_decompress(&ci);

	if (extra > 1) {
		int nw, nh;
		unsigned char *small = box_down(pix, w, h, 3, stride, extra, &nw, &nh);
		free(pix);
		if (!small)
			return 0;
		pix = small;
		w = nw;
		h = nh;
		stride = (size_t)w * 3;
	}
	d->pix = pix;
	d->w = w;
	d->h = h;
	d->stride = stride;
	d->fmt = GDK_MEMORY_R8G8B8;
	return 1;
}

/* --- WebP --- */
static int decode_webp(const unsigned char *data, size_t len, int sample, int bounds, int *ow, int *oh, Decoded *d)
{
	WebPDecoderConfig cfg;
	if (!WebPInitDecoderConfig(&cfg))
		return 0;
	if (WebPGetFeatures(data, len, &cfg.input) != VP8_STATUS_OK)
		return 0;
	if (cfg.input.has_animation)
		return 0; /* gdk-pixbuf path (also unsupported there, but keeps old behaviour) */
	*ow = cfg.input.width;
	*oh = cfg.input.height;
	if (bounds)
		return 1;

	int alpha = cfg.input.has_alpha;
	int w = (*ow + sample - 1) / sample, h = (*oh + sample - 1) / sample;
	if (w < 1) w = 1;
	if (h < 1) h = 1;
	int ch = alpha ? 4 : 3;
	size_t stride = (size_t)w * ch;
	unsigned char *pix = malloc(stride * h);
	if (!pix)
		return 0;
	if (sample > 1) {
		cfg.options.use_scaling = 1;
		cfg.options.scaled_width = w;
		cfg.options.scaled_height = h;
	}
	cfg.options.use_threads = 0;
	cfg.output.colorspace = alpha ? MODE_RGBA : MODE_RGB;
	cfg.output.is_external_memory = 1;
	cfg.output.u.RGBA.rgba = pix;
	cfg.output.u.RGBA.stride = (int)stride;
	cfg.output.u.RGBA.size = stride * h;
	if (WebPDecode(data, len, &cfg) != VP8_STATUS_OK) {
		WebPFreeDecBuffer(&cfg.output);
		free(pix);
		return 0;
	}
	d->pix = pix;
	d->w = w;
	d->h = h;
	d->stride = stride;
	d->fmt = alpha ? GDK_MEMORY_R8G8B8A8 : GDK_MEMORY_R8G8B8;
	return 1;
}

/* --- PNG (libpng): the AppImage's gdk-pixbuf fails on 8-bit gray+alpha PNGs (common in xhdpi/xxhdpi drawables) --- */
static int decode_png(const unsigned char *data, size_t len, int sample, int *ow, int *oh, Decoded *d)
{
	png_image im;
	memset(&im, 0, sizeof im);
	im.version = PNG_IMAGE_VERSION;
	if (!png_image_begin_read_from_memory(&im, data, len))
		return 0;
	*ow = (int)im.width;
	*oh = (int)im.height;
	im.format = PNG_FORMAT_RGBA; /* expands palette/gray/16-bit, non-premultiplied like GDK_MEMORY_R8G8B8A8 */
	size_t stride = PNG_IMAGE_ROW_STRIDE(im), sz = PNG_IMAGE_SIZE(im);
	if (sz == 0 || sz > (size_t)768 * 1024 * 1024) {
		png_image_free(&im);
		return 0;
	}
	unsigned char *pix = malloc(sz);
	if (!pix) {
		png_image_free(&im);
		return 0;
	}
	if (!png_image_finish_read(&im, NULL, pix, (png_int_32)stride, NULL)) {
		png_image_free(&im);
		free(pix);
		return 0;
	}
	int w = (int)im.width, h = (int)im.height;
	if (sample > 1) {
		int nw, nh;
		unsigned char *small = box_down(pix, w, h, 4, stride, sample, &nw, &nh);
		free(pix);
		if (!small)
			return 0;
		pix = small;
		w = nw;
		h = nh;
		stride = (size_t)w * 4;
	}
	d->pix = pix;
	d->w = w;
	d->h = h;
	d->stride = stride;
	d->fmt = GDK_MEMORY_R8G8B8A8;
	return 1;
}

/* --- header probes for formats we do not decode ourselves --- */
static int probe_png(const unsigned char *b, size_t n, int *w, int *h)
{
	if (n < 24 || memcmp(b, "\x89PNG\r\n\x1a\n", 8) || memcmp(b + 12, "IHDR", 4))
		return 0;
	*w = (int)(((unsigned)b[16] << 24) | (b[17] << 16) | (b[18] << 8) | b[19]);
	*h = (int)(((unsigned)b[20] << 24) | (b[21] << 16) | (b[22] << 8) | b[23]);
	return 1;
}

static int probe_gif(const unsigned char *b, size_t n, int *w, int *h)
{
	if (n < 10 || memcmp(b, "GIF8", 4))
		return 0;
	*w = b[6] | (b[7] << 8);
	*h = b[8] | (b[9] << 8);
	return 1;
}

/* --- gdk-pixbuf fallback (PNG, GIF, BMP, ICO, TIFF, AVIF ... ) --- */
static int decode_pixbuf(const unsigned char *data, size_t len, int sample, int bounds, int *ow, int *oh, char *mime, Decoded *d)
{
	gpointer loader = p_loader_new(), err = NULL, pb;
	size_t off = 0;
	int ok = 1;

	while (off < len) {
		size_t n = len - off > 65536 ? 65536 : len - off;
		if (!p_loader_write(loader, data + off, n, &err)) {
			if (err) { p_error_free(err); err = NULL; }
			ok = 0;
			break;
		}
		off += n;
	}
	if (!p_loader_close(loader, &err) && err) {
		p_error_free(err);
		ok = 0;
	}
	pb = ok ? p_loader_get_pixbuf(loader) : NULL;
	if (pb) {
		gpointer fmt = p_loader_get_format(loader);
		*ow = p_pixbuf_w(pb);
		*oh = p_pixbuf_h(pb);
		if (fmt) {
			char **m = p_format_mime(fmt);
			if (m && m[0]) { strncpy(mime, m[0], 63); mime[63] = 0; }
			if (m) p_strfreev(m);
		}
		if (!bounds) {
			if (sample > 1) {
				int nw = (*ow + sample - 1) / sample, nh = (*oh + sample - 1) / sample;
				gpointer sm = p_pixbuf_scale_simple(pb, nw > 0 ? nw : 1, nh > 0 ? nh : 1, 2 /* GDK_INTERP_BILINEAR */);
				d->pixbuf = sm ? sm : p_object_ref(pb);
			} else {
				d->pixbuf = p_object_ref(pb);
			}
			d->w = p_pixbuf_w(d->pixbuf);
			d->h = p_pixbuf_h(d->pixbuf);
		}
	}
	p_object_unref(loader);
	return pb != NULL;
}

/*
 * Decode `data`. Returns 1 on success (for bounds: size known; otherwise d is filled), 0 on failure.
 * ow/oh = ORIGINAL image size, mime = mime type or "".
 */
static int atl_decode_buf(const unsigned char *data, size_t len, int sample, int bounds, int *ow, int *oh, char *mime, Decoded *d)
{
	int ok = 0;
	*ow = *oh = 0;
	mime[0] = 0;
	memset(d, 0, sizeof *d);
	sample = pow2_floor(sample < 1 ? 1 : sample);

	int held = 0;
	if (!bounds) {
		lim_acquire();
		held = 1;
	}

	if (len > 3 && data[0] == 0xFF && data[1] == 0xD8 && data[2] == 0xFF) {
		ok = decode_jpeg(data, len, sample, bounds, ow, oh, d);
		if (ok)
			strcpy(mime, "image/jpeg");
	} else if (len > 12 && !memcmp(data, "RIFF", 4) && !memcmp(data + 8, "WEBP", 4)) {
		ok = decode_webp(data, len, sample, bounds, ow, oh, d);
		if (ok)
			strcpy(mime, "image/webp");
	} else if (bounds && probe_png(data, len, ow, oh)) {
		strcpy(mime, "image/png");
		ok = 1;
	} else if (!bounds && len > 8 && !memcmp(data, "\x89PNG\r\n\x1a\n", 8)) {
		ok = decode_png(data, len, sample, ow, oh, d);
		if (ok)
			strcpy(mime, "image/png");
	} else if (bounds && probe_gif(data, len, ow, oh)) {
		strcpy(mime, "image/gif");
		ok = 1;
	}
	if (!ok) {
		*ow = *oh = 0;
		if (!held) { /* gdk-pixbuf may decode the whole image even for a bounds probe */
			lim_acquire();
			held = 1;
		}
		ok = decode_pixbuf(data, len, sample, bounds, ow, oh, mime, d);
	}

	if (held)
		lim_release();
	return ok;
}

/* read an entire stream into a malloc'd buffer */
static unsigned char *read_all(atl_read_fn rd, void *rctx, size_t *outlen)
{
	size_t cap = 256 * 1024, len = 0;
	unsigned char *buf = malloc(cap);
	if (!buf)
		return NULL;
	for (;;) {
		if (cap - len < 64 * 1024) {
			if (cap >= ATL_MAX_INPUT) {
				free(buf);
				return NULL;
			}
			cap *= 2;
			unsigned char *nb = realloc(buf, cap);
			if (!nb) {
				free(buf);
				return NULL;
			}
			buf = nb;
		}
		long n = rd(rctx, buf + len, cap - len);
		if (n < 0) {
			free(buf);
			return NULL;
		}
		if (n == 0)
			break;
		len += (size_t)n;
	}
	*outlen = len;
	return buf;
}

static gpointer decoded_to_texture(Decoded *d)
{
	gpointer tex = NULL;
	if (d->pix) {
		gpointer bytes = p_bytes_new_with_free_func(d->pix, d->stride * d->h, free, d->pix);
		tex = p_memory_texture_new(d->w, d->h, d->fmt, bytes, d->stride);
		p_bytes_unref(bytes);
		d->pix = NULL;
	} else if (d->pixbuf) {
		tex = p_texture_for_pixbuf(d->pixbuf);
		p_object_unref(d->pixbuf);
		d->pixbuf = NULL;
	}
	return tex;
}

#ifndef ATLBMP_NO_JNI
/* ---- JNI glue ---- */
#include <jni.h>

typedef struct {
	JNIEnv *env;
	jobject is;
	jbyteArray storage;
	jint storage_size;
	jmethodID read;
} JRead;

static long jread(void *ctx, unsigned char *buf, size_t cap)
{
	JRead *j = ctx;
	JNIEnv *env = j->env;
	jint want = (jint)(cap < (size_t)j->storage_size ? cap : (size_t)j->storage_size);
	for (;;) {
		jint n = (*env)->CallIntMethod(env, j->is, j->read, j->storage, 0, want);
		if ((*env)->ExceptionCheck(env)) {
			(*env)->ExceptionClear(env);
			return -1;
		}
		if (n < 0)
			return 0; /* EOF */
		if (n == 0)
			continue; /* InputStream.read must not return 0 for len>0, but be safe */
		(*env)->GetByteArrayRegion(env, j->storage, 0, n, (jbyte *)buf);
		return n;
	}
}

static jfieldID f_bounds, f_sample, f_outw, f_outh, f_outmime;
static int log_enabled;

static jlong JNICALL native_decode_stream(JNIEnv *env, jclass cls, jobject is, jbyteArray storage, jobject padding, jobject opts)
{
	int sample = 1, bounds = 0, ow = 0, oh = 0;
	char mime[64];
	JRead jr;
	Decoded d;
	size_t len = 0;
	unsigned char *data;
	gpointer texture = NULL;

	if (opts) {
		bounds = (*env)->GetBooleanField(env, opts, f_bounds) ? 1 : 0;
		sample = (*env)->GetIntField(env, opts, f_sample);
		if (sample < 1)
			sample = 1;
	}

	jr.env = env;
	jr.is = is;
	jr.storage = storage;
	jr.storage_size = (*env)->GetArrayLength(env, storage);
	{
		jclass isc = (*env)->GetObjectClass(env, is);
		jr.read = (*env)->GetMethodID(env, isc, "read", "([BII)I");
		(*env)->DeleteLocalRef(env, isc);
	}
	if (!jr.read || jr.storage_size <= 0) {
		(*env)->ExceptionClear(env);
		return 0;
	}

	data = read_all(jread, &jr, &len);
	if (!data)
		return 0;

	int ok = atl_decode_buf(data, len, sample, bounds, &ow, &oh, mime, &d);
	free(data);

	if (opts && ok && ow > 0 && oh > 0) {
		/* Android: with inJustDecodeBounds outWidth/outHeight = real size; otherwise the (sampled) result
		 * size, which BitmapFactory.decodeStream() sets from the Bitmap afterwards. */
		(*env)->SetIntField(env, opts, f_outw, ow);
		(*env)->SetIntField(env, opts, f_outh, oh);
	}
	if (opts && ok && mime[0]) {
		jstring s = (*env)->NewStringUTF(env, mime);
		(*env)->SetObjectField(env, opts, f_outmime, s);
		(*env)->DeleteLocalRef(env, s);
	}
	if (log_enabled)
		fprintf(stderr, "atlbmpfix: %s %zu bytes %dx%d sample=%d -> %dx%d %s%s\n", bounds ? "bounds" : "decode", len, ow, oh,
		        sample, d.w, d.h, mime, ok ? "" : " FAILED");

	if (ok && !bounds)
		texture = decoded_to_texture(&d);
	free(d.pix);
	if (d.pixbuf)
		p_object_unref(d.pixbuf);
	return (jlong)(intptr_t)texture;
}

JNIEXPORT jint JNICALL JNI_OnLoad(JavaVM *vm, void *reserved)
{
	JNIEnv *env;
	jclass bf, optc;
	const char *e;

	if ((*vm)->GetEnv(vm, (void **)&env, JNI_VERSION_1_6) != JNI_OK)
		return JNI_VERSION_1_6;
	if (!resolve_syms(1))
		return JNI_VERSION_1_6; /* leave the stock implementation in place */

	if ((e = getenv("ATL_DECODE_CONC")) && atoi(e) > 0)
		lim_max = atoi(e);
	log_enabled = (e = getenv("ATL_BMPFIX_LOG")) && *e && *e != '0';

	bf = (*env)->FindClass(env, "android/graphics/BitmapFactory");
	optc = (*env)->FindClass(env, "android/graphics/BitmapFactory$Options");
	if (!bf || !optc) {
		(*env)->ExceptionClear(env);
		fprintf(stderr, "atlbmpfix: BitmapFactory classes not found\n");
		return JNI_VERSION_1_6;
	}
	f_bounds = (*env)->GetFieldID(env, optc, "inJustDecodeBounds", "Z");
	f_sample = (*env)->GetFieldID(env, optc, "inSampleSize", "I");
	f_outw = (*env)->GetFieldID(env, optc, "outWidth", "I");
	f_outh = (*env)->GetFieldID(env, optc, "outHeight", "I");
	f_outmime = (*env)->GetFieldID(env, optc, "outMimeType", "Ljava/lang/String;");
	if (!f_bounds || !f_sample || !f_outw || !f_outh || !f_outmime) {
		(*env)->ExceptionClear(env);
		fprintf(stderr, "atlbmpfix: Options fields not found\n");
		return JNI_VERSION_1_6;
	}

	JNINativeMethod m = {
		"nativeDecodeStream",
		"(Ljava/io/InputStream;[BLandroid/graphics/Rect;Landroid/graphics/BitmapFactory$Options;)J",
		(void *)native_decode_stream,
	};
	if ((*env)->RegisterNatives(env, bf, &m, 1) != JNI_OK) {
		(*env)->ExceptionClear(env);
		fprintf(stderr, "atlbmpfix: RegisterNatives failed\n");
	} else if (log_enabled) {
		fprintf(stderr, "atlbmpfix: nativeDecodeStream registered (conc=%d)\n", lim_max);
	}
	return JNI_VERSION_1_6;
}
#endif /* ATLBMP_NO_JNI */
