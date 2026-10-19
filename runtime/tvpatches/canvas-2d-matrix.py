#!/usr/bin/env python3
"""Canvas.concat(Matrix) with an affine matrix is a 2D transform, not an "unknown" one.
GskCanvas.native_concat used gtk_snapshot_transform_matrix(), which makes a GskTransform of category UNKNOWN. GTK's
cairo renderer cannot draw such transform nodes and fills their bounds with hot pink (gsktransformnode.c). ATL renders
Bitmaps off the main thread with the cairo renderer (Bitmap.native_create_texture), so every bitmap that was drawn
through a matrix on a worker thread came out solid pink: Glide's CenterCrop/FitCenter/RoundedCorners
(TransformationUtils.applyMatrix -> canvas.drawBitmap(bmp, matrix, paint)), likewise Picasso/Coil transformations.
SmartTube and FilmPlus thumbnails were all pink. Affine matrices now go through gsk_transform_matrix_2d() (category 2D,
translate+scale fast path); matrices with perspective keep the old path.
usage: canvas-2d-matrix.py <atl src root>"""
import os, sys
p = os.path.join(sys.argv[1], "src/api-impl-jni/graphics/android_atl_GskCanvas.c")
s = open(p).read()
OLD = """	graphene_matrix_t *matrix = (graphene_matrix_t *)_PTR(matrix_ptr);
	gtk_snapshot_transform_matrix(snapshot, matrix);
"""
NEW = """	graphene_matrix_t *matrix = (graphene_matrix_t *)_PTR(matrix_ptr);
	double xx, yx, xy, yy, dx, dy;
	if (graphene_matrix_is_2d(matrix) && graphene_matrix_to_2d(matrix, &xx, &yx, &xy, &yy, &dx, &dy)) {
		// 2D category: the cairo renderer (used for Bitmaps rendered off the main thread) draws UNKNOWN-category transforms pink
		GskTransform *transform = gsk_transform_matrix_2d(NULL, xx, yx, xy, yy, dx, dy);
		gtk_snapshot_transform(snapshot, transform);
		gsk_transform_unref(transform);
	} else {
		gtk_snapshot_transform_matrix(snapshot, matrix);
	}
"""
if NEW in s:
    print("canvas-2d-matrix: already applied")
    sys.exit(0)
assert OLD in s, "native_concat not found in " + p
open(p, "w").write(s.replace(OLD, NEW))
print("canvas-2d-matrix: applied")
