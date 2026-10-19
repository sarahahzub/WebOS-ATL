/* Bench stand-in for libNDL_directmedia (linked into atlvdec-stub with -rdynamic): logs every call. */
#include <stdio.h>

#define S(...) fprintf(stderr, "  [ndl-stub] " __VA_ARGS__)

typedef void (*ndl_load_cb)(int type, long long num, const char *str);
static unsigned long plays;

const char *NDL_DirectMediaGetError(void) { return "stub"; }
/* the TV's NDL impl exports a video window at init (resolved in atlvdec: -rdynamic) */
const char *SDL_webOSCreateExportedWindow(int type);
void *SDL_webOSSetExportedWindow(const char *id, const int *src, const int *dst);
int NDL_DirectMediaInit(const char *appid, void *cb)
{
	S("Init(%s)\n", appid);
	(void)cb;
	const char *id = SDL_webOSCreateExportedWindow(0);
	static const int full[4] = {0, 0, 1920, 1080};
	if (id)
		SDL_webOSSetExportedWindow(id, full, full);
	return 0;
}
int NDL_DirectMediaQuit(void) { S("Quit\n"); return 0; }
int NDL_DirectMediaLoad(int *info, ndl_load_cb cb)
{
	S("Load(video %dx%d type=%d audio=%d)\n", info[0], info[1], info[2], info[4]);
	plays = 0;
	if (cb) {
		cb(0x16, 0, "");
		cb(0x1a, 0, "");
	}
	return 0;
}
int NDL_DirectMediaUnload(void) { S("Unload (after %lu plays)\n", plays); return 0; }
int NDL_DirectVideoPlay(void *buf, unsigned int size, long long pts)
{
	const unsigned char *b = buf;
	plays++;
	S("Play(#%lu, %u bytes, pts %lld, head %02x%02x%02x%02x%02x)\n", plays, size, pts, b[0], b[1], b[2], b[3], size > 4 ? b[4] : 0);
	return 0;
}
int NDL_DirectVideoSetArea(int x, int y, int w, int h) { S("SetArea(%d,%d,%d,%d)\n", x, y, w, h); return 0; }
int NDL_DirectVideoGetRenderBufferLength(int *l) { *l = 0; return 0; }
