#!/usr/bin/env python3
"""MediaSession.setPlaybackState is null-safe: a null state returns, and the active queue item's title/subtitle/iconUri
may be null (media3 legacy queue items have subtitle == null), as may its description. Before, every media3 app that
publishes a queue died in loadExo with an NPE (CloudStream: "loadExo error" on every link, back to Home).
usage: mediasession-null.py <atl src root>"""
import os, sys
p = os.path.join(sys.argv[1], "src/api-impl/android/media/session/MediaSession.java")
s = open(p).read()
if "private static String str(" in s:
    sys.exit(0)


def rep(old, new):
    global s
    assert old in s, old[:60]
    s = s.replace(old, new, 1)


rep("""	public void setPlaybackState(PlaybackState state) {
		String title = null;""", """	private static String str(Object o) {
		return o == null ? null : o.toString();
	}

	public void setPlaybackState(PlaybackState state) {
		if (state == null)
			return;
		String title = null;""")
rep("""				if (item.id == state.activeQueueItemId) {
					title = item.description.title.toString();
					subTitle = item.description.subtitle.toString();
					artUrl = item.description.iconUri == null ? null : item.description.iconUri.toString();
					break;
				}""", """				if (item.id == state.activeQueueItemId) {
					MediaDescription d = item.description;
					if (d != null) {
						title = str(d.title);
						subTitle = str(d.subtitle);
						artUrl = str(d.iconUri);
					}
					break;
				}""")
open(p, "w").write(s)
