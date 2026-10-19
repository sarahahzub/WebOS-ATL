#!/usr/bin/env python3
"""Context.getCodeCacheDir() is <data dir>/code_cache (created), like Android. It was a StubGen stub returning null, so
DexClassLoader users (FilmPlus' downloaded scraper dex) got a relative optimized dir.
usage: context-codecache.py <atl src root>"""
import os, sys
p = os.path.join(sys.argv[1], "src/api-impl/android/content/Context.java")
s = open(p).read()
if "getCodeCacheDir" in s:
    sys.exit(0)


def rep(old, new):
    global s
    assert old in s, old[:60]
    s = s.replace(old, new, 1)


rep("""	public File getCacheDir() {
""", """	public File getCodeCacheDir() {
		File code_cache_dir = new File(getDataDirFile(), "code_cache");
		if (!code_cache_dir.exists() && !code_cache_dir.mkdirs() && !code_cache_dir.exists())
			Slog.w(TAG, "Unable to create code_cache directory >" + code_cache_dir.getPath() + "<");
		return code_cache_dir;
	}

	public File getCacheDir() {
""")
open(p, "w").write(s)
