/*
 * low4g.so - LD_PRELOAD fix for ART hanging at startup on the LG TV (aarch64, 39-bit VA).
 *
 * ART's art::MemMap::MapInternalArtLow4GBAllocator() finds space below 4 GB by
 * probing page by page with msync() and restarting after every foreign mapping.
 * On the TV the low 4 GB already holds the non-PIE ATL binary, its brk heap and
 * the boot image, so the search effectively never ends (100% CPU in Heap()).
 * This replacement reads /proc/self/maps, takes the first gap that fits
 * (searching from 0x10000000 up), and maps it with MAP_FIXED_NOREPLACE.
 * libartbase calls it directly (no PLT), so a constructor patches the first 16
 * bytes of the original with `ldr x16,#8; br x16; .quad ours`.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/mman.h>
#include <sys/types.h>
#include <dlfcn.h>
#include <string.h>
#include <unistd.h>

#ifndef MAP_FIXED_NOREPLACE
#define MAP_FIXED_NOREPLACE 0x100000
#endif
#define LOW_START 0x10000000UL
#define LIMIT (1UL << 32)

static void *try_at(uintptr_t at, size_t len, int prot, int flags, int fd, off_t off)
{
	void *p = mmap((void *)at, len, prot, (flags & ~MAP_FIXED) | MAP_FIXED_NOREPLACE, fd, off);
	if (p == MAP_FAILED) return NULL;
	if ((uintptr_t)p != at) { munmap(p, len); return NULL; } /* kernel without NOREPLACE */
	return p;
}

/* static void* art::MemMap::MapInternalArtLow4GBAllocator(size_t, int, int, int, off_t) */
static void *low4g_map(size_t length, int prot, int flags, int fd, off_t offset);
#define SYM "_ZN3art6MemMap29MapInternalArtLow4GBAllocatorEmiiil"

__attribute__((constructor)) static void low4g_init(void)
{
	void *h = dlopen("libartbase.so", RTLD_NOW | RTLD_GLOBAL);
	uint32_t *fn = h ? dlsym(h, SYM) : NULL;
	if (!fn) { fprintf(stderr, "low4g: %s not found (%s)\n", SYM, dlerror()); return; }
	uintptr_t pg = (uintptr_t)fn & ~0xfffUL;
	if (mprotect((void *)pg, 0x2000, PROT_READ | PROT_WRITE | PROT_EXEC)) { perror("low4g mprotect"); return; }
	uint64_t target = (uint64_t)(uintptr_t)low4g_map;
	fn[0] = 0x58000050; /* ldr x16, #8 */
	fn[1] = 0xd61f0200; /* br x16 */
	memcpy(&fn[2], &target, 8);
	__builtin___clear_cache((char *)fn, (char *)(fn + 4));
	mprotect((void *)pg, 0x2000, PROT_READ | PROT_EXEC);
	if (getenv("LOW4G_DEBUG")) fprintf(stderr, "low4g: patched %p\n", (void *)fn);
}

static void *low4g_map(size_t length, int prot, int flags, int fd, off_t offset)
{
	length = (length + 0xfff) & ~0xfffUL;
	for (int attempt = 0; attempt < 4; attempt++) {
		FILE *f = fopen("/proc/self/maps", "re");
		if (!f) break;
		uintptr_t prev_end = LOW_START, a, b;
		char line[512];
		void *p = NULL;
		while (!p && fgets(line, sizeof line, f)) {
			if (sscanf(line, "%lx-%lx", &a, &b) != 2) continue;
			if (b <= prev_end) continue;
			if (a >= LIMIT) break;
			if (a > prev_end && a - prev_end >= length) p = try_at(prev_end, length, prot, flags, fd, offset);
			prev_end = b > prev_end ? b : prev_end;
		}
		fclose(f);
		if (!p && prev_end < LIMIT && LIMIT - prev_end >= length) p = try_at(prev_end, length, prot, flags, fd, offset);
		if (p) return p;
	}
	errno = ENOMEM;
	return MAP_FAILED;
}

/* ATL's Bitmap texture path opens a second GdkDisplay (gdk_display_open(NULL)) for off-screen
 * rendering. On the TV (through xdgshim) the main window then stops redrawing after its first
 * frames (Compose apps freeze). Hand back the default display instead: rendering a texture does not
 * need a separate connection. LOW4G_OFFSCREEN=1 restores the original behaviour. */
void *gdk_display_open(const char *name)
{
	static void *(*real)(const char *);
	static void *(*get_default)(void);
	static void *(*ref)(void *);
	if (!real) {
		real = dlsym(RTLD_NEXT, "gdk_display_open");
		get_default = dlsym(RTLD_DEFAULT, "gdk_display_get_default");
		ref = dlsym(RTLD_DEFAULT, "g_object_ref");
	}
	if (!name && !getenv("LOW4G_OFFSCREEN") && get_default && ref) {
		void *d = get_default();
		if (d) {
			if (getenv("LOW4G_DEBUG")) fprintf(stderr, "low4g: gdk_display_open(NULL) -> default display\n");
			return ref(d);
		}
	}
	return real ? real(name) : NULL;
}
