#!/usr/bin/env python3
"""GL forwarding (ATL_GLFWD=1): all GL in the ATL process - GTK's own UI (GSK_RENDERER=gl, formerly ngl) and the app's EGL/GLES
(GLSurfaceView, Android EGL in libandroid/egl.c, native GLES) - goes through Mesa's virgl gallium driver with the vtest
winsys (virpipe, bundled libgallium) to a virgl_test_server, which on the TV is a 32-bit armhf virglrenderer on the
PowerVR GLES/EGL stack. Frames come back through vtest transfers into Mesa's wayland swrast path, i.e. wl_shm buffers
attached to ATL's OWN wl_surface (no second window, nothing new through xdgshim).
Done at the very start of main(), before GTK/EGL are initialised and before any thread exists:
 - ATL_GLFWD unset/!= "1": nothing changes (today's GSK_RENDERER / llvmpipe path).
 - socket = ATL_GLFWD_SOCK, else VTEST_SOCKET_NAME, else $XDG_RUNTIME_DIR/atlgl-<pid>.sock.
 - if nothing listens there and ATL_GLFWD_SERVER (default /media/developer/atl/gpu/virgl_test_server) is executable,
   it is started (same process group as ATL, so atld's killpg also ends it; PR_SET_PDEATHSIG too) with a clean
   environment, args "--use-gles --socket-path <sock>" + ATL_GLFWD_SERVER_ARGS, log ATL_GLFWD_LOG (default
   /dev/null); ATL waits <= ATL_GLFWD_WAIT_MS (2000) for the socket.
 - socket connects: GALLIUM_DRIVER=virpipe LIBGL_ALWAYS_SOFTWARE=1 VTEST_SOCKET_NAME=<sock> and
   GSK_RENDERER=ATL_GLFWD_GSK (default "gl" = the new GL renderer, called "ngl" before GTK 4.18; "cairo" keeps GTK on cairo and forwards only the app's GL).
   Otherwise one log line and today's software path (GSK_RENDERER untouched).
usage: glfwd.py <atl src root>"""
import os, sys
p = os.path.join(sys.argv[1], "src/main-executable/main.c")
s = open(p).read()
if "glfwd_setup" in s:
    print("glfwd: already applied")
    sys.exit(0)

INC_OLD = "#include <sys/resource.h>\n#include <sys/stat.h>\n"
INC_NEW = """#include <sys/resource.h>
#include <sys/stat.h>
#include <signal.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
"""
FN_ANCHOR = "int main(int argc, char **argv)\n{"
FN = r'''static int glfwd_probe(const char *path)
{
	struct sockaddr_un a = {.sun_family = AF_UNIX};
	if (strlen(path) >= sizeof(a.sun_path))
		return 0;
	strcpy(a.sun_path, path);
	int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
	if (fd < 0)
		return 0;
	int ok = connect(fd, (struct sockaddr *)&a, sizeof(a)) == 0;
	close(fd);
	return ok;
}

static int glfwd_env(char **env, int n, const char *name)
{
	const char *v = getenv(name);
	if (v && n < 30)
		env[n++] = g_strdup_printf("%s=%s", name, v);
	return n;
}

/* ATL_GLFWD=1: route all GL (GSK ngl + app EGL/GLES) through virgl vtest; see tvpatches/glfwd.py */
static void glfwd_setup(void)
{
	const char *on = getenv("ATL_GLFWD");
	if (!on || strcmp(on, "1"))
		return;
	char *sock = g_strdup(getenv("ATL_GLFWD_SOCK") ?: getenv("VTEST_SOCKET_NAME") ?: "");
	if (!*sock) {
		g_free(sock);
		sock = g_strdup_printf("%s/atlgl-%d.sock", getenv("XDG_RUNTIME_DIR") ?: "/tmp", getpid());
	}
	int ok = glfwd_probe(sock);
	const char *srv = getenv("ATL_GLFWD_SERVER") ?: "/media/developer/atl/gpu/virgl_test_server";
	if (!ok && access(srv, X_OK) == 0) {
		unlink(sock);
		pid_t pid = fork();
		if (pid == 0) {
			prctl(PR_SET_PDEATHSIG, SIGTERM);
			const char *log = getenv("ATL_GLFWD_LOG") ?: "/dev/null";
			/* (main.c has its own static open(), so no fcntl.h here) */
			if (freopen(log, "w", stdout))
				dup2(fileno(stdout), 2);
			char *argv[40] = {(char *)srv, "--use-gles", "--socket-path", sock};
			int argc = 4;
			char **extra = g_strsplit(getenv("ATL_GLFWD_SERVER_ARGS") ?: "", " ", 32);
			for (char **e = extra; *e && argc < 38; e++)
				if (**e)
					argv[argc++] = *e;
			argv[argc] = NULL;
			/* clean environment: the AppDir's Mesa/glvnd/LD_* settings would break the host (TV) EGL stack */
			char *env[32];
			int n = 0;
			n = glfwd_env(env, n, "PATH");
			n = glfwd_env(env, n, "HOME");
			n = glfwd_env(env, n, "XDG_RUNTIME_DIR");
			n = glfwd_env(env, n, "VREND_DEBUG");
			char **pass = g_strsplit(getenv("ATL_GLFWD_SERVER_ENV") ?: "", " ", 16);
			for (char **e = pass; *e && n < 30; e++)
				if (strchr(*e, '='))
					env[n++] = *e;
			env[n] = NULL;
			execve(srv, argv, env);
			_exit(127);
		}
		if (pid > 0) {
			int wait_ms = atoi(getenv("ATL_GLFWD_WAIT_MS") ?: "2000");
			for (int t = 0; t < wait_ms && !(ok = glfwd_probe(sock)); t += 20)
				nanosleep(&(struct timespec){0, 20 * 1000 * 1000}, NULL);
			if (!ok)
				kill(pid, SIGKILL);
		}
	}
	if (!ok) {
		fprintf(stderr, "ATL_GLFWD: no vtest server at %s (server %s), keeping software rendering\n", sock, srv);
		g_free(sock);
		return;
	}
	setenv("GALLIUM_DRIVER", "virpipe", 1);
	setenv("LIBGL_ALWAYS_SOFTWARE", "1", 1);
	setenv("VTEST_SOCKET_NAME", sock, 1);
	setenv("GSK_RENDERER", getenv("ATL_GLFWD_GSK") ?: "gl", 1);
	fprintf(stderr, "ATL_GLFWD: GL via virgl vtest %s, GSK_RENDERER=%s\n", sock, getenv("GSK_RENDERER"));
	g_free(sock);
}

'''
CALL_OLD = """	pregrow_stack();
"""
CALL_NEW = """	pregrow_stack();

	glfwd_setup();
"""
for old in (INC_OLD, FN_ANCHOR, CALL_OLD):
    assert s.count(old) == 1, "glfwd: anchor not found once: " + old[:40]
s = s.replace(INC_OLD, INC_NEW).replace(FN_ANCHOR, FN + FN_ANCHOR).replace(CALL_OLD, CALL_NEW)
open(p, "w").write(s)
print("glfwd: applied")
