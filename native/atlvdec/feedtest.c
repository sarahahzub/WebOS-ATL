/* feedtest <socket> <file.h264|file.hevc> <width> <height> [fps] [noflush]   env FT_DUE_MS, FT_SCRIPT, FT_SECS
 * Splits an Annex-B elementary stream into access units and drives atlvdec:
 * OPEN, AREA(0,0,1920,1080), FEED at fps, FLUSH at the half (unless "noflush"), CLOSE. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include "proto.h"

static int hevc;

static int send_msg(int fd, uint32_t type, const void *p, uint32_t len, int64_t pts)
{
	struct avd_hdr h = {type, len, pts};
	if (write(fd, &h, sizeof(h)) != sizeof(h))
		return -1;
	return len && write(fd, p, len) != (ssize_t)len ? -1 : 0;
}

/* next start code at or after i; returns offset of the 00 00 01 (or n) */
static size_t next_sc(const uint8_t *b, size_t i, size_t n)
{
	for (; i + 3 <= n; i++)
		if (b[i] == 0 && b[i + 1] == 0 && b[i + 2] == 1)
			return i;
	return n;
}

static int is_vcl(const uint8_t *nal) { return hevc ? ((nal[0] >> 1) & 0x3f) < 32 : (nal[0] & 0x1f) >= 1 && (nal[0] & 0x1f) <= 5; }
static int first_slice(const uint8_t *nal) { return hevc ? nal[2] & 0x80 : nal[1] & 0x80; }
static int starts_au(const uint8_t *nal)
{
	int t = hevc ? (nal[0] >> 1) & 0x3f : nal[0] & 0x1f;
	return hevc ? (t == 35 || t == 32 || t == 33 || t == 34 || t == 39) : (t == 9 || t == 7 || t == 8 || t == 6);
}

int main(int argc, char **argv)
{
	if (argc < 5) {
		fprintf(stderr, "usage: feedtest <socket> <file.h264|.hevc> <w> <h> [fps] [noflush]\n");
		return 2;
	}
	const char *f = argv[2];
	hevc = strstr(f, ".hevc") || strstr(f, ".h265") || strstr(f, ".265");
	double fps = argc > 5 ? atof(argv[5]) : 30;
	int noflush = argc > 6 && !strcmp(argv[6], "noflush");
	FILE *fp = fopen(f, "rb");
	if (!fp) {
		perror(f);
		return 1;
	}
	fseek(fp, 0, SEEK_END);
	size_t n = ftell(fp);
	fseek(fp, 0, SEEK_SET);
	uint8_t *b = malloc(n);
	if (fread(b, 1, n, fp) != n)
		return 1;
	fclose(fp);

	/* AU boundaries */
	size_t *au = malloc(sizeof(size_t) * (n / 4 + 2));
	int nau = 0, have_vcl = 0;
	for (size_t i = next_sc(b, 0, n); i < n;) {
		size_t s = i;
		if (s > 0 && b[s - 1] == 0)
			s--; /* 4-byte start code */
		const uint8_t *nal = b + i + 3;
		if (i + 6 > n)
			break;
		if (nau == 0 || (have_vcl && (starts_au(nal) || (is_vcl(nal) && first_slice(nal))))) {
			au[nau++] = s;
			have_vcl = 0;
		}
		if (is_vcl(nal))
			have_vcl = 1;
		i = next_sc(b, i + 3, n);
	}
	au[nau] = n;
	fprintf(stderr, "feedtest: %s: %s, %d access units\n", f, hevc ? "hevc" : "h264", nau);

	int fd = socket(AF_UNIX, SOCK_STREAM, 0);
	struct sockaddr_un a = {.sun_family = AF_UNIX};
	snprintf(a.sun_path, sizeof(a.sun_path), "%s", argv[1]);
	if (connect(fd, (struct sockaddr *)&a, sizeof(a))) {
		perror("connect");
		return 1;
	}
	struct avd_open o = {hevc ? AVD_CODEC_HEVC : AVD_CODEC_H264, atoi(argv[3]), atoi(argv[4])};
	send_msg(fd, AVD_OPEN, &o, sizeof(o), 0);
	struct {
		struct avd_hdr h;
		struct avd_result r;
	} res;
	if (read(fd, &res, sizeof(res)) != sizeof(res)) {
		fprintf(stderr, "feedtest: no OPEN reply\n");
		return 1;
	}
	fprintf(stderr, "feedtest: OPEN -> %d %s\n", res.r.status, res.r.msg);
	if (res.r.status)
		return 1;
	struct avd_area ar = {0, 0, 1920, 1080};
	if (!getenv("FT_SCRIPT"))
		send_msg(fd, AVD_AREA, &ar, sizeof(ar), 0);
	struct timespec d = {0, (long)(1e9 / fps)};
	long due_ms = getenv("FT_DUE_MS") ? atol(getenv("FT_DUE_MS")) : -1;
	/* FT_SCRIPT="t:a,x,y,w,h;t:f;..." (t = seconds since start): AREA or FLUSH (feed restarts at the IDR);
	   with a script the file is looped until FT_SECS (default: last script time + 12 s) */
	const char *sc = getenv("FT_SCRIPT");
	double secs = getenv("FT_SECS") ? atof(getenv("FT_SECS")) : 0, t0 = 0;
	struct timespec ts0;
	clock_gettime(CLOCK_MONOTONIC, &ts0);
	if (sc) {
		noflush = 1;
		const char *q = sc;
		double last = 0;
		while (*q) { last = atof(q); q = strchr(q, ';'); if (!q) break; q++; }
		if (!secs) secs = last + 12;
	}
	for (int i = 0; i < nau; i++) {
		if (sc) {
			struct timespec tn;
			clock_gettime(CLOCK_MONOTONIC, &tn);
			double t = (tn.tv_sec - ts0.tv_sec) + (tn.tv_nsec - ts0.tv_nsec) / 1e9;
			const char *q = sc;
			while (q && *q) {
				double at = atof(q);
				const char *c = strchr(q, ':');
				if (c && at > t0 && at <= t) {
					if (c[1] == 'f') {
						send_msg(fd, AVD_FLUSH, NULL, 0, 0);
						i = 0;
						fprintf(stderr, "feedtest: %.1f s FLUSH\n", t);
					} else if (c[1] == 'a') {
						struct avd_area r2 = {0};
						sscanf(c + 3, "%d,%d,%d,%d", &r2.x, &r2.y, &r2.w, &r2.h);
						send_msg(fd, AVD_AREA, &r2, sizeof(r2), 0);
						fprintf(stderr, "feedtest: %.1f s AREA %d,%d %dx%d\n", t, r2.x, r2.y, r2.w, r2.h);
					}
				}
				q = strchr(q, ';');
				if (q) q++;
			}
			t0 = t;
			if (t > secs) break;
			if (i == nau - 1) i = 0; /* loop (first AU has SPS/PPS + IDR) */
		}
		if (!noflush && i == nau / 2) {
			/* seek back to 0 once (first AU has SPS/PPS + IDR) */
			send_msg(fd, AVD_FLUSH, NULL, 0, 0);
			noflush = 1;
			i = 0;
		}
		if (due_ms >= 0) { /* FT_DUE_MS=<n>: AVD_DUE = now + n ms before each AU (like MediaCodec.releaseOutputBuffer(i, t)) */
			struct timespec tn;
			clock_gettime(CLOCK_MONOTONIC, &tn);
			int64_t due = tn.tv_sec * 1000000000LL + tn.tv_nsec + due_ms * 1000000LL;
			send_msg(fd, AVD_DUE, &due, sizeof(due), 0);
		}
		if (send_msg(fd, AVD_FEED, b + au[i], au[i + 1] - au[i], (int64_t)(i * 1e6 / fps))) {
			fprintf(stderr, "feedtest: send failed\n");
			return 1;
		}
		nanosleep(&d, NULL);
	}
	send_msg(fd, AVD_CLOSE, NULL, 0, 0);
	usleep(200000);
	close(fd);
	fprintf(stderr, "feedtest: done, %d AUs fed\n", nau);
	return 0;
}
