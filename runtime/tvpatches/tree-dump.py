#!/usr/bin/env python3
"""Diagnostics: `touch /tmp/atltree` logs the main window's widget tree to stderr (app.log), lines "ATLTREE ...":
depth, GTK type, Java class (WrapperWidget), position/size in the window, opacity, background paintable and, for
pictures/backgrounds, the paintable type. Only mapped widgets of at least 200x100 are printed. Always installed, costs
one stat() per second until the file appears (it is removed after the dump) (used to find what covered CloudStream's video).
usage: tree-dump.py <atl src root>"""
import os, sys
p = os.path.join(sys.argv[1], "src/api-impl-jni/views/android_view_View.c")
s = open(p).read()
if "ATLTREE" in s:
    sys.exit(0)
s = s.replace("#include <gtk/gtk.h>\n", "#include <gtk/gtk.h>\n#include <unistd.h>\n", 1)
s += r'''

/* ---- tv: widget tree dump on `touch /tmp/atltree` (ATLTREE) ---- */
static void atltree_walk(GtkWidget *w, GtkWidget *root, int depth)
{
	int ww = gtk_widget_get_width(w), hh = gtk_widget_get_height(w);
	if (gtk_widget_get_mapped(w) && ww >= 200 && hh >= 100) {
		graphene_point_t pt = GRAPHENE_POINT_INIT(0, 0);
		gtk_widget_compute_point(w, root, &GRAPHENE_POINT_INIT(0, 0), &pt);
		const char *jc = "";
		const char *bg = "";
		if (WRAPPER_IS_WIDGET(w)) {
			WrapperWidget *wr = WRAPPER_WIDGET(w);
			if (wr->jobj)
				jc = jcls(wr->jobj);
			if (wr->background) {
				GdkPaintable *pa = GTK_IS_PICTURE(wr->background) ? gtk_picture_get_paintable(GTK_PICTURE(wr->background)) : NULL;
				bg = pa ? G_OBJECT_TYPE_NAME(pa) : "bg-widget";
			}
		}
		const char *paint = "";
		if (GTK_IS_PICTURE(w)) {
			GdkPaintable *pa = gtk_picture_get_paintable(GTK_PICTURE(w));
			paint = pa ? G_OBJECT_TYPE_NAME(pa) : "none";
		}
		fprintf(stderr, "ATLTREE %*s%s %s @%.0f,%.0f %dx%d op=%.2f%s%s%s%s\n", depth, "", G_OBJECT_TYPE_NAME(w), jc, pt.x, pt.y, ww, hh,
		        gtk_widget_get_opacity(w), *bg ? " bg=" : "", bg, *paint ? " paint=" : "", paint);
	}
	for (GtkWidget *c = gtk_widget_get_first_child(w); c; c = gtk_widget_get_next_sibling(c))
		if (gtk_widget_get_mapped(c))
			atltree_walk(c, root, depth + 1);
}

static gboolean atltree_dump(gpointer data)
{
	if (access("/tmp/atltree", F_OK))
		return G_SOURCE_CONTINUE;
	unlink("/tmp/atltree");
	fprintf(stderr, "ATLTREE ---- dump ----\n");
	if (window)
		atltree_walk(GTK_WIDGET(window), GTK_WIDGET(window), 0);
	fprintf(stderr, "ATLTREE ---- end ----\n");
	return G_SOURCE_CONTINUE;
}

__attribute__((constructor)) static void atltree_init(void)
{
	g_timeout_add_seconds(1, atltree_dump, NULL);
}
'''
open(p, "w").write(s)
